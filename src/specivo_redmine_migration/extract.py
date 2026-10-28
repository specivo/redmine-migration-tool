"""Turn Redmine rows into bundle records.

Every function here is pure: a row dict in, a record of the migration SDK out. That keeps the
translation decisions — and there are a lot of them — testable against literal
dicts, with no Redmine instance and no database in the way.

The awkward cases are documented where they occur. The recurring ones:

* Redmine stores timestamps as naive UTC, and Specivo's columns are
  timezone-aware, so every timestamp is stamped UTC on the way through.
* Redmine's ``users`` table holds people, groups and the anonymous account,
  discriminated by ``type``.
* Several fields have no Specivo equivalent and are dropped deliberately rather
  than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

from specivo_migration_sdk import (
    Activity,
    Attachment,
    Category,
    Credential,
    CustomField,
    CustomValue,
    Group,
    Issue,
    Journal,
    JournalDetail,
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
    WikiVersion,
)
from specivo_migration_sdk.models import (
    AnonymousPermission,
    ContainerKind,
    DetailProperty,
    PrincipalKind,
    ProjectModule,
    ProjectStatus,
    RelationType,
    StatusCategory,
    TimeEntriesVisibility,
    UserStatus,
    UsersVisibility,
    ValueKind,
    VersionSharing,
    VersionStatus,
)

from specivo_redmine_migration.files import build_storage_key

# The visibility settings both systems share, and the narrower of each.
USERS_VISIBILITY_VALUES: tuple[UsersVisibility, ...] = ("all", "members_of_visible_projects")
USERS_VISIBILITY_MEMBERS: UsersVisibility = "members_of_visible_projects"
TIME_ENTRIES_VISIBILITY_VALUES: tuple[TimeEntriesVisibility, ...] = ("all", "own")
TIME_ENTRIES_VISIBILITY_OWN: TimeEntriesVisibility = "own"

# Order matters: ``trackers.fields_bits`` sets bit i when CORE_FIELDS[i] is
# disabled. Copied from Redmine's Tracker::CORE_FIELDS, where the comment
# reads "Other (future) fields should be appended, not inserted!".
REDMINE_CORE_FIELDS: tuple[str, ...] = (
    "assigned_to_id",
    "category_id",
    "fixed_version_id",
    "parent_issue_id",
    "start_date",
    "due_date",
    "estimated_hours",
    "done_ratio",
    "description",
    "priority_id",
)

# The one core field Specivo names differently.
_CORE_FIELD_RENAMES: dict[str, str] = {"parent_issue_id": "parent_id"}

# Redmine's Principal::STATUS_* constants.
USER_STATUS_ANONYMOUS = 0
USER_STATUS_ACTIVE = 1
USER_STATUS_REGISTERED = 2
USER_STATUS_LOCKED = 3

_USER_STATUS_MAP: dict[int, UserStatus] = {
    USER_STATUS_ACTIVE: "active",
    # Registered means signed up but not yet confirmed.
    USER_STATUS_REGISTERED: "pending_verification",
    USER_STATUS_LOCKED: "locked",
}

# Status-name hints used to place a status in Specivo's four-way grouping.
# Redmine only records whether a status closes an issue, so everything else is
# inference — the operator can override any of it per status name.
_BACKLOG_HINTS = frozenset({"new", "open", "backlog", "todo", "to do", "unconfirmed", "submitted"})
_DONE_HINTS = frozenset({"resolved", "done", "fixed", "completed", "complete", "verified", "ready for release"})

# Redmine's principal subclasses.
TYPE_USER = "User"
TYPE_GROUP = "Group"
TYPE_ANONYMOUS = "AnonymousUser"
# Redmine 4.1+ builtin group principals. A project membership held by one of
# them overrides the builtin Anonymous or Non member role on that project.
TYPE_GROUP_ANONYMOUS = "GroupAnonymous"
TYPE_GROUP_NON_MEMBER = "GroupNonMember"


class ZeroDateCounter:
    """Counts MySQL zero dates turned into empty values, for the export summary."""

    def __init__(self) -> None:
        self.count = 0

    def reset(self) -> None:
        self.count = 0


# MySQL's "no date": 0000-00-00, with or without a time. The driver hands it
# over as a string, since no date or datetime can hold it.
_ZERO_DATE_PREFIX = "0000-00-00"
zero_dates = ZeroDateCounter()


def _is_zero_date(value: object) -> bool:
    if isinstance(value, str) and value.strip().startswith(_ZERO_DATE_PREFIX):
        zero_dates.count += 1
        return True
    return False


def as_utc(value: datetime | str | None) -> datetime | None:
    """Stamp a naive Redmine timestamp as UTC.

    Rails writes UTC into ``timestamp without time zone``, so the value is
    already UTC and only the marker is missing. A value that somehow arrives
    with a zone is left alone. A MySQL zero date is ``None`` (and counted); a
    timestamp a driver left as text is parsed.
    """
    if value is None or _is_zero_date(value):
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip())
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def as_date(value: date | str | None) -> date | None:
    """Return a Redmine date, ``None`` for a MySQL zero date (counted), parsing one left as text."""
    if value is None or _is_zero_date(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value.strip()[:10])
    return value


def decode_disabled_core_fields(fields_bits: int | None) -> list[str]:
    """Return the core fields a tracker hides, from Redmine's bitmask.

    Bit *i* set means ``REDMINE_CORE_FIELDS[i]`` is disabled. Specivo stores the
    same idea as a list of names, so only the one differing name is translated.
    """
    bits = fields_bits or 0
    return [
        _CORE_FIELD_RENAMES.get(name, name) for index, name in enumerate(REDMINE_CORE_FIELDS) if bits & (1 << index)
    ]


def status_category(name: str, is_closed: bool, overrides: dict[str, str] | None = None) -> StatusCategory:
    """Place a Redmine status in Specivo's backlog/active/done/closed grouping.

    Redmine records only ``is_closed``, so the rest is inferred from the name.
    The inference is deliberately shallow — an operator override, matched on the
    status name case-insensitively, always wins, and Specivo's import report lists
    what was guessed so it can be corrected.
    """
    if overrides:
        override = overrides.get(name.strip().lower())
        if override:
            return cast(StatusCategory, override)

    if is_closed:
        return "closed"

    normalised = name.strip().lower()
    if normalised in _BACKLOG_HINTS:
        return "backlog"
    if normalised in _DONE_HINTS:
        return "done"
    return "active"


def map_user_status(status: int | None) -> UserStatus:
    """Map Redmine's numeric user status to Specivo's status string.

    Anything unrecognised becomes ``locked``: refusing sign-in to an account
    whose state we could not read is the safe direction to be wrong in.
    """
    return _USER_STATUS_MAP.get(status if status is not None else -1, "locked")


def normalise_language(language: str | None) -> str | None:
    """Return a bare language code, or ``None`` when Redmine has no preference.

    Redmine stores codes like ``en``, ``pt-BR`` and ``zh-TW``; Specivo's
    catalogs are per language. The region is dropped and Specivo decides
    whether the result is one it ships, falling back to the instance default.
    """
    if not language:
        return None
    return language.strip().lower().split("-")[0] or None


def display_name(row: dict[str, Any]) -> str:
    """Build a display name from a Redmine principal row.

    Groups carry their name in ``lastname`` and leave ``firstname`` empty, so
    joining the two parts covers both people and groups. An account with
    neither falls back to its login, since a blank name is worse than a login.
    """
    parts = [(row.get("firstname") or "").strip(), (row.get("lastname") or "").strip()]
    name = " ".join(part for part in parts if part)
    return name or (row.get("login") or "").strip() or f"user-{row['id']}"


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------


def extract_tracker(row: dict[str, Any]) -> Tracker:
    """Build a :class:`Tracker` from a ``trackers`` row.

    ``private_by_default`` maps straight onto Specivo's column of the same name.
    """
    return Tracker(
        source_ref=str(row["id"]),
        name=(row.get("name") or "").strip(),
        description=row.get("description") or None,
        is_in_roadmap=bool(row.get("is_in_roadmap")),
        default_status_ref=str(row["default_status_id"]) if row.get("default_status_id") else None,
        disabled_core_fields=decode_disabled_core_fields(row.get("fields_bits")),
        position=row.get("position") or 0,
        private_by_default=bool(row.get("private_by_default")),
    )


def extract_status(row: dict[str, Any], overrides: dict[str, str] | None = None) -> Status:
    """Build a :class:`Status`, resolving its Specivo category."""
    name = (row.get("name") or "").strip()
    return Status(
        source_ref=str(row["id"]),
        name=name,
        category=status_category(name, bool(row.get("is_closed")), overrides),
        position=row.get("position") or 0,
        default_done_ratio=row.get("default_done_ratio"),
    )


def extract_priority(row: dict[str, Any]) -> Priority:
    """Build a :class:`Priority` from an ``enumerations`` row."""
    return Priority(
        source_ref=str(row["id"]),
        name=(row.get("name") or "").strip(),
        position=row.get("position") or 0,
        is_default=bool(row.get("is_default")),
        active=bool(row.get("active")),
    )


def extract_activity(row: dict[str, Any]) -> Activity:
    """Build an :class:`Activity` from an ``enumerations`` row."""
    return Activity(
        source_ref=str(row["id"]),
        name=(row.get("name") or "").strip(),
        is_default=bool(row.get("is_default")),
        active=bool(row.get("active")),
    )


def extract_role(row: dict[str, Any]) -> Role:
    """Build a :class:`Role` from a ``roles`` row.

    Permissions are not carried over. Redmine stores them as serialised Ruby
    YAML naming Redmine's own permissions, and the two systems' permission
    vocabularies do not correspond one to one — translating them would be
    guesswork that quietly widens or narrows access. Specivo matches roles by
    name against the roles already in Specivo, and anything unmatched is created
    with no permissions and listed in its import report for an administrator to
    fill in.

    ``users_visibility`` and ``time_entries_visibility`` carry over: both
    systems use the same values (``all`` / ``members_of_visible_projects`` and
    ``all`` / ``own``). A value Specivo does not know becomes the narrower
    setting rather than the wider one.

    ``builtin`` carries over directly — both systems use 0 for an ordinary role,
    1 for the non-member role and 2 for the anonymous one. Specivo maps the
    non-member role onto Specivo's seeded one and creates no anonymous role;
    what those two roles allowed is translated separately, by
    :func:`build_public_access`.
    """
    return Role(
        source_ref=str(row["id"]),
        name=(row.get("name") or "").strip(),
        builtin=row.get("builtin") or 0,
        permissions=[],
        users_visibility=_visibility(
            row.get("users_visibility"), USERS_VISIBILITY_VALUES, narrowest=USERS_VISIBILITY_MEMBERS
        ),
        time_entries_visibility=_visibility(
            row.get("time_entries_visibility"), TIME_ENTRIES_VISIBILITY_VALUES, narrowest=TIME_ENTRIES_VISIBILITY_OWN
        ),
    )


def _visibility[V: str](value: Any, known: tuple[V, ...], *, narrowest: V) -> V | None:
    """Return *value* when Specivo knows it, None when absent, else *narrowest*."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    for candidate in known:
        if candidate == text:
            return candidate
    return narrowest


