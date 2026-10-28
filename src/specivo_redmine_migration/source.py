"""Read a Redmine database: the queries, ported from Specivo's direct import.

:class:`RedmineSource` connects to a Redmine database and yields bundle records
(and, for attachments, :class:`~specivo_redmine_migration.extract.SourceAttachment`).
It is the only place in the tool that knows Redmine's schema. The queries are
the ones Specivo's direct Redmine import ran, so both read an installation the
same way; :mod:`specivo_redmine_migration.export` decides what goes in the bundle.

Streams are paged rather than materialised, and the two that have to be ordered
parents-first, projects and issues, say so on the method that produces them.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from specivo_migration_sdk import (
    Activity,
    Category,
    CustomField,
    CustomValue,
    Group,
    Issue,
    Journal,
    Membership,
    Priority,
    Project,
    PublicAccess,
    Relation,
    Role,
    Status,
    TimeEntry,
    Tracker,
    User,
    Version,
    Watcher,
    WikiPage,
    WikiRedirect,
    WikiVersion,
)
from specivo_migration_sdk.models import PrincipalKind
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from specivo_redmine_migration import db, extract

logger = logging.getLogger(__name__)

# Redmine's ``text_formatting`` setting. An instance switched to Markdown needs
# no Textile conversion, only link rewriting.
TEXTILE_FORMAT = "textile"
_MARKDOWN_FORMATS = frozenset({"markdown", "common_mark"})

# enumerations.type discriminators.
_PRIORITY_TYPE = "IssuePriority"
_ACTIVITY_TYPE = "TimeEntryActivity"


@dataclass(kw_only=True, slots=True)
class Lookups:
    """The instance-wide lists, which go in ``lookups.json``."""

    trackers: list[Tracker] = field(default_factory=list)
    statuses: list[Status] = field(default_factory=list)
    priorities: list[Priority] = field(default_factory=list)
    activities: list[Activity] = field(default_factory=list)
    roles: list[Role] = field(default_factory=list)


class RedmineSource:
    """Reads a Redmine 7.x database.

    ``status_category_overrides`` maps a status name, lower-cased, to one of
    Specivo's status categories. Redmine records only whether a status closes an
    issue, so the rest is inferred and the operator gets the final say.
    ``cf_key_overrides`` maps a custom field name, lower-cased, to the metadata
    key it is stored under.
    """

    # Wiki pages whose revisions are read in one query.
    wiki_page_batch = 10

    def __init__(
        self,
        db_url: str,
        status_category_overrides: dict[str, str] | None = None,
        cf_key_overrides: dict[str, str] | None = None,
        batch_size: int = 500,
    ) -> None:
        self._url = db_url
        self._status_overrides = {k.strip().lower(): v for k, v in (status_category_overrides or {}).items()}
        self._cf_key_overrides = {k.strip().lower(): v for k, v in (cf_key_overrides or {}).items()}
        self._batch_size = batch_size
        self._engine: AsyncEngine | None = None
        self._conn: AsyncConnection | None = None
        self._source_format: str | None = None
        self._host_name: str | None = None
        # Filled while streaming, for the manifest's not_carried_over.
        self._dropped_modules: dict[str, list[str]] = {}
        self._custom_fields: dict[int, CustomField] | None = None
        self.relaxed_required_fields: list[str] = []
        self.unmapped_relation_types: dict[str, str] = {}
        # Time entries whose day is a zero date and that have no creation time either.
        self.undated_time_entries: list[str] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def engine(self) -> AsyncEngine:
        """Return the connected engine, or explain that connect was not called."""
        if self._engine is None:
            raise RuntimeError("RedmineSourceAdapter.connect() must be called before reading the source")
        return self._engine

    async def connect(self) -> None:
        """Open the connection, start the export's snapshot, and read the settings it depends on.

        Every query of the export runs on this one connection, in one
        transaction that is read-only and repeatable-read (see
        :func:`db.begin_snapshot`), so the whole bundle shows Redmine as it
        was at one moment, even while people keep working in it.
        """
        self._engine = db.create_source_engine(self._url)
        self._conn = await self._engine.connect()
        await db.begin_snapshot(self._conn)
        async with self._connection() as conn:
            # Doubles as a reachability check: a bad URL or missing table fails
            # here rather than half way through a long export.
            self._source_format = (await db.fetch_setting(conn, "text_formatting")) or TEXTILE_FORMAT
            self._host_name = ((await db.fetch_setting(conn, "host_name")) or "").strip() or None
        logger.info("Source text formatting: %s", self._source_format)

    @property
    def host_name(self) -> str | None:
        """Return Redmine's ``host_name`` setting, or ``None`` when it is unset or blank."""
        return self._host_name

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[AsyncConnection]:
        """Lend the export's connection. It stays open, in its transaction, until :meth:`close`."""
        if self._conn is None:
            raise RuntimeError("RedmineSource.connect() must be called before reading the source")
        yield self._conn

    async def close(self) -> None:
        """End the snapshot (nothing was written, so it is rolled back) and dispose of the engine.

        Safe when connect failed or never ran.
        """
        if self._conn is not None:
            try:
                await self._conn.rollback()
                await self._conn.close()
            finally:
                self._conn = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    @property
    def source_format(self) -> str:
        """Return the markup Redmine stores, e.g. ``textile`` or ``markdown``."""
        return self._source_format or TEXTILE_FORMAT

    @property
    def needs_textile_conversion(self) -> bool:
        """Whether the source stores Textile rather than Markdown."""
        return self.source_format not in _MARKDOWN_FORMATS

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    async def extract_lookups(self) -> Lookups:
        """Read trackers, statuses, priorities, activities and roles.

        These are instance-wide and small, so they are read in one pass rather
        than streamed.

        Priorities and activities live in Redmine's shared ``enumerations``
        table. Only the instance-wide rows are taken: a row with a
        ``project_id`` is a per-project override, which Specivo cannot express
        because its priorities and activities are global.
        """
        async with self._connection() as conn:
            tracker_rows = (await conn.execute(select(db.trackers).order_by(db.trackers.c.id))).mappings().all()
            status_rows = (
                (await conn.execute(select(db.issue_statuses).order_by(db.issue_statuses.c.id))).mappings().all()
            )
            priority_rows = (
                (
                    await conn.execute(
                        select(db.enumerations)
                        .where(
                            db.enumerations.c.type == _PRIORITY_TYPE,
                            db.enumerations.c.project_id.is_(None),
                        )
                        .order_by(db.enumerations.c.id)
                    )
                )
                .mappings()
                .all()
            )
            activity_rows = (
                (
                    await conn.execute(
                        select(db.enumerations)
                        .where(
                            db.enumerations.c.type == _ACTIVITY_TYPE,
                            db.enumerations.c.project_id.is_(None),
                        )
                        .order_by(db.enumerations.c.id)
                    )
                )
                .mappings()
                .all()
            )
            role_rows = (await conn.execute(select(db.roles).order_by(db.roles.c.id))).mappings().all()

        return Lookups(
            trackers=[extract.extract_tracker(dict(row)) for row in tracker_rows],
            statuses=[extract.extract_status(dict(row), self._status_overrides) for row in status_rows],
            priorities=[extract.extract_priority(dict(row)) for row in priority_rows],
            activities=[extract.extract_activity(dict(row)) for row in activity_rows],
            roles=[extract.extract_role(dict(row)) for row in role_rows],
        )

    async def count_project_enumerations(self) -> int:
        """Count per-project priority and activity overrides, which are dropped.

        Reported so an operator who relies on them learns that they did not
        survive, instead of discovering it later.
        """
        async with self._connection() as conn:
            rows = (
                await conn.execute(
                    select(db.enumerations.c.id).where(
                        db.enumerations.c.type.in_([_PRIORITY_TYPE, _ACTIVITY_TYPE]),
                        db.enumerations.c.project_id.is_not(None),
                    )
                )
            ).all()
        return len(rows)

    # ------------------------------------------------------------------
    # Principals
    # ------------------------------------------------------------------

    async def extract_users(self) -> AsyncIterator[User]:
        """Stream people, with their default email address.

        Groups and the anonymous account share this table and are excluded:
        groups come from :meth:`extract_groups`, and Specivo represents
        anonymous access with a role rather than an account.
        """
        async with self._connection() as conn:
            emails = await self._default_emails(conn)
            stmt = select(db.users).where(db.users.c.type == extract.TYPE_USER)
            async for row in db.stream(conn, stmt, db.users.c.id, self._batch_size):
                yield extract.extract_user(row, emails.get(row["id"]))

    async def extract_groups(self) -> AsyncIterator[Group]:
        """Stream groups with their members.

        Redmine keeps groups in ``users`` alongside people, distinguished by
        ``type``; the builtin anonymous and non-member principals have types of
        their own and so are left out here. Membership is read up front: a
        Redmine instance has few groups, and the join table has no primary key
        to page on.
        """
        async with self._connection() as conn:
            members: dict[int, list[str]] = {}
            membership_rows = (await conn.execute(select(db.groups_users))).mappings().all()
            for membership in membership_rows:
                members.setdefault(membership["group_id"], []).append(str(membership["user_id"]))

            stmt = select(db.users).where(db.users.c.type == extract.TYPE_GROUP)
            async for row in db.stream(conn, stmt, db.users.c.id, self._batch_size):
                yield extract.extract_group(row, members.get(row["id"], []))

    async def _default_emails(self, conn: Any) -> dict[int, str]:
        """Return each user's default address.

        Redmine allows several addresses per account; the default one is the
        login address. Read in one pass rather than a query per user.
        """
        stmt = select(db.email_addresses.c.user_id, db.email_addresses.c.address).where(
            db.email_addresses.c.is_default.is_(True)
        )
        return {user_id: address for user_id, address in (await conn.execute(stmt)).all()}

    # ------------------------------------------------------------------
    # Projects and project-scoped entities
    # ------------------------------------------------------------------

    async def extract_projects(self) -> AsyncIterator[Project]:
        """Stream projects with their enabled modules, parents before children.

        Redmine keeps projects in a nested set, so ordering by ``lft`` yields a
        parent before any of its descendants and no topological sort is needed.
        Read in one pass rather than paged: an instance has hundreds of
        projects, not hundreds of thousands, and paging by key would break the
        ordering that makes the parent guarantee hold.
        """
        async with self._connection() as conn:
            modules: dict[int, list[str]] = {}
            for row in (await conn.execute(select(db.enabled_modules))).mappings().all():
                modules.setdefault(row["project_id"], []).append(row["name"])

            rows = (await conn.execute(select(db.projects).order_by(db.projects.c.lft))).mappings().all()

        for row in rows:
            project = extract.extract_project(dict(row))
            mapped, dropped = extract.map_modules(modules.get(row["id"], []))
            project.modules = mapped
            self._dropped_modules[project.source_ref] = dropped
            yield project

    async def extract_public_access(self) -> PublicAccess:
        """Read what non-members and anonymous visitors could see, translated.

        Reads the ``login_required`` setting, the roles, every project with its
        modules, and the project memberships held by the builtin
        ``GroupAnonymous`` / ``GroupNonMember`` principals, which override the
        builtin roles on that project. The translation is
        :func:`extract.build_public_access`.
        """
        async with self._connection() as conn:
            login_required = await db.fetch_setting(conn, "login_required")
            role_rows = [
                dict(row) for row in (await conn.execute(select(db.roles).order_by(db.roles.c.id))).mappings().all()
            ]
            p = db.projects.c
            project_columns = (p.id, p.identifier, p.is_public, p.status)
            project_rows = [dict(row) for row in (await conn.execute(select(*project_columns))).mappings().all()]
            modules: dict[int, list[str]] = {}
            for row in (await conn.execute(select(db.enabled_modules))).mappings().all():
                modules.setdefault(row["project_id"], []).append(row["name"])

            builtin_types = (extract.TYPE_GROUP_ANONYMOUS, extract.TYPE_GROUP_NON_MEMBER)
            override_rows = (
                await conn.execute(
                    select(db.members.c.project_id, db.users.c.type, db.member_roles.c.role_id)
                    .select_from(
                        db.members.join(db.users, db.users.c.id == db.members.c.user_id).join(
                            db.member_roles, db.member_roles.c.member_id == db.members.c.id
                        )
                    )
                    .where(db.users.c.type.in_(builtin_types))
                )
            ).all()

        overrides: dict[tuple[int, str], list[int]] = {}
        for project_id, principal_type, role_id in override_rows:
            overrides.setdefault((project_id, principal_type), []).append(role_id)

        return extract.build_public_access(
            login_required=login_required,
            roles=role_rows,
            projects=project_rows,
            modules=modules,
            overrides=overrides,
        )

    def dropped_modules(self, project_ref: str) -> list[str]:
        """Return the modules of *project_ref* that Specivo has no equivalent for."""
        return self._dropped_modules.get(project_ref, [])

    async def extract_versions(self, project_ref: str) -> AsyncIterator[Version]:
        """Stream a project's versions."""
        async with self._connection() as conn:
            stmt = select(db.versions).where(db.versions.c.project_id == int(project_ref))
            for row in (await conn.execute(stmt.order_by(db.versions.c.id))).mappings().all():
                yield extract.extract_version(dict(row))

    async def extract_categories(self, project_ref: str) -> AsyncIterator[Category]:
        """Stream a project's issue categories."""
        async with self._connection() as conn:
            stmt = select(db.issue_categories).where(db.issue_categories.c.project_id == int(project_ref))
            for row in (await conn.execute(stmt.order_by(db.issue_categories.c.id))).mappings().all():
                yield extract.extract_category(dict(row))

    async def extract_memberships(self, project_ref: str) -> AsyncIterator[Membership]:
        """Stream a project's memberships, for people and for groups.

        Only directly granted roles are emitted. Redmine materialises the grants
        a user inherits from a group as extra rows with ``inherited_from`` set,
        because that is how it answers "what can this person do here". Specivo
        answers the same question by unioning a user's own roles with those of
        the groups they belong to, so importing those rows would create a second
        copy of a grant the group already carries — one that would then outlive
        the person leaving the group.
        """
        async with self._connection() as conn:
            member_rows = (
                (
                    await conn.execute(
                        select(db.members).where(db.members.c.project_id == int(project_ref)).order_by(db.members.c.id)
                    )
                )
                .mappings()
                .all()
            )
            if not member_rows:
                return

            member_ids = [row["id"] for row in member_rows]
            role_rows = (
                (
                    await conn.execute(
                        select(db.member_roles).where(
                            db.member_roles.c.member_id.in_(member_ids),
                            db.member_roles.c.inherited_from.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
            roles_by_member: dict[int, list[str]] = {}
            for role_row in role_rows:
                roles_by_member.setdefault(role_row["member_id"], []).append(str(role_row["role_id"]))

            principal_ids = [row["user_id"] for row in member_rows]
            kinds = {
                user_id: principal_type
                for user_id, principal_type in (
                    await conn.execute(select(db.users.c.id, db.users.c.type).where(db.users.c.id.in_(principal_ids)))
                ).all()
            }

        for row in member_rows:
            principal_type = kinds.get(row["user_id"])
            kind: PrincipalKind
            if principal_type == extract.TYPE_USER:
                kind = "user"
            elif principal_type == extract.TYPE_GROUP:
                kind = "group"
            else:
                # The builtin non-member and anonymous group principals, whose
                # access Specivo expresses with a role and the project's
                # anonymous permissions rather than a membership. See
                # extract_public_access.
                continue

            roles = roles_by_member.get(row["id"], [])
            if not roles:
                continue
            yield extract.extract_membership(dict(row), roles, kind)

    async def extract_custom_fields(self) -> AsyncIterator[CustomField]:
        """Stream issue custom fields with their scope and choices.

        Fields defined on users, projects, versions, groups and time entries are
        skipped: Specivo's metadata schemas only target issues today. They are
        counted so the report can say what was dropped.
        """
        async with self._connection() as conn:
            rows = (
                (
                    await conn.execute(
                        select(db.custom_fields)
                        .where(db.custom_fields.c.type == extract.ISSUE_CUSTOM_FIELD_TYPE)
                        .order_by(db.custom_fields.c.id)
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                return

            field_ids = [row["id"] for row in rows]
            trackers_by_field: dict[int, list[str]] = {}
            for link in (
                (
                    await conn.execute(
                        select(db.custom_fields_trackers).where(
                            db.custom_fields_trackers.c.custom_field_id.in_(field_ids)
                        )
                    )
                )
                .mappings()
                .all()
            ):
                trackers_by_field.setdefault(link["custom_field_id"], []).append(str(link["tracker_id"]))

            projects_by_field: dict[int, list[str]] = {}
            for link in (
                (
                    await conn.execute(
                        select(db.custom_fields_projects).where(
                            db.custom_fields_projects.c.custom_field_id.in_(field_ids)
                        )
                    )
                )
                .mappings()
                .all()
            ):
                projects_by_field.setdefault(link["custom_field_id"], []).append(str(link["project_id"]))

            choices_by_field: dict[int, list[str]] = {}
            for choice in (
                (
                    await conn.execute(
                        select(db.custom_field_enumerations)
                        .where(db.custom_field_enumerations.c.custom_field_id.in_(field_ids))
                        .order_by(db.custom_field_enumerations.c.position)
                    )
                )
                .mappings()
                .all()
            ):
                choices_by_field.setdefault(choice["custom_field_id"], []).append(choice["name"])

            # A field marked required in Redmine may still have issues with no
            # value: it was made required later, or the value was cleared by an
            # import. Declaring it required in the schema would then reject the
            # very data being imported, so requiredness is only kept when every
            # existing value is filled in.
            blank_fields = set(
                (
                    await conn.execute(
                        select(db.custom_values.c.custom_field_id)
                        .where(
                            db.custom_values.c.custom_field_id.in_(field_ids),
                            db.custom_values.c.customized_type == "Issue",
                            or_(db.custom_values.c.value.is_(None), db.custom_values.c.value == ""),
                        )
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )

        for row in rows:
            data = dict(row)
            choices = choices_by_field.get(row["id"]) or extract.parse_possible_values(row.get("possible_values"))
            field = extract.extract_custom_field(
                data,
                tracker_refs=trackers_by_field.get(row["id"], []),
                project_refs=projects_by_field.get(row["id"], []),
                choices=choices,
                key=self._cf_key_overrides.get((row.get("name") or "").strip().lower()),
            )
            if field.is_required and row["id"] in blank_fields:
                field.is_required = False
                self.relaxed_required_fields.append(field.name)
            yield field

    async def count_non_issue_custom_fields(self) -> int:
        """Count custom fields on entities Specivo cannot attach metadata to."""
        async with self._connection() as conn:
            rows = (
                await conn.execute(
                    select(db.custom_fields.c.id).where(db.custom_fields.c.type != extract.ISSUE_CUSTOM_FIELD_TYPE)
                )
            ).all()
        return len(rows)

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    async def _custom_field_index(self) -> dict[int, CustomField]:
        """Return issue custom fields by id, read once and cached.

        Values are stored untyped, so turning one into what its schema declares
        needs the field definition.
        """
        if self._custom_fields is None:
            self._custom_fields = {int(field.source_ref): field async for field in self.extract_custom_fields()}
        return self._custom_fields

    async def issue_ids(self) -> frozenset[str]:
        """Return the ref of every issue in Redmine, whatever project it is in.

        Only ids are read, once per export, so a ``#N`` naming an issue outside
        the bundle can be told from a number that names no issue at all.
        """
        async with self._connection() as conn:
            ids = (await conn.execute(select(db.issues.c.id))).scalars().all()
        return frozenset(str(issue_id) for issue_id in ids)

    async def issue_order(self, project_ref: str) -> list[str]:
        """Return the refs of a project's issues, parents before children.

        Only the id and parent of every issue are read; the export pages
        through the rows themselves with :meth:`extract_issues`.
        """
        async with self._connection() as conn:
            pairs = (
                await conn.execute(
                    select(db.issues.c.id, db.issues.c.parent_id).where(db.issues.c.project_id == int(project_ref))
                )
            ).all()
        parents = {str(issue_id): (str(parent_id) if parent_id else None) for issue_id, parent_id in pairs}
        return extract.order_parents_first(parents)

    async def extract_issues(self, order: list[str]) -> AsyncIterator[Issue]:
        """Stream the issues named by *order*, in that order, a page at a time."""
        if not order:
            return
        fields = await self._custom_field_index()

        async with self._connection() as conn:
            for start in range(0, len(order), self._batch_size):
                chunk = order[start : start + self._batch_size]
                ids = [int(ref) for ref in chunk]

                rows = {
                    row["id"]: dict(row)
                    for row in (await conn.execute(select(db.issues).where(db.issues.c.id.in_(ids)))).mappings().all()
                }
                values = await self._custom_values_for(conn, ids, fields)

                for ref in chunk:
                    row = rows.get(int(ref))
                    if row is not None:
                        yield extract.extract_issue(row, values.get(int(ref), []))

    async def _custom_values_for(
        self,
        conn: Any,
        issue_ids: list[int],
        fields: dict[int, CustomField],
    ) -> dict[int, list[CustomValue]]:
        """Return custom-field values for a page of issues.

        A multi-value field is stored as one row per value, so those are
        collected into a list under a single key.
        """
        stmt = select(db.custom_values).where(
            db.custom_values.c.customized_type == "Issue",
            db.custom_values.c.customized_id.in_(issue_ids),
        )
        rows = (await conn.execute(stmt)).mappings().all()

        collected: dict[int, dict[int, list[Any]]] = {}
        for row in rows:
            field = fields.get(row["custom_field_id"])
            if field is None or row["value"] in (None, ""):
                continue
            value = extract.coerce_custom_value(row["value"], field.field_format, field.multiple)
            collected.setdefault(row["customized_id"], {}).setdefault(row["custom_field_id"], []).append(value)

        result: dict[int, list[CustomValue]] = {}
        for issue_id, by_field in collected.items():
            for field_id, values in by_field.items():
                field = fields[field_id]
                result.setdefault(issue_id, []).append(
                    CustomValue(
                        field_ref=field.source_ref,
                        key=field.key,
                        value=values if field.multiple else values[0],
                        value_kind=extract.value_kind_for(field.field_format),
                    )
                )
        return result

    async def extract_journals(self, issue_ids: list[int]) -> AsyncIterator[Journal]:
        """Stream the journals of *issue_ids* oldest first, with their field changes.

        Ordered by creation time so Specivo can number them the way it does,
        per issue and in the order they happened.
        """
        if not issue_ids:
            return
        async with self._connection() as conn:
            for start in range(0, len(issue_ids), self._batch_size):
                chunk = issue_ids[start : start + self._batch_size]
                rows = (
                    (
                        await conn.execute(
                            select(db.journals)
                            .where(
                                db.journals.c.journalized_type == "Issue",
                                db.journals.c.journalized_id.in_(chunk),
                            )
                            .order_by(db.journals.c.journalized_id, db.journals.c.created_on, db.journals.c.id)
                        )
                    )
                    .mappings()
                    .all()
                )
                if not rows:
                    continue

                journal_ids = [row["id"] for row in rows]
                details: dict[int, list[Any]] = {}
                for detail in (
                    (
                        await conn.execute(
                            select(db.journal_details)
                            .where(db.journal_details.c.journal_id.in_(journal_ids))
                            .order_by(db.journal_details.c.id)
                        )
                    )
                    .mappings()
                    .all()
                ):
                    details.setdefault(detail["journal_id"], []).append(extract.extract_journal_detail(dict(detail)))

                for row in rows:
                    yield extract.extract_journal(dict(row), details.get(row["id"], []))

    async def extract_relations(self, issue_ids: list[int]) -> AsyncIterator[Relation]:
        """Stream relations touching *issue_ids*.

        A relation crossing two projects appears under either of them; the
        export writes it once. A relation type Specivo has no equivalent for is
        not yielded but kept, by relation, in :attr:`unmapped_relation_types`.
        """
        if not issue_ids:
            return
        async with self._connection() as conn:
            for start in range(0, len(issue_ids), self._batch_size):
                chunk = issue_ids[start : start + self._batch_size]
                rows = (
                    (
                        await conn.execute(
                            select(db.issue_relations)
                            .where(
                                or_(
                                    db.issue_relations.c.issue_from_id.in_(chunk),
                                    db.issue_relations.c.issue_to_id.in_(chunk),
                                )
                            )
                            .order_by(db.issue_relations.c.id)
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    relation = extract.extract_relation(dict(row))
                    if relation is None:
                        self.unmapped_relation_types[str(row["id"])] = (row.get("relation_type") or "").strip()
                        continue
                    yield relation

    async def extract_watchers(self, issue_ids: list[int]) -> AsyncIterator[Watcher]:
        """Stream the watchers of *issue_ids*."""
        if not issue_ids:
            return
        async with self._connection() as conn:
            for start in range(0, len(issue_ids), self._batch_size):
                chunk = issue_ids[start : start + self._batch_size]
                rows = (
                    (
                        await conn.execute(
                            select(db.watchers)
                            .where(
                                db.watchers.c.watchable_type == "Issue",
                                db.watchers.c.watchable_id.in_(chunk),
                            )
                            .order_by(db.watchers.c.id)
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    yield extract.extract_watcher(dict(row), "issue")

    # ------------------------------------------------------------------
    # Wiki
    # ------------------------------------------------------------------

    async def extract_wiki_pages(self, project_ref: str) -> AsyncIterator[WikiPage]:
        """Stream a project's wiki pages with their history, parents first.

        History comes from ``wiki_content_versions`` alone. That table holds
        every revision including the current one, so reading the separate
        current-content table as well would duplicate the latest revision.
        """
        async with self._connection() as conn:
            wiki_id = (
                await conn.execute(select(db.wikis.c.id).where(db.wikis.c.project_id == int(project_ref)))
            ).scalar_one_or_none()
            if wiki_id is None:
                return

            page_rows = (
                (
                    await conn.execute(
                        select(db.wiki_pages).where(db.wiki_pages.c.wiki_id == wiki_id).order_by(db.wiki_pages.c.id)
                    )
                )
                .mappings()
                .all()
            )
            if not page_rows:
                return

            pages = {str(row["id"]): dict(row) for row in page_rows}
            parents = {ref: (str(row["parent_id"]) if row.get("parent_id") else None) for ref, row in pages.items()}

            order = extract.order_parents_first(parents)
            # Revisions are read for a few pages at a time: each one is a full
            # copy of its page, so a whole wiki's history can be large.
            for start in range(0, len(order), self.wiki_page_batch):
                batch = order[start : start + self.wiki_page_batch]
                versions: dict[int, list[WikiVersion]] = {}
                rows = (
                    await conn.execute(
                        select(db.wiki_content_versions)
                        .where(db.wiki_content_versions.c.page_id.in_([int(ref) for ref in batch]))
                        .order_by(db.wiki_content_versions.c.page_id, db.wiki_content_versions.c.version)
                    )
                ).mappings()
                for row in rows:
                    versions.setdefault(row["page_id"], []).append(extract.extract_wiki_version(dict(row)))
                for ref in batch:
                    page_row = pages[ref]
                    # wiki_pages hangs off the wiki, not the project, so the
                    # project is attached here for the record.
                    page_row["project_id"] = int(project_ref)
                    yield extract.extract_wiki_page(page_row, versions.pop(int(ref), []))

    async def extract_wiki_watchers(self, project_ref: str) -> AsyncIterator[Watcher]:
        """Stream the wiki-page watchers of a project."""
        async with self._connection() as conn:
            wiki_id = (
                await conn.execute(select(db.wikis.c.id).where(db.wikis.c.project_id == int(project_ref)))
            ).scalar_one_or_none()
            if wiki_id is None:
                return

            page_ids = list(
                (await conn.execute(select(db.wiki_pages.c.id).where(db.wiki_pages.c.wiki_id == wiki_id)))
                .scalars()
                .all()
            )
            if not page_ids:
                return

            rows = (
                (
                    await conn.execute(
                        select(db.watchers).where(
                            db.watchers.c.watchable_type == "WikiPage",
                            db.watchers.c.watchable_id.in_(page_ids),
                        )
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            yield extract.extract_watcher(dict(row), "wiki_page")

    async def extract_wiki_redirects(self, project_ref: str) -> AsyncIterator[WikiRedirect]:
        """Stream a project's wiki redirects.

        Redmine records one whenever a page is renamed and Specivo has the same
        table, so links to a page's old name keep working after the import.
        """
        async with self._connection() as conn:
            wiki_id = (
                await conn.execute(select(db.wikis.c.id).where(db.wikis.c.project_id == int(project_ref)))
            ).scalar_one_or_none()
            if wiki_id is None:
                return

            rows = (
                (await conn.execute(select(db.wiki_redirects).where(db.wiki_redirects.c.wiki_id == wiki_id)))
                .mappings()
                .all()
            )
        for row in rows:
            title, target = (row.get("title") or "").strip(), (row.get("redirects_to") or "").strip()
            if title and target:
                yield WikiRedirect(project_ref=project_ref, title=title, redirects_to=target)

    # ------------------------------------------------------------------
    # Time tracking
    # ------------------------------------------------------------------

    async def extract_time_entries(self, project_ref: str) -> AsyncIterator[TimeEntry]:
        """Stream a project's logged time."""
        async with self._connection() as conn:
            stmt = select(db.time_entries).where(db.time_entries.c.project_id == int(project_ref))
            async for row in db.stream(conn, stmt, db.time_entries.c.id, self._batch_size):
                entry = extract.extract_time_entry(row)
                if entry is None:
                    self.undated_time_entries.append(str(row["id"]))
                    continue
                yield entry

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    async def extract_attachments(
        self, project_ref: str, issue_ids: list[int]
    ) -> AsyncIterator[extract.SourceAttachment]:
        """Stream the files attached to *issue_ids* and to a project's wiki pages.

        Files hanging off anything else — a forum message, a document, a
        version — belong to features Specivo does not have, so they are counted
        and left behind rather than attached to something they did not belong
        to.
        """
        async with self._connection() as conn:
            wiki_page_ids = await self._wiki_page_ids(conn, project_ref)

            for container_type, ids in (("Issue", issue_ids), ("WikiPage", wiki_page_ids)):
                kind = extract.container_kind_for(container_type)
                if kind is None or not ids:
                    continue

                for start in range(0, len(ids), self._batch_size):
                    chunk = ids[start : start + self._batch_size]
                    rows = (
                        (
                            await conn.execute(
                                select(db.attachments)
                                .where(
                                    db.attachments.c.container_type == container_type,
                                    db.attachments.c.container_id.in_(chunk),
                                )
                                .order_by(db.attachments.c.id)
                            )
                        )
                        .mappings()
                        .all()
                    )
                    for row in rows:
                        yield extract.extract_attachment(dict(row), kind)

    async def count_unsupported_attachments(self) -> dict[str, int]:
        """Count attachments on containers Specivo cannot hold, by container."""
        async with self._connection() as conn:
            rows = (
                await conn.execute(
                    select(db.attachments.c.container_type, func.count())
                    .where(db.attachments.c.container_type.not_in(["Issue", "WikiPage"]))
                    .group_by(db.attachments.c.container_type)
                )
            ).all()
        return {container_type or "(none)": count for container_type, count in rows}

    async def _wiki_page_ids(self, conn: Any, project_ref: str) -> list[int]:
        """Return the ids of a project's wiki pages."""
        wiki_id = (
            await conn.execute(select(db.wikis.c.id).where(db.wikis.c.project_id == int(project_ref)))
        ).scalar_one_or_none()
        if wiki_id is None:
            return []
        stmt = select(db.wiki_pages.c.id).where(db.wiki_pages.c.wiki_id == wiki_id).order_by(db.wiki_pages.c.id)
        return [int(page_id) for page_id in (await conn.execute(stmt)).scalars().all()]
