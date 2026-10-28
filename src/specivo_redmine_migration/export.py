"""Write a Redmine installation to an import bundle.

:func:`export_bundle` reads everything :class:`~specivo_redmine_migration.source.RedmineSource`
yields and writes it with the SDK's :class:`~specivo_migration_sdk.BundleWriter`,
converting text to Markdown on the way. It carries what Specivo's direct
Redmine import carries, under the same source refs, so a bundle can continue an
installation that was imported directly.

Where the direct import resolved a reference against what it had imported and
fell back when it could not (an author who is not a user any more, a parent
issue outside the selected projects), the bundle leaves the reference out, so
Specivo's bundle import falls back the same way. A record the direct import
skipped for the same reason (a relation to an issue outside the scope) is left
out and counted in the manifest's ``not_carried_over``.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from specivo_migration_sdk import (
    BundleWriter,
    CustomField,
    CustomValue,
    Issue,
    Journal,
    Project,
    PublicAccess,
    User,
    WikiPage,
)

from specivo_redmine_migration import db, extract
from specivo_redmine_migration.extract import SourceAttachment
from specivo_redmine_migration.files import AttachmentPathError, resolve_attachment_path
from specivo_redmine_migration.references import issue_placeholders
from specivo_redmine_migration.source import RedmineSource
from specivo_redmine_migration.textile_convert import ConversionContext, RedmineTextileConverter

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "redmine"
GENERATOR_NAME = "specivo-redmine-migration"

# What progress messages go to; the command prints them to stderr.
type Progress = Callable[[str], None]


def generator_version() -> str:
    try:
        return version(GENERATOR_NAME)
    except PackageNotFoundError:  # pragma: no cover - running from an uninstalled checkout
        return "0"


class ExportError(Exception):
    """The export cannot start as asked: a missing option, an unknown project, a target in the way."""


@dataclass(kw_only=True)
class ExportOptions:
    """What to export and where.

    ``projects`` names projects by identifier or id; each comes with its
    subprojects, as with Specivo's ``--project``. ``limit`` keeps at most that
    many issues per project (parents first), for a trial run.
    """

    db_url: str
    out: Path
    files_dir: Path | None = None
    instance: str | None = None
    projects: Sequence[str] = ()
    import_passwords: bool = False
    limit: int | None = None
    zip: bool | None = None
    status_category_map: dict[str, str] = field(default_factory=dict)
    cf_key_map: dict[str, str] = field(default_factory=dict)
    batch_size: int = 500


@dataclass
class ExportSummary:
    """What an export wrote."""

    path: Path
    instance: str
    counts: dict[str, int]
    not_carried_over: list[str]
    conversion_failures: int
    zero_dates: int


def _silent(message: str) -> None:
    del message


async def export_bundle(options: ExportOptions, progress: Progress = _silent) -> ExportSummary:
    """Read the Redmine database in *options* and write its bundle to ``options.out``."""
    source = RedmineSource(
        options.db_url,
        status_category_overrides=options.status_category_map,
        cf_key_overrides=options.cf_key_map,
        batch_size=options.batch_size,
    )
    progress(f"Reading {db.safe_url(db.normalise_source_url(options.db_url))}")
    try:
        await source.connect()
        instance = options.instance or _instance_from_host_name(source.host_name)
        extract.zero_dates.reset()
        return await _Export(source, options, instance, progress).run()
    finally:
        await source.close()


# Host names that do not tell one Redmine from another: Redmine's own default
# (localhost:3000), loopback and unspecified addresses, and documentation
# domains. Two installations left at these would share one identifier map.
_PLACEHOLDER_HOST_RE = re.compile(r"^(localhost\b|127\.|\[::1\]|::1\b|0\.0\.0\.0\b|example\.)", re.IGNORECASE)

_NAME_IT = (
    "pass --instance with a name for this installation (its public host name, say), and the same name on "
    "every later export of it."
)


def _instance_from_host_name(host_name: str | None) -> str:
    """Return Redmine's host name as the instance, or explain why it cannot be one."""
    if not host_name:
        raise ExportError(f"Redmine's host name setting is empty, so the bundle cannot be named after it: {_NAME_IT}")
    if _PLACEHOLDER_HOST_RE.match(host_name):
        raise ExportError(
            f"Redmine's host name setting is {host_name!r}, a default or local address that does not identify "
            f"this installation: {_NAME_IT}"
        )
    return host_name