# --------------------------------------------------------------------------
# Non-member and anonymous access
# --------------------------------------------------------------------------

# Redmine builtin role kinds (``roles.builtin``).
BUILTIN_NON_MEMBER = 1
BUILTIN_ANONYMOUS = 2

# The only Redmine permissions that carry over to people without a
# membership, with the Specivo permission each becomes and the Redmine module
# that has to be enabled on the project for it to apply. Anonymous visitors in
# Specivo can read issues and wiki pages and nothing else, and the seeded Non
# member role is read-only, so every other permission is reported instead.
READ_PERMISSION_MAP: dict[str, tuple[AnonymousPermission, str]] = {
    "view_issues": ("view_issues", "issue_tracking"),
    "view_wiki_pages": ("view_wiki", "wiki"),
}

# Redmine project statuses under which anonymous visitors can read a project:
# STATUS_ACTIVE (1) and STATUS_CLOSED (5), where Redmine still allows read
# actions. Archived (9) grants nothing to anyone, members included, and
# scheduled for deletion (10) or any status Redmine adds later grants nothing
# here either: an allowlist fails closed.
_REDMINE_READABLE_STATUSES = frozenset({1, 5})

_PERMISSION_LINE_RE = re.compile(r"^\s*-\s*:?([A-Za-z0-9_]+)\s*$", re.MULTILINE)


