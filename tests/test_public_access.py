"""Unit tests for translating Redmine's non-member and anonymous access.

Literal rows, shaped like a Redmine 7.0.1 database, so the rules that decide
whether an import may open a project are pinned without a Redmine instance.
"""

from __future__ import annotations

import pytest

from specivo_redmine_migration.extract import (
    TYPE_GROUP_ANONYMOUS,
    TYPE_GROUP_NON_MEMBER,
    anonymous_login_allowed,
    build_public_access,
    parse_role_permissions,
    readable_permissions,
)

NON_MEMBER = {
    "id": 1,
    "name": "Non member",
    "builtin": 1,
    "permissions": "---\n- :view_issues\n- :add_issues\n- :add_issue_notes\n- :view_wiki_pages\n",
}
ANONYMOUS = {
    "id": 2,
    "name": "Anonymous",
    "builtin": 2,
    "permissions": "---\n- :view_issues\n- :view_wiki_pages\n- :view_wiki_edits\n- :view_files\n",
}
REPORTER = {"id": 5, "name": "Reporter", "builtin": 0, "permissions": "---\n- :view_issues\n- :add_issues\n"}
ISSUE_READER = {"id": 6, "name": "Issue Reader", "builtin": 0, "permissions": "---\n- :view_issues\n- :view_gantt\n"}
ROLES = [NON_MEMBER, ANONYMOUS, REPORTER, ISSUE_READER]

BOTH_MODULES = ["issue_tracking", "wiki"]


def _project(project_id: int, identifier: str, *, is_public: bool = True, status: int = 1) -> dict:
    return {"id": project_id, "identifier": identifier, "is_public": is_public, "status": status}


def _build(login_required="0", projects=None, modules=None, overrides=None):
    projects = projects if projects is not None else [_project(1, "alpha")]
    modules = modules if modules is not None else {p["id"]: BOTH_MODULES for p in projects}
    return build_public_access(
        login_required=login_required,
        roles=ROLES,
        projects=projects,
        modules=modules,
        overrides=overrides or {},
    )


class TestParseRolePermissions:
    def test_reads_ruby_yaml_symbols(self):
        assert parse_role_permissions("---\n- :view_issues\n- :add_issues\n") == ["view_issues", "add_issues"]

    def test_empty_and_missing(self):
        assert parse_role_permissions(None) == []
        assert parse_role_permissions("--- []\n") == []

    def test_duplicates_collapse(self):
        assert parse_role_permissions("---\n- :view_issues\n- :view_issues\n") == ["view_issues"]


class TestLoginRequired:
    @pytest.mark.parametrize("value", [None, "1", "true", "", " "])
    def test_anything_but_an_explicit_zero_requires_sign_in(self, value):
        assert anonymous_login_allowed(value) is False

    def test_explicit_zero_allows_anonymous(self):
        assert anonymous_login_allowed("0") is True


class TestReadablePermissions:
    def test_only_read_permissions_carry_over(self):
        assert readable_permissions(["view_issues", "add_issues", "view_wiki_pages", "view_files"]) == [
            "view_issues",
            "view_wiki",
        ]

    def test_a_disabled_module_grants_nothing(self):
        assert readable_permissions(["view_issues", "view_wiki_pages"], ["issue_tracking"]) == ["view_issues"]
        assert readable_permissions(["view_issues", "view_wiki_pages"], []) == []


class TestAnonymousAccess:
    def test_public_project_gets_the_anonymous_roles_read_permissions(self):
        access = _build()
        assert access.anonymous_allowed is True
        assert access.anonymous_permissions == {"1": ["view_issues", "view_wiki"]}

    @pytest.mark.parametrize("value", [None, "1"])
    def test_nothing_when_the_source_requires_sign_in(self, value):
        access = _build(login_required=value)
        assert access.anonymous_allowed is False
        assert access.anonymous_permissions == {}

    def test_private_project_never_gets_anything(self):
        access = _build(projects=[_project(1, "alpha", is_public=False)])
        assert access.anonymous_permissions == {}

    @pytest.mark.parametrize("status", [9, 10, 0, 2, 42, None])
    def test_only_active_and_closed_projects_can_be_readable(self, status):
        """Archived (9), scheduled for deletion (10) and any status Redmine may
        add later grant nothing: the statuses are an allowlist, failing closed."""
        access = _build(projects=[_project(1, "alpha", status=status)])
        assert access.anonymous_permissions == {}

    def test_closed_project_keeps_its_access(self):
        """Redmine lets anyone allowed read a closed project."""
        access = _build(projects=[_project(1, "alpha", status=5)])
        assert access.anonymous_permissions == {"1": ["view_issues", "view_wiki"]}

    def test_module_gating(self):
        access = _build(modules={1: ["issue_tracking"]})
        assert access.anonymous_permissions == {"1": ["view_issues"]}

    def test_group_anonymous_override_replaces_the_builtin_role(self):
        projects = [_project(1, "alpha"), _project(2, "beta")]
        access = _build(projects=projects, overrides={(2, TYPE_GROUP_ANONYMOUS): [6]})
        assert access.anonymous_permissions == {"1": ["view_issues", "view_wiki"], "2": ["view_issues"]}
        assert "beta: per-project Anonymous override (Issue Reader): view_gantt" in access.not_carried_over

    def test_override_without_read_permissions_hides_the_project(self):
        access = _build(overrides={(1, TYPE_GROUP_ANONYMOUS): [5]})
        # Reporter reads issues; its write permission is reported, not granted.
        assert access.anonymous_permissions == {"1": ["view_issues"]}
        assert "alpha: per-project Anonymous override (Reporter): add_issues" in access.not_carried_over


class TestNonMemberAccess:
    def test_read_permissions_are_translated(self):
        assert _build().non_member_permissions == ["view_issues", "view_wiki"]

    def test_write_permissions_are_reported_as_dropped(self):
        assert "Non member role: add_issue_notes, add_issues" in _build().not_carried_over

    def test_anonymous_extras_are_reported(self):
        assert "Anonymous role: view_files, view_wiki_edits" in _build().not_carried_over

    def test_per_project_non_member_override_is_reported_not_applied(self):
        access = _build(overrides={(1, TYPE_GROUP_NON_MEMBER): [5]})
        assert "alpha: per-project Non member override (Reporter): add_issues, view_issues" in access.not_carried_over
        assert access.non_member_permissions == ["view_issues", "view_wiki"]