class _Export:
    """One export run: the scope, the refs written so far, and the writer."""

    def __init__(self, source: RedmineSource, options: ExportOptions, instance: str, progress: Progress) -> None:
        self.source = source
        self.options = options
        self.instance = instance
        self.progress = progress
        self.converter = RedmineTextileConverter()
        self.context = ConversionContext(source_format=source.source_format)
        # Refs written, by kind, for the references that point at them.
        self.users: set[str] = set()
        self.groups: set[str] = set()
        self.trackers: set[str] = set()
        self.roles: set[str] = set()
        self.activities: set[str] = set()
        self.projects: set[str] = set()
        self.versions: set[str] = set()
        self.categories: set[str] = set()
        self.issues: set[str] = set()
        # Every issue of Redmine, in the bundle or not, for references to them.
        self.redmine_issues: frozenset[str] = frozenset()
        self.relations: set[str] = set()
        self.notes: list[str] = []

    # ------------------------------------------------------------------
    # The run
    # ------------------------------------------------------------------

    async def run(self) -> ExportSummary:
        projects = await self._selected_projects()
        orders = {project.source_ref: await self.source.issue_order(project.source_ref) for project in projects}
        if self.options.limit is not None:
            orders = {ref: order[: self.options.limit] for ref, order in orders.items()}
        # Every issue in the bundle, known before any text is written, so a
        # reference to an issue of a later project still becomes a placeholder.
        self.issues = {ref for order in orders.values() for ref in order}
        self.redmine_issues = await self.source.issue_ids()

        # A bundle with password hashes is secret: nobody else may read it,
        # not even while it is being written.
        private = self.options.import_passwords
        previous_umask = os.umask(0o077) if private else None
        try:
            if private and not self._writes_zip():
                self._make_private_directory(self.options.out)
            try:
                writer = BundleWriter(
                    self.options.out,
                    source_system=SOURCE_SYSTEM,
                    source_instance=self.instance,
                    generator_name=GENERATOR_NAME,
                    generator_version=generator_version(),
                    zip=self.options.zip,
                )
            except FileExistsError as exc:
                raise ExportError(f"{exc}: choose a new path, or remove it first") from None

            with writer:
                self.writer = writer
                await self._lookups()
                await self._principals()
                for project in projects:
                    self._project(project)
                await self._custom_fields()
                for project in projects:
                    await self._project_content(project, orders[project.source_ref])
                writer.set_public_access(await self._public_access())
                await self._instance_notes()
            if private and self._writes_zip():
                self.options.out.chmod(0o600)
        finally:
            if previous_umask is not None:
                os.umask(previous_umask)
        self.progress(f"Wrote {self.options.out}")
        return ExportSummary(
            path=self.options.out,
            instance=self.instance,
            counts=writer.counts,
            not_carried_over=self.notes,
            conversion_failures=self.converter.failure_count,
            zero_dates=extract.zero_dates.count,
        )

    def _writes_zip(self) -> bool:
        """Whether the bundle is an archive, by the rule the SDK's writer uses."""
        if self.options.zip is not None:
            return self.options.zip
        return self.options.out.suffix.lower() == ".zip"

    @staticmethod
    def _make_private_directory(path: Path) -> None:
        """Create *path* readable by its owner only, or restrict an existing empty one."""
        if path.exists():
            if path.is_dir() and not any(path.iterdir()):
                path.chmod(0o700)
            # Anything else is refused by the writer, with its own message.
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir(mode=0o700)

    def _not_carried(
        self, what: str, *, count: int | None = None, entity_type: str | None = None, source_ref: str | None = None
    ) -> None:
        self.writer.not_carried_over(what, count=count, entity_type=entity_type, source_ref=source_ref)
        self.notes.append(what if count is None else f"{what} ({count})")

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------

    async def _selected_projects(self) -> list[Project]:
        """Return the projects to export, parents first, with their dropped modules noted later.

        A selected project brings its subprojects. A project whose parent is
        outside the scope becomes a root, as the direct import makes it.
        """
        projects = [project async for project in self.source.extract_projects()]
        if not self.options.projects:
            return projects

        by_name = {project.identifier: project.source_ref for project in projects}
        by_ref = {project.source_ref for project in projects}
        wanted: set[str] = set()
        for selector in self.options.projects:
            name = selector.strip()
            ref = by_name.get(name.lower()) or (name if name in by_ref else None)
            if ref is None:
                raise ExportError(f"No project {name!r} in Redmine (give its identifier or its id)")
            wanted.add(ref)

        selected: list[Project] = []
        included: set[str] = set()
        for project in projects:
            if project.source_ref in wanted or project.parent_ref in included:
                included.add(project.source_ref)
                if project.parent_ref not in included:
                    project = replace(project, parent_ref=None)
                selected.append(project)
        return selected

    # ------------------------------------------------------------------
    # Instance-wide records
    # ------------------------------------------------------------------

    async def _lookups(self) -> None:
        lookups = await self.source.extract_lookups()
        for tracker in lookups.trackers:
            self.writer.add(tracker)
            self.trackers.add(tracker.source_ref)
        for status in lookups.statuses:
            self.writer.add(status)
        for priority in lookups.priorities:
            self.writer.add(priority)
        for activity in lookups.activities:
            self.writer.add(activity)
            self.activities.add(activity.source_ref)
        for role in lookups.roles:
            self.writer.add(role)
            self.roles.add(role.source_ref)
        self.progress(
            f"Lookups: {len(lookups.trackers)} trackers, {len(lookups.statuses)} statuses, "
            f"{len(lookups.priorities)} priorities, {len(lookups.activities)} activities, {len(lookups.roles)} roles"
        )

    async def _principals(self) -> None:
        async for user in self.source.extract_users():
            self.writer.add(self._user(user))
            self.users.add(user.source_ref)
        async for group in self.source.extract_groups():
            group.member_refs = [ref for ref in group.member_refs if ref in self.users]
            self.writer.add(group)
            self.groups.add(group.source_ref)
        self.progress(f"Users: {len(self.users)}, groups: {len(self.groups)}")

    def _user(self, user: User) -> User:
        """Keep the Redmine credential only when passwords were asked for, and never an administrator's.

        Specivo makes every imported administrator set a new password, so an
        administrator's hash would only be one more secret in the bundle.
        """
        if not self.options.import_passwords or user.is_admin:
            user.credential = None
        return user

    def _project(self, project: Project) -> None:
        self.writer.add(project)
        self.projects.add(project.source_ref)
        for module in self.source.dropped_modules(project.source_ref):
            self._not_carried(f"module {module}", entity_type="project", source_ref=project.source_ref)

    async def _custom_fields(self) -> None:
        async for custom_field in self.source.extract_custom_fields():
            self.writer.add(self._custom_field(custom_field))
        for name in self.source.relaxed_required_fields:
            self._not_carried(f"required flag of custom field {name!r}: some issues have no value")

    def _custom_field(self, custom_field: CustomField) -> CustomField:
        custom_field.tracker_refs = [ref for ref in custom_field.tracker_refs if ref in self.trackers]
        custom_field.project_refs = [ref for ref in custom_field.project_refs if ref in self.projects]
        return custom_field

    async def _public_access(self) -> PublicAccess:
        access = await self.source.extract_public_access()
        access.anonymous_permissions = {
            ref: permissions for ref, permissions in access.anonymous_permissions.items() if ref in self.projects
        }
        return access

    async def _instance_notes(self) -> None:
        """List what the instance had that no project in the bundle carries."""
        enumerations = await self.source.count_project_enumerations()
        if enumerations:
            self._not_carried("per-project priorities and activities", count=enumerations)
        fields = await self.source.count_non_issue_custom_fields()
        if fields:
            self._not_carried("custom fields on users, projects, versions, groups and time entries", count=fields)
        for container, count in sorted((await self.source.count_unsupported_attachments()).items()):
            self._not_carried(f"attachments on {container} records", count=count)
        for ref in self.source.undated_time_entries:
            self._not_carried("time entry without a date", entity_type="time_entry", source_ref=ref)
        if extract.zero_dates.count:
            self._not_carried("dates stored as 0000-00-00 (left empty)", count=extract.zero_dates.count)
            self.progress(f"MySQL zero dates left empty: {extract.zero_dates.count}")
        for ref, relation_type in sorted(self.source.unmapped_relation_types.items()):
            self._not_carried(f"relation type {relation_type}", entity_type="relation", source_ref=ref)

    # ------------------------------------------------------------------
    # Per project
    # ------------------------------------------------------------------

    async def _project_content(self, project: Project, order: list[str]) -> None:
        ref = project.source_ref
        counts: dict[str, int] = {}

        def count(name: str) -> None:
            counts[name] = counts.get(name, 0) + 1

        async for membership in self.source.extract_memberships(ref):
            known = self.users if membership.principal_kind == "user" else self.groups
            membership.role_refs = [role for role in membership.role_refs if role in self.roles]
            if membership.principal_ref in known and membership.role_refs:
                self.writer.add(membership)
                count("memberships")
        async for project_version in self.source.extract_versions(ref):
            self.writer.add(project_version)
            self.versions.add(project_version.source_ref)
        async for category in self.source.extract_categories(ref):
            category.assigned_to_ref = self._user_ref(category.assigned_to_ref)
            self.writer.add(category)
            self.categories.add(category.source_ref)

        async for issue in self.source.extract_issues(order):
            self.writer.add(self._issue(issue))
            count("issues")

        issue_ids = [int(issue_ref) for issue_ref in order]
        async for journal in self.source.extract_journals(issue_ids):
            self.writer.add(self._journal(journal))
            count("journals")
        async for relation in self.source.extract_relations(issue_ids):
            # A relation across two projects is read under both.
            if relation.source_ref in self.relations:
                continue
            self.relations.add(relation.source_ref)
            if relation.from_ref in self.issues and relation.to_ref in self.issues:
                self.writer.add(relation)
                count("relations")
            else:
                self._not_carried(
                    "relation to an issue outside the exported projects",
                    entity_type="relation",
                    source_ref=relation.source_ref,
                )
        async for watcher in self.source.extract_watchers(issue_ids):
            if watcher.user_ref in self.users:
                self.writer.add(watcher)

        async for page in self.source.extract_wiki_pages(ref):
            self.writer.add(self._wiki_page(page))
            count("wiki pages")
        async for watcher in self.source.extract_wiki_watchers(ref):
            if watcher.user_ref in self.users:
                self.writer.add(watcher)
        async for redirect in self.source.extract_wiki_redirects(ref):
            self.writer.add(redirect)

        if self.options.files_dir is None:
            skipped = 0
            async for _ in self.source.extract_attachments(ref, issue_ids):
                skipped += 1
            if skipped:
                self._not_carried(
                    "attachments: no --files-dir was given", count=skipped, entity_type="project", source_ref=ref
                )
        else:
            async for attachment in self.source.extract_attachments(ref, issue_ids):
                if self._attachment(attachment, self.options.files_dir):
                    count("attachments")

        async for entry in self.source.extract_time_entries(ref):
            if entry.issue_ref not in self.issues:
                # Kept against the project, as the direct import keeps time
                # logged on an issue it did not import.
                entry.issue_ref = None
            entry.user_ref = self._user_ref(entry.user_ref)
            if entry.activity_ref not in self.activities:
                # A per-project activity: Specivo skips the entry, as the direct import does.
                entry.activity_ref = None
            self.writer.add(entry)
            count("time entries")

        summary = ", ".join(f"{number} {name}" for name, number in counts.items()) or "nothing"
        self.progress(f"Project {project.identifier}: {summary}")

    # ------------------------------------------------------------------
    # Records with text or references
    # ------------------------------------------------------------------

    def _markdown(self, text: str | None, *, issue_references: bool) -> str | None:
        if not text:
            return text
        converted = self.converter.convert(text, self.context)
        return issue_placeholders(converted, self.issues, self.redmine_issues) if issue_references else converted

    def _user_ref(self, ref: str | None) -> str | None:
        return ref if ref in self.users else None

    def _issue(self, issue: Issue) -> Issue:
        issue.description = self._markdown(issue.description, issue_references=True)
        issue.author_ref = self._user_ref(issue.author_ref)
        if issue.assigned_to_ref not in self.users and issue.assigned_to_ref not in self.groups:
            issue.assigned_to_ref = None
        if issue.parent_ref not in self.issues:
            issue.parent_ref = None
        if issue.category_ref not in self.categories:
            issue.category_ref = None
        if issue.fixed_version_ref not in self.versions:
            issue.fixed_version_ref = None
        issue.custom_values = [value for value in map(self._custom_value, issue.custom_values) if value is not None]
        return issue

    def _custom_value(self, value: CustomValue) -> CustomValue | None:
        """Write a reference value as refs (strings), keeping only those in the bundle."""
        if value.value_kind == "scalar":
            return value
        known: Collection[str] = self.users if value.value_kind == "user_ref" else self.versions
        if isinstance(value.value, list):
            value.value = [str(item) for item in value.value if str(item) in known]
            return value
        return replace(value, value=str(value.value)) if str(value.value) in known else None

    def _journal(self, journal: Journal) -> Journal:
        journal.notes = self._markdown(journal.notes, issue_references=True)
        journal.user_ref = self._user_ref(journal.user_ref)
        return journal

    def _wiki_page(self, page: WikiPage) -> WikiPage:
        for revision in page.versions:
            # The direct import leaves #N in wiki text as written.
            revision.text = self._markdown(revision.text, issue_references=False) or ""
            revision.author_ref = self._user_ref(revision.author_ref)
        return page

    def _attachment(self, attachment: SourceAttachment, files_dir: Path) -> bool:
        """Store one attachment's bytes and write its record; note it instead if the file cannot be read."""
        try:
            path = resolve_attachment_path(files_dir, attachment.storage_key)
            stored = self.writer.add_file(path)
        except (OSError, AttachmentPathError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            self._not_carried(
                f"attachment {attachment.filename}: {reason}",
                entity_type="attachment",
                source_ref=attachment.source_ref,
            )
            return False
        attachment.author_ref = self._user_ref(attachment.author_ref)
        self.writer.add(attachment.record(stored.sha256, stored.size))
        return True