def parse_role_permissions(raw: str | None) -> list[str]:
    """Return the permission names in a ``roles.permissions`` value.

    Redmine serialises the list as Ruby YAML, one symbol per line
    (``- :view_issues``). Read with a pattern rather than a YAML parser: the
    format is fixed, and a symbol is not something a safe YAML loader should
    be asked to interpret.
    """
    if not raw:
        return []
    seen: dict[str, None] = {}
    for name in _PERMISSION_LINE_RE.findall(raw):
        seen.setdefault(name, None)
    return list(seen)


def anonymous_login_allowed(login_required: str | None) -> bool:
    """Whether a Redmine instance served anonymous visitors.

    Only an explicit ``'0'`` counts. Redmine stores only settings that differ
    from its defaults, so an absent row cannot be told apart from one an
    administrator never looked at, and guessing in the permissive direction
    would publish a tracker. Anything but ``'0'`` is treated as "sign-in
    required".
    """
    return login_required is not None and login_required.strip() == "0"


def describe_login_required(login_required: str | None) -> str:
    """Return the report line saying what the source's sign-in setting was."""
    not_carried = "so no anonymous access was carried over"
    if login_required is None:
        return f"login_required is not set in the source; treated as sign-in required, {not_carried}"
    if anonymous_login_allowed(login_required):
        return "login_required = '0' in the source: anonymous visitors could read public projects"
    return f"login_required = {login_required.strip()!r} in the source: sign-in required, {not_carried}"


def readable_permissions(source_permissions: list[str], modules: list[str] | None = None) -> list[AnonymousPermission]:
    """Translate Redmine permissions into the read permissions Specivo carries over.

    With *modules*, a permission counts only when its module is enabled on the
    project, which is how Redmine itself decides. The result is sorted.
    """
    granted: set[AnonymousPermission] = set()
    for name in source_permissions:
        mapped = READ_PERMISSION_MAP.get(name)
        if mapped is None:
            continue
        specivo_name, module = mapped
        if modules is None or module in modules:
            granted.add(specivo_name)
    return sorted(granted)


