"""A small Redmine database in SQLite, for tests that run the whole export.

The tables are the tool's own declarations (``specivo_redmine_migration.db``),
which name every column the export reads, so the rows below exercise the real
queries without a Redmine instance. The values are shaped like a Redmine 7.0.1
database: naive UTC timestamps, ``type`` discriminators on ``users`` and
``enumerations``, Ruby YAML in ``roles.permissions``, gzip-compressed wiki
revisions, and attachments stored under ``YYYY/MM`` as well as flat.

The end-to-end test against a real Redmine is ``test_e2e_fixture.py``.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, insert

from specivo_redmine_migration import db

T0 = datetime(2026, 1, 5, 9, 0, 0)
T1 = datetime(2026, 1, 6, 10, 30, 0)
T2 = datetime(2026, 1, 7, 14, 15, 0)

ADMIN_SALT = "0123456789abcdef0123456789abcdef"
ADMIN_HASH = "a" * 40
DEV_SALT = "fedcba9876543210fedcba9876543210"
DEV_HASH = "b" * 40

LOG_BYTES = b"session expired\nrenew failed\n"
NOTES_BYTES = b"Architecture notes.\n"


@dataclass(frozen=True)
class SampleRedmine:
    """Where the sample lives: its database URL and its files directory."""

    url: str
    files_dir: Path


def build(
    tmp_path: Path, *, host_name: str | None = "tracker.example.com", text_formatting: str = "textile"
) -> SampleRedmine:
    """Write the sample database and its files under *tmp_path*."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "redmine.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    db.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, rows in _rows(host_name, text_formatting).items():
            for row in rows:
                # One at a time: rows leave out the columns they do not set.
                conn.execute(insert(db.metadata.tables[table]).values(**row))
    engine.dispose()

    files_dir = tmp_path / "files"
    (files_dir / "2026" / "01").mkdir(parents=True)
    (files_dir / "2026" / "01" / "260105090000_server.log").write_bytes(LOG_BYTES)
    # An older file, stored before Redmine moved to dated directories.
    (files_dir / "1234_notes.txt").write_bytes(NOTES_BYTES)
    return SampleRedmine(url=f"sqlite+aiosqlite:///{path}", files_dir=files_dir)