def build_public_access(
    *,
    login_required: str | None,
    roles: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    modules: dict[int, list[str]],
    overrides: dict[tuple[int, str], list[int]],
) -> PublicAccess:
    """Translate Redmine's non-member and anonymous access into Specivo's model.

    *roles* are ``roles`` rows. *projects* are ``projects`` rows (``id``,
    ``identifier``, ``is_public``, ``status``) and *modules* their enabled
    module names. *overrides* maps ``(project_id, principal type)`` to the
    role ids a ``GroupAnonymous`` or ``GroupNonMember`` membership holds on
    that project.

    Anonymous access follows Redmine's own resolution: on a public project that
    is active or closed an anonymous visitor holds the roles of the project's
    ``GroupAnonymous`` membership if it has one, and the builtin Anonymous role
    otherwise. Of those roles' permissions only ``view_issues`` and
    ``view_wiki_pages`` carry over, each only when its module is enabled. None
    of it applies unless :func:`anonymous_login_allowed` says the source
    served anonymous visitors.

    The Non member role is translated the same way, without the per-project
    part: Specivo has one Non member role for the whole instance, so a
    ``GroupNonMember`` membership is reported rather than applied.
    """
    by_id = {int(row["id"]): row for row in roles}
    builtin: dict[int, dict[str, Any]] = {}
    for row in roles:
        kind = int(row.get("builtin") or 0)
        if kind in (BUILTIN_NON_MEMBER, BUILTIN_ANONYMOUS):
            builtin.setdefault(kind, row)

    def permissions_of(role_ids: list[int]) -> list[str]:
        merged: dict[str, None] = {}
        for role_id in role_ids:
            for name in parse_role_permissions((by_id.get(role_id) or {}).get("permissions")):
                merged.setdefault(name, None)
        return list(merged)

    def role_names(role_ids: list[int]) -> str:
        return ", ".join(sorted((by_id.get(r) or {}).get("name") or f"role {r}" for r in role_ids))

    allowed = anonymous_login_allowed(login_required)
    access = PublicAccess(anonymous_allowed=allowed, anonymous_source_setting=describe_login_required(login_required))
    not_carried: list[str] = []

    non_member = builtin.get(BUILTIN_NON_MEMBER)
    if non_member is not None:
        access.non_member_source_name = (non_member.get("name") or "").strip() or "Non member"
        source = parse_role_permissions(non_member.get("permissions"))
        access.non_member_permissions = [str(name) for name in readable_permissions(source)]
        dropped = sorted(name for name in source if name not in READ_PERMISSION_MAP)
        if dropped:
            not_carried.append(f"{access.non_member_source_name} role: {', '.join(dropped)}")

    anonymous = builtin.get(BUILTIN_ANONYMOUS)
    anonymous_default = parse_role_permissions(anonymous.get("permissions")) if anonymous is not None else []
    if anonymous is not None:
        dropped = sorted(name for name in anonymous_default if name not in READ_PERMISSION_MAP)
        if dropped:
            name = (anonymous.get("name") or "").strip() or "Anonymous"
            not_carried.append(f"{name} role: {', '.join(dropped)}")

    for project in projects:
        project_id = int(project["id"])
        identifier = (project.get("identifier") or "").strip().lower() or str(project_id)
        is_public = bool(project.get("is_public"))
        readable = project.get("status") in _REDMINE_READABLE_STATUSES

        non_member_roles = overrides.get((project_id, TYPE_GROUP_NON_MEMBER))
        if non_member_roles:
            not_carried.append(
                f"{identifier}: per-project Non member override ({role_names(non_member_roles)}): "
                f"{', '.join(sorted(permissions_of(non_member_roles))) or 'no permissions'}"
            )

        anonymous_roles = overrides.get((project_id, TYPE_GROUP_ANONYMOUS))
        if anonymous_roles:
            source = permissions_of(anonymous_roles)
            dropped = sorted(name for name in source if name not in READ_PERMISSION_MAP)
            if dropped:
                not_carried.append(
                    f"{identifier}: per-project Anonymous override ({role_names(anonymous_roles)}): "
                    f"{', '.join(dropped)}"
                )
        else:
            source = anonymous_default

        if not (allowed and is_public and readable):
            continue
        granted = readable_permissions(source, modules.get(project_id, []))
        if granted:
            access.anonymous_permissions[str(project_id)] = granted

    access.not_carried_over = not_carried
    return access


# --------------------------------------------------------------------------
# Principals
# --------------------------------------------------------------------------


# The algorithm id of Redmine's local password scheme, which Specivo's
# password hasher of the same name understands.
REDMINE_PASSWORD_ALGORITHM = "redmine_sha1"


def extract_credential(row: dict[str, Any]) -> Credential | None:
    """Return the account's Redmine credential, or ``None`` if it has none.

    The raw columns only — the tool does no cryptography. ``None`` for an
    account bound to an external auth source (LDAP), whose local hash Redmine
    never checks, and for one with a blank salt or hash.
    """
    if row.get("auth_source_id") is not None:
        return None
    hashed = (row.get("hashed_password") or "").strip()
    salt = (row.get("salt") or "").strip()
    if not hashed or not salt:
        return None
    return Credential(algorithm=REDMINE_PASSWORD_ALGORITHM, fields={"salt": salt, "hashed_password": hashed})


def extract_user(row: dict[str, Any], email: str | None = None) -> User:
    """Build a :class:`User` from a ``users`` row and its default address.

    The credential is carried as Redmine's raw salt and hash (see
    :func:`extract_credential`). The export writes it only with
    ``--import-passwords``, and Specivo uses it only when its import is run
    with the same option and the algorithm enabled, and never for an
    administrator.
    """
    return User(
        source_ref=str(row["id"]),
        login=(row.get("login") or "").strip(),
        display_name=display_name(row),
        email=(email or "").strip() or None,
        status=map_user_status(row.get("status")),
        is_admin=bool(row.get("admin")),
        language=normalise_language(row.get("language")),
        last_login_at=as_utc(row.get("last_login_on")),
        created_at=as_utc(row.get("created_on")),
        updated_at=as_utc(row.get("updated_on")),
        credential=extract_credential(row),
        must_change_password=bool(row.get("must_change_passwd")),
        password_changed_at=as_utc(row.get("passwd_changed_on")),
        external_auth=row.get("auth_source_id") is not None,
    )


def extract_group(row: dict[str, Any], member_refs: list[str]) -> Group:
    """Build a :class:`Group` from a ``users`` row of type ``Group``."""
    return Group(
        source_ref=str(row["id"]),
        name=display_name(row),
        member_refs=list(member_refs),
    )


# --------------------------------------------------------------------------
# Projects and project-scoped entities
# --------------------------------------------------------------------------

# Redmine and Specivo happen to use the same project status encoding. Mapped
# explicitly anyway: relying on two systems agreeing by coincidence is how a
# closed project quietly comes back to life.
_PROJECT_STATUS_MAP: dict[int, ProjectStatus] = {1: 1, 5: 5, 9: 9}

# Redmine modules that have a Specivo equivalent. Everything else — repository,
# boards, calendar, gantt, news, documents — belongs to a feature Specivo does
# not have, so it is dropped and counted rather than mapped onto something else.
MODULE_MAP: dict[str, ProjectModule] = {
    "issue_tracking": "issue_tracking",
    "wiki": "wiki",
    "time_tracking": "time_tracking",
}


def extract_project(row: dict[str, Any]) -> Project:
    """Build a :class:`Project` from a ``projects`` row.

    ``homepage`` and the various default_* columns are dropped: Specivo has no
    equivalent for them.
    """
    return Project(
        source_ref=str(row["id"]),
        identifier=(row.get("identifier") or "").strip().lower(),
        name=(row.get("name") or "").strip(),
        description=row.get("description") or None,
        is_public=bool(row.get("is_public")),
        parent_ref=str(row["parent_id"]) if row.get("parent_id") else None,
        status=_PROJECT_STATUS_MAP.get(row.get("status") or 1, 1),
        modules=[],
        created_at=as_utc(row.get("created_on")),
        updated_at=as_utc(row.get("updated_on")),
    )


def map_modules(names: list[str]) -> tuple[list[ProjectModule], list[str]]:
    """Split Redmine module names into ones Specivo has and ones it does not."""
    mapped = [MODULE_MAP[name] for name in names if name in MODULE_MAP]
    dropped = [name for name in names if name not in MODULE_MAP]
    return sorted(set(mapped)), sorted(set(dropped))


def extract_version(row: dict[str, Any]) -> Version:
    """Build a :class:`Version`.

    ``status`` and ``sharing`` use the same vocabulary in both systems, so they
    carry across unchanged.
    """
    return Version(
        source_ref=str(row["id"]),
        project_ref=str(row["project_id"]),
        name=(row.get("name") or "").strip(),
        description=row.get("description") or None,
        status=cast(VersionStatus, (row.get("status") or "open").strip()),
        effective_date=as_date(row.get("effective_date")),
        sharing=cast(VersionSharing, (row.get("sharing") or "none").strip()),
        wiki_page_title=row.get("wiki_page_title") or None,
        created_at=as_utc(row.get("created_on")),
        updated_at=as_utc(row.get("updated_on")),
    )


def extract_category(row: dict[str, Any]) -> Category:
    """Build a :class:`Category` from an ``issue_categories`` row."""
    return Category(
        source_ref=str(row["id"]),
        project_ref=str(row["project_id"]),
        name=(row.get("name") or "").strip(),
        assigned_to_ref=str(row["assigned_to_id"]) if row.get("assigned_to_id") else None,
    )


def extract_membership(
    row: dict[str, Any],
    role_refs: list[str],
    principal_kind: PrincipalKind,
) -> Membership:
    """Build a :class:`Membership` from a ``members`` row.

    Redmine keeps people and groups in one table, so the caller decides which
    kind this membership belongs to.
    """
    return Membership(
        source_ref=str(row["id"]),
        project_ref=str(row["project_id"]),
        principal_ref=str(row["user_id"]),
        principal_kind=principal_kind,
        role_refs=list(role_refs),
    )


# --------------------------------------------------------------------------
# Custom fields
# --------------------------------------------------------------------------

# Redmine's custom_fields.type discriminator. Only issue fields have a target.
ISSUE_CUSTOM_FIELD_TYPE = "IssueCustomField"

# One YAML list item, as Redmine serialises possible_values.
_YAML_ITEM_RE = re.compile(r"^\s*-\s+(.*?)\s*$")