def _rows(host_name: str | None, text_formatting: str) -> dict[str, list[dict[str, Any]]]:
    settings = [
        {"id": 1, "name": "text_formatting", "value": text_formatting},
        {"id": 2, "name": "login_required", "value": "0"},
    ]
    if host_name is not None:
        settings.append({"id": 3, "name": "host_name", "value": host_name})

    user = {
        "admin": False,
        "status": 1,
        "language": "en",
        "created_on": T0,
        "updated_on": T0,
        "must_change_passwd": False,
        "hashed_password": None,
        "salt": None,
        "auth_source_id": None,
    }
    return {
        "settings": settings,
        "trackers": [
            {"id": 1, "name": "Bug", "position": 1, "is_in_roadmap": False, "fields_bits": 0, "default_status_id": 1},
            {
                "id": 2,
                "name": "Feature",
                "position": 2,
                "is_in_roadmap": True,
                "fields_bits": 2,
                "default_status_id": 1,
            },
        ],
        "issue_statuses": [
            {"id": 1, "name": "New", "is_closed": False, "position": 1},
            {"id": 2, "name": "In Progress", "is_closed": False, "position": 2},
            {"id": 5, "name": "Closed", "is_closed": True, "position": 5},
        ],
        "enumerations": [
            {"id": 10, "name": "Normal", "position": 1, "is_default": True, "type": "IssuePriority", "active": True},
            {"id": 11, "name": "High", "position": 2, "is_default": False, "type": "IssuePriority", "active": True},
            {
                "id": 12,
                "name": "Development",
                "position": 1,
                "is_default": True,
                "type": "TimeEntryActivity",
                "active": True,
            },
            # A per-project override, which has no Specivo equivalent.
            {
                "id": 13,
                "name": "Development",
                "position": 1,
                "is_default": False,
                "type": "TimeEntryActivity",
                "active": True,
                "project_id": 1,
                "parent_id": 12,
            },
        ],
        "roles": [
            {
                "id": 1,
                "name": "Non member",
                "position": 0,
                "builtin": 1,
                "permissions": "---\n- :view_issues\n- :add_issues\n",
            },
            {
                "id": 2,
                "name": "Anonymous",
                "position": 0,
                "builtin": 2,
                "permissions": "---\n- :view_issues\n- :view_wiki_pages\n",
            },
            {
                "id": 3,
                "name": "Manager",
                "position": 1,
                "builtin": 0,
                "permissions": "---\n- :add_issues\n",
                "users_visibility": "all",
                "time_entries_visibility": "all",
            },
        ],
        "users": [
            {
                **user,
                "id": 1,
                "login": "admin",
                "firstname": "Sample",
                "lastname": "Admin",
                "admin": True,
                "type": "User",
                "hashed_password": ADMIN_HASH,
                "salt": ADMIN_SALT,
                "last_login_on": T2,
            },
            {
                **user,
                "id": 2,
                "login": "dev",
                "firstname": "Dev",
                "lastname": "Account",
                "type": "User",
                "language": "pt-BR",
                "hashed_password": DEV_HASH,
                "salt": DEV_SALT,
            },
            {**user, "id": 3, "login": "", "firstname": "", "lastname": "Platform Team", "type": "Group"},
            {
                **user,
                "id": 4,
                "login": "",
                "firstname": "",
                "lastname": "Anonymous",
                "type": "AnonymousUser",
                "status": 0,
            },
            {**user, "id": 5, "login": "", "firstname": "", "lastname": "Anonymous users", "type": "GroupAnonymous"},
            {**user, "id": 6, "login": "", "firstname": "", "lastname": "Non member users", "type": "GroupNonMember"},
        ],
        "email_addresses": [
            {"id": 1, "user_id": 1, "address": "admin@example.com", "is_default": True, "notify": True},
            {"id": 2, "user_id": 2, "address": "dev@example.com", "is_default": True, "notify": True},
            {"id": 3, "user_id": 2, "address": "dev-alt@example.com", "is_default": False, "notify": False},
        ],
        "groups_users": [{"group_id": 3, "user_id": 2}],
        "projects": [
            {
                "id": 1,
                "name": "Acme App",
                "identifier": "acme-app",
                "description": "Main product.",
                "is_public": True,
                "status": 1,
                "lft": 1,
                "rgt": 4,
                "created_on": T0,
                "updated_on": T1,
            },
            {
                "id": 2,
                "name": "Acme Mobile",
                "identifier": "acme-mobile",
                "is_public": True,
                "status": 1,
                "parent_id": 1,
                "lft": 2,
                "rgt": 3,
                "created_on": T0,
                "updated_on": T0,
            },
            {
                "id": 3,
                "name": "Acme Ops",
                "identifier": "acme-ops",
                "is_public": False,
                "status": 1,
                "lft": 5,
                "rgt": 6,
                "created_on": T0,
                "updated_on": T0,
            },
        ],
        "enabled_modules": [
            {"id": 1, "project_id": 1, "name": "issue_tracking"},
            {"id": 2, "project_id": 1, "name": "wiki"},
            {"id": 3, "project_id": 1, "name": "time_tracking"},
            {"id": 4, "project_id": 1, "name": "repository"},
            {"id": 5, "project_id": 2, "name": "issue_tracking"},
            {"id": 6, "project_id": 3, "name": "issue_tracking"},
        ],
        "members": [
            {"id": 1, "user_id": 1, "project_id": 1, "created_on": T0},
            {"id": 2, "user_id": 3, "project_id": 1, "created_on": T0},
            # Holds only the role inherited from the group, which is not imported twice.
            {"id": 3, "user_id": 2, "project_id": 1, "created_on": T0},
            {"id": 4, "user_id": 5, "project_id": 2, "created_on": T0},
            {"id": 5, "user_id": 2, "project_id": 2, "created_on": T0},
        ],
        "member_roles": [
            {"id": 1, "member_id": 1, "role_id": 3},
            {"id": 2, "member_id": 2, "role_id": 3},
            {"id": 3, "member_id": 3, "role_id": 3, "inherited_from": 2},
            {"id": 4, "member_id": 4, "role_id": 1},
            {"id": 5, "member_id": 5, "role_id": 3},
        ],
        "versions": [
            {
                "id": 1,
                "project_id": 1,
                "name": "1.0",
                "description": "First release",
                "status": "locked",
                "sharing": "descendants",
                "effective_date": date(2026, 3, 1),
                "created_on": T0,
                "updated_on": T1,
            },
        ],
        "issue_categories": [{"id": 1, "project_id": 1, "name": "Backend", "assigned_to_id": 2}],
        "custom_fields": [
            {
                "id": 1,
                "type": "IssueCustomField",
                "name": "Severity",
                "field_format": "list",
                "possible_values": "---\n- Low\n- High\n",
                "is_required": True,
                "is_for_all": True,
                "multiple": False,
            },
            {
                "id": 2,
                "type": "IssueCustomField",
                "name": "Reviewer",
                "field_format": "user",
                "is_required": False,
                "is_for_all": False,
                "multiple": False,
            },
            {
                "id": 3,
                "type": "UserCustomField",
                "name": "Phone",
                "field_format": "string",
                "is_required": False,
                "is_for_all": True,
                "multiple": False,
            },
        ],
        "custom_fields_trackers": [{"custom_field_id": 1, "tracker_id": 1}, {"custom_field_id": 2, "tracker_id": 1}],
        "custom_fields_projects": [{"custom_field_id": 2, "project_id": 1}],
        "custom_values": [
            {"id": 1, "customized_type": "Issue", "customized_id": 1, "custom_field_id": 1, "value": "High"},
            {"id": 2, "customized_type": "Issue", "customized_id": 1, "custom_field_id": 2, "value": "2"},
            # Severity is required, but this issue has none: the field is not declared required.
            {"id": 3, "customized_type": "Issue", "customized_id": 2, "custom_field_id": 1, "value": ""},
        ],
        "issues": [
            {
                "id": 1,
                "project_id": 1,
                "tracker_id": 1,
                "status_id": 2,
                "priority_id": 11,
                "author_id": 1,
                "assigned_to_id": 2,
                "category_id": 1,
                "fixed_version_id": 1,
                "subject": "Login fails",
                "description": "h2. Steps\n\n# Sign in\n# Wait\n\nRelated to #2. Unrelated to #9999.",
                "start_date": date(2026, 1, 5),
                "due_date": date(2026, 2, 1),
                "estimated_hours": 7.5,
                "done_ratio": 30,
                "is_private": False,
                "created_on": T0,
                "updated_on": T2,
            },
            {
                "id": 2,
                "project_id": 1,
                "tracker_id": 1,
                "status_id": 1,
                "priority_id": 10,
                "author_id": 4,
                "subject": "Session cookie is not cleared",
                "parent_id": 1,
                "done_ratio": 0,
                "is_private": False,
                "created_on": T1,
                "updated_on": T1,
            },
            {
                "id": 3,
                "project_id": 2,
                "tracker_id": 2,
                "status_id": 5,
                "priority_id": 10,
                "author_id": 2,
                "subject": "Crash on cold start",
                "description": "See #1.",
                "done_ratio": 100,
                "is_private": True,
                "created_on": T1,
                "updated_on": T2,
                "closed_on": T2,
            },
            {
                "id": 4,
                "project_id": 3,
                "tracker_id": 1,
                "status_id": 1,
                "priority_id": 10,
                "author_id": 1,
                "subject": "Rotate the certificates",
                "done_ratio": 0,
                "is_private": False,
                "created_on": T1,
                "updated_on": T1,
            },
        ],
        "journals": [
            {
                "id": 1,
                "journalized_id": 1,
                "journalized_type": "Issue",
                "user_id": 2,
                "notes": "Reproduced. See #3, and not #9999.",
                "created_on": T1,
                "private_notes": False,
            },
            {
                "id": 2,
                "journalized_id": 1,
                "journalized_type": "Issue",
                "user_id": 1,
                "notes": "",
                "created_on": T2,
                "private_notes": False,
            },
            {
                "id": 3,
                "journalized_id": 3,
                "journalized_type": "Issue",
                "user_id": 1,
                "notes": "Internal note.",
                "created_on": T2,
                "private_notes": True,
            },
        ],
        "journal_details": [
            {"id": 1, "journal_id": 2, "property": "attr", "prop_key": "status_id", "old_value": "1", "value": "2"},
            {
                "id": 2,
                "journal_id": 2,
                "property": "attr",
                "prop_key": "description",
                "old_value": "Was #2",
                "value": "Related to #2.",
            },
        ],
        "issue_relations": [
            {"id": 1, "issue_from_id": 1, "issue_to_id": 3, "relation_type": "relates"},
            # A reverse name: stored canonically, with the ends swapped.
            {"id": 2, "issue_from_id": 3, "issue_to_id": 4, "relation_type": "blocked"},
            {"id": 3, "issue_from_id": 1, "issue_to_id": 2, "relation_type": "precedes", "delay": 2},
            {"id": 4, "issue_from_id": 2, "issue_to_id": 3, "relation_type": "plugin_specific"},
        ],
        "watchers": [
            {"id": 1, "watchable_type": "Issue", "watchable_id": 1, "user_id": 2},
            {"id": 2, "watchable_type": "WikiPage", "watchable_id": 1, "user_id": 1},
        ],
        "wikis": [{"id": 1, "project_id": 1, "start_page": "Home", "status": 1}],
        "wiki_pages": [
            {"id": 1, "wiki_id": 1, "title": "Home", "created_on": T0, "protected": True},
            {"id": 2, "wiki_id": 1, "title": "Child_Page", "created_on": T1, "protected": False, "parent_id": 1},
        ],
        "wiki_content_versions": [
            {
                "id": 1,
                "wiki_content_id": 1,
                "page_id": 1,
                "author_id": 1,
                "data": b"h1. Home\n\nSee #1.",
                "compression": "",
                "comments": "",
                "updated_on": T0,
                "version": 1,
            },
            {
                "id": 2,
                "wiki_content_id": 1,
                "page_id": 1,
                "author_id": 2,
                "data": gzip.compress(b"h1. Home\n\nSecond *draft*."),
                "compression": "gzip",
                "comments": "Expanded",
                "updated_on": T1,
                "version": 2,
            },
            {
                "id": 3,
                "wiki_content_id": 2,
                "page_id": 2,
                "author_id": 2,
                "data": b"Child text.",
                "compression": "",
                "updated_on": T1,
                "version": 1,
            },
        ],
        "wiki_redirects": [
            {
                "id": 1,
                "wiki_id": 1,
                "title": "Old_Home",
                "redirects_to": "Home",
                "created_on": T1,
                "redirects_to_wiki_id": 1,
            },
        ],
        "attachments": [
            {
                "id": 1,
                "container_id": 1,
                "container_type": "Issue",
                "filename": "server.log",
                "disk_filename": "260105090000_server.log",
                "disk_directory": "2026/01",
                "filesize": len(LOG_BYTES),
                "content_type": "text/plain",
                "author_id": 1,
                "created_on": T0,
                "description": "The failing session",
            },
            {
                "id": 2,
                "container_id": 1,
                "container_type": "WikiPage",
                "filename": "notes.txt",
                "disk_filename": "1234_notes.txt",
                "disk_directory": "2026/01",
                "filesize": len(NOTES_BYTES),
                "content_type": "text/plain",
                "author_id": 2,
                "created_on": T1,
            },
            # Attached to a version: Specivo has nowhere to put it.
            {
                "id": 3,
                "container_id": 1,
                "container_type": "Version",
                "filename": "plan.pdf",
                "disk_filename": "999_plan.pdf",
                "disk_directory": "",
                "filesize": 10,
                "author_id": 1,
                "created_on": T1,
            },
            # Its file is gone from the files directory.
            {
                "id": 4,
                "container_id": 3,
                "container_type": "Issue",
                "filename": "missing.png",
                "disk_filename": "404_missing.png",
                "disk_directory": "2026/01",
                "filesize": 5,
                "author_id": 2,
                "created_on": T2,
            },
        ],
        "time_entries": [
            {
                "id": 1,
                "project_id": 1,
                "user_id": 2,
                "issue_id": 1,
                "hours": 1.333333,
                "comments": "Investigating",
                "activity_id": 12,
                "spent_on": date(2026, 1, 6),
                "created_on": T1,
                "updated_on": T1,
                "author_id": 2,
            },
            {
                "id": 2,
                "project_id": 2,
                "user_id": 2,
                "hours": 0.75,
                "activity_id": 12,
                "spent_on": date(2026, 1, 7),
                "created_on": T2,
                "updated_on": T2,
                "author_id": 1,
            },
        ],
    }