def parse_possible_values(raw: str | None) -> list[str]:
    """Return the choices of a list-format custom field.

    Redmine stores them as a serialised Ruby YAML array. Parsed by hand rather
    than with a YAML library: the payload is a flat list of strings, and a full
    parser would be a dependency and an attack surface for the sake of one
    column.
    """
    if not raw:
        return []
    values: list[str] = []
    for line in raw.splitlines():
        if line.strip() in {"---", ""}:
            continue
        match = _YAML_ITEM_RE.match(line)
        if match:
            values.append(_strip_quotes(match.group(1)))
        else:
            # Very old instances stored a plain newline-separated list.
            values.append(_strip_quotes(line.strip()))
    return [value for value in values if value]


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def metadata_key(name: str) -> str:
    """Turn a custom field name into a JSON key.

    Snake case, matching how the rest of Specivo's JSONB keys are written. A
    name with no usable characters — one written entirely in a non-Latin script,
    say — has no sensible key, so the caller is expected to supply one.
    """
    key = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return key


def json_schema_for(row: dict[str, Any], choices: list[str] | None = None) -> dict[str, Any]:
    """Translate a Redmine field format into a JSON Schema fragment.

    ``user`` and ``version`` fields hold a reference. They become integers
    because the value stored is a Specivo id, resolved once the referenced
    entity has been imported.

    An unrecognised format falls back to a string, which stores the value
    faithfully even when its shape is not understood.
    """
    field_format = (row.get("field_format") or "string").strip()
    multiple = bool(row.get("multiple"))

    if field_format in {"list", "enumeration"}:
        item: dict[str, Any] = {"type": "string"}
        if choices:
            item["enum"] = list(choices)
        return {"type": "array", "items": item} if multiple else item

    if field_format in {"user", "version"}:
        item = {"type": "integer"}
        return {"type": "array", "items": item} if multiple else item

    simple: dict[str, dict[str, Any]] = {
        "int": {"type": "integer"},
        "float": {"type": "number"},
        "bool": {"type": "boolean"},
        "date": {"type": "string", "format": "date"},
        "link": {"type": "string", "format": "uri"},
        "text": {"type": "string"},
        "string": {"type": "string"},
    }
    schema = dict(simple.get(field_format, {"type": "string"}))

    if field_format in {"string", "text", "link"}:
        if row.get("min_length"):
            schema["minLength"] = row["min_length"]
        if row.get("max_length"):
            schema["maxLength"] = row["max_length"]

    return {"type": "array", "items": schema} if multiple else schema


def extract_custom_field(
    row: dict[str, Any],
    tracker_refs: list[str],
    project_refs: list[str],
    choices: list[str] | None = None,
    key: str | None = None,
) -> CustomField:
    """Build a :class:`CustomField` with its JSON Schema fragment."""
    name = (row.get("name") or "").strip()
    return CustomField(
        source_ref=str(row["id"]),
        name=name,
        key=key or metadata_key(name) or f"field_{row['id']}",
        field_format=(row.get("field_format") or "string").strip(),
        json_schema=json_schema_for(row, choices),
        is_required=bool(row.get("is_required")),
        multiple=bool(row.get("multiple")),
        is_for_all=bool(row.get("is_for_all")),
        tracker_refs=list(tracker_refs),
        project_refs=list(project_refs),
    )


# --------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------

# Redmine names both directions of a relation; Specivo stores one canonical
# form and derives the reverse when reading. A reverse name therefore becomes
# its canonical form with the endpoints swapped.
_RELATION_CANONICAL: dict[str, tuple[RelationType, bool]] = {
    "relates": ("relates", False),
    "duplicates": ("duplicates", False),
    "duplicated": ("duplicates", True),
    "blocks": ("blocks", False),
    "blocked": ("blocks", True),
    "precedes": ("precedes", False),
    "follows": ("precedes", True),
    "copied_to": ("copied_to", False),
    "copied_from": ("copied_to", True),
}

# Redmine field formats whose stored value is a reference to something else.
_REFERENCE_FORMATS: dict[str, ValueKind] = {
    "user": "user_ref",
    "version": "version_ref",
}


def order_parents_first(parents: dict[str, str | None]) -> list[str]:
    """Order issue references so a parent always precedes its children.

    Redmine issue ids give no such guarantee — a subtask can be older than the
    issue it was later attached to — and Specivo's nested set has to be built
    top down.

    A reference whose parent is not in *parents* is treated as a root: its
    parent lives in another project or outside the selected scope, so it cannot
    be waited for. Anything left over after the sweep is part of a cycle, which
    Redmine should not allow; it is returned at the end rather than dropped, so
    Specivo can report issues that exist instead of silently losing
    them.
    """
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for ref, parent in parents.items():
        if parent is None or parent not in parents:
            roots.append(ref)
        else:
            children.setdefault(parent, []).append(ref)

    ordered: list[str] = []
    queue = list(roots)
    while queue:
        ref = queue.pop(0)
        ordered.append(ref)
        queue.extend(children.get(ref, []))

    if len(ordered) < len(parents):
        placed = set(ordered)
        ordered.extend(ref for ref in parents if ref not in placed)
    return ordered


def coerce_custom_value(raw: str | None, field_format: str, multiple: bool) -> Any:
    """Turn a stored custom-field value into the type its schema declares.

    Redmine keeps every value as text, so a field declared as an integer still
    arrives as ``"3"``. A value that cannot be converted is kept as text: the
    schema will reject it, which is better than discarding what the source held.
    """
    if raw is None or raw == "":
        return [] if multiple else None

    if field_format == "int":
        return int(raw) if raw.lstrip("-").isdigit() else raw
    if field_format == "float":
        try:
            return float(raw)
        except ValueError:
            return raw
    if field_format == "bool":
        return raw in {"1", "true", "t", "yes"}
    if field_format in _REFERENCE_FORMATS:
        return int(raw) if raw.isdigit() else raw
    return raw


def value_kind_for(field_format: str) -> ValueKind:
    """Return how a value of this format has to be resolved."""
    return _REFERENCE_FORMATS.get(field_format, "scalar")


def extract_issue(row: dict[str, Any], custom_values: list[CustomValue] | None = None) -> Issue:
    """Build an :class:`Issue` from an ``issues`` row.

    ``description`` is left as the source wrote it; the caller converts markup.
    The nested-set columns are deliberately not carried: Specivo rebuilds its
    own tree as issues are created, and two systems' nested sets never line up.
    """
    estimated = row.get("estimated_hours")
    return Issue(
        source_ref=str(row["id"]),
        project_ref=str(row["project_id"]),
        tracker_ref=str(row["tracker_id"]),
        status_ref=str(row["status_id"]),
        priority_ref=str(row["priority_id"]),
        subject=(row.get("subject") or "").strip(),
        author_ref=str(row["author_id"]) if row.get("author_id") else None,
        assigned_to_ref=str(row["assigned_to_id"]) if row.get("assigned_to_id") else None,
        description=row.get("description") or None,
        parent_ref=str(row["parent_id"]) if row.get("parent_id") else None,
        category_ref=str(row["category_id"]) if row.get("category_id") else None,
        fixed_version_ref=str(row["fixed_version_id"]) if row.get("fixed_version_id") else None,
        start_date=as_date(row.get("start_date")),
        due_date=as_date(row.get("due_date")),
        estimated_hours=Decimal(str(estimated)) if estimated is not None else None,
        done_ratio=row.get("done_ratio") or 0,
        is_private=bool(row.get("is_private")),
        custom_values=list(custom_values or []),
        created_at=as_utc(row.get("created_on")),
        updated_at=as_utc(row.get("updated_on")),
        closed_at=as_utc(row.get("closed_on")),
    )


def extract_journal(row: dict[str, Any], details: list[JournalDetail]) -> Journal:
    """Build a :class:`Journal` from a ``journals`` row.

    The sequence number is not carried: Specivo numbers journals per issue and
    Specivo assigns them in chronological order.
    """
    return Journal(
        source_ref=str(row["id"]),
        issue_ref=str(row["journalized_id"]),
        user_ref=str(row["user_id"]) if row.get("user_id") else None,
        notes=row.get("notes") or None,
        is_private=bool(row.get("private_notes")),
        created_at=as_utc(row.get("created_on")),
        details=list(details),
    )


def extract_journal_detail(row: dict[str, Any]) -> JournalDetail:
    """Build a :class:`JournalDetail`.

    Both systems use the same ``property`` vocabulary and the same attribute
    names, so only the column holding the new value is renamed.
    """
    return JournalDetail(
        property=cast(DetailProperty, (row.get("property") or "attr").strip()),
        prop_key=(row.get("prop_key") or "").strip(),
        old_value=row.get("old_value"),
        new_value=row.get("value"),
    )


def extract_relation(row: dict[str, Any]) -> Relation | None:
    """Build a :class:`Relation`, normalised to Specivo's canonical form.

    Returns ``None`` for a relation type Specivo has no equivalent for, so the
    caller can count it rather than storing something that means the wrong
    thing.
    """
    raw_type = (row.get("relation_type") or "").strip()
    canonical = _RELATION_CANONICAL.get(raw_type)
    if canonical is None:
        return None

    relation_type, swap = canonical
    from_ref, to_ref = str(row["issue_from_id"]), str(row["issue_to_id"])
    if swap:
        from_ref, to_ref = to_ref, from_ref

    return Relation(
        source_ref=str(row["id"]),
        from_ref=from_ref,
        to_ref=to_ref,
        relation_type=relation_type,
        delay=row.get("delay"),
    )


def extract_watcher(row: dict[str, Any], container_kind: ContainerKind) -> Watcher:
    """Build a :class:`Watcher` from a ``watchers`` row."""
    return Watcher(
        container_kind=container_kind,
        container_ref=str(row["watchable_id"]),
        user_ref=str(row["user_id"]),
    )


# --------------------------------------------------------------------------
# Wiki and time tracking
# --------------------------------------------------------------------------


def decode_wiki_text(data: bytes | str | None, compression: str | None) -> str:
    """Return the text of a wiki revision.

    Redmine stores revision bodies as bytes and may gzip them, which the
    ``compression`` column records. A body that cannot be decoded comes back as
    replacement characters rather than raising: a garbled revision is worth
    keeping, and losing the page's history is not.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data

    payload = data
    if (compression or "").strip().lower() == "gzip":
        import gzip
        import zlib

        try:
            payload = gzip.decompress(data)
        except (OSError, zlib.error):
            return data.decode("utf-8", errors="replace")

    return payload.decode("utf-8", errors="replace")


def extract_wiki_version(row: dict[str, Any]) -> WikiVersion:
    """Build a :class:`WikiVersion` from a ``wiki_content_versions`` row."""
    return WikiVersion(
        source_ref=str(row["id"]),
        version=row.get("version") or 1,
        text=decode_wiki_text(row.get("data"), row.get("compression")),
        comments=row.get("comments") or None,
        author_ref=str(row["author_id"]) if row.get("author_id") else None,
        created_at=as_utc(row.get("updated_on")),
    )


def extract_wiki_page(row: dict[str, Any], versions: list[WikiVersion]) -> WikiPage:
    """Build a :class:`WikiPage` with its history, oldest revision first.

    The slug is not carried: Specivo derives its own from the title, and its
    rules differ from Redmine's.
    """
    return WikiPage(
        source_ref=str(row["id"]),
        project_ref=str(row["project_id"]),
        title=(row.get("title") or "").strip(),
        parent_ref=str(row["parent_id"]) if row.get("parent_id") else None,
        protected=bool(row.get("protected")),
        versions=sorted(versions, key=lambda version: version.version),
    )


def extract_time_entry(row: dict[str, Any]) -> TimeEntry | None:
    """Build a :class:`TimeEntry` from a ``time_entries`` row.

    An entry whose day is a MySQL zero date is put on the day it was logged;
    ``None`` when that is unknown too, since an entry needs a day.

    Redmine records both whose time it is and who logged it; Specivo keeps only
    the former, so ``author_id`` is dropped.

    Hours are converted from a float to a Decimal through its string form,
    which is what keeps 7.5 from becoming 7.499999999999999.
    """
    hours = row.get("hours")
    created_at = as_utc(row.get("created_on"))
    spent_on = as_date(row.get("spent_on")) or (created_at.date() if created_at else None)
    if spent_on is None:
        return None
    return TimeEntry(
        source_ref=str(row["id"]),
        project_ref=str(row["project_id"]),
        hours=Decimal(str(hours if hours is not None else 0)),
        spent_on=spent_on,
        issue_ref=str(row["issue_id"]) if row.get("issue_id") else None,
        user_ref=str(row["user_id"]) if row.get("user_id") else None,
        activity_ref=str(row["activity_id"]) if row.get("activity_id") else None,
        comments=row.get("comments") or None,
        created_at=created_at,
        updated_at=as_utc(row.get("updated_on")),
    )


# Attachment containers Specivo can hold. Redmine also attaches files to
# forum messages, documents, versions and projects, all of which belong to
# features Specivo does not have.
_CONTAINER_KINDS: dict[str, ContainerKind] = {
    "Issue": "issue",
    "WikiPage": "wiki_page",
}


def container_kind_for(container_type: str) -> ContainerKind | None:
    """Return the Specivo container for a Redmine container type, if any."""
    return _CONTAINER_KINDS.get((container_type or "").strip())


@dataclass(kw_only=True, slots=True)
class SourceAttachment:
    """An attachment as Redmine records it, before its bytes are stored.

    ``storage_key`` locates the file under Redmine's files directory (see
    :mod:`specivo_redmine_migration.files`). The bundle names a file by the
    SHA-256 of its bytes instead, so :meth:`record` takes the stored hash.
    """

    source_ref: str
    container_kind: ContainerKind
    container_ref: str
    filename: str
    storage_key: str
    content_type: str | None = None
    filesize: int | None = None
    description: str | None = None
    author_ref: str | None = None
    created_at: datetime | None = None

    def record(self, sha256: str, size: int) -> Attachment:
        """Return the bundle record for these bytes."""
        return Attachment(
            source_ref=self.source_ref,
            container_kind=self.container_kind,
            container_ref=self.container_ref,
            filename=self.filename,
            sha256=sha256,
            content_type=self.content_type,
            filesize=size,
            description=self.description,
            author_ref=self.author_ref,
            created_at=self.created_at,
        )


def extract_attachment(row: dict[str, Any], container_kind: ContainerKind) -> SourceAttachment:
    """Build a :class:`SourceAttachment` from an ``attachments`` row.

    Redmine's own ``digest`` is not carried. Its algorithm has changed across
    versions and is configurable, and the point of a hash here is to describe
    the bytes that actually arrived, so it is recomputed on copy, and the size
    written is the size of the bytes copied.
    """
    return SourceAttachment(
        source_ref=str(row["id"]),
        container_kind=container_kind,
        container_ref=str(row["container_id"]),
        filename=(row.get("filename") or "").strip(),
        storage_key=build_storage_key(row.get("disk_directory"), row.get("disk_filename") or ""),
        content_type=(row.get("content_type") or "").strip() or None,
        filesize=row.get("filesize"),
        description=row.get("description") or None,
        author_ref=str(row["author_id"]) if row.get("author_id") else None,
        created_at=as_utc(row.get("created_on")),
    )
