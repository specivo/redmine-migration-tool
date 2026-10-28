"""Source refs are the ones Specivo's direct Redmine import recorded.

The identifier map keys every imported row by entity type and source ref. A
bundle from this tool can continue an installation that was first imported
directly (and the other way round) only if both name every record the same way,
so the table below is the direct adapter's rule, entity type by entity type:
the ref is the Redmine row's primary key, as a string, taken from the table
named. Watchers and wiki redirects have no ref of their own in either.
"""

from __future__ import annotations

import pytest

from tests.conftest import Bundle

# Bundle list -> (Redmine table the direct adapter reads, refs the sample must give).
DIRECT_ADAPTER_REFS: dict[str, tuple[str, set[str]]] = {
    "trackers": ("trackers.id", {"1", "2"}),
    "statuses": ("issue_statuses.id", {"1", "2", "5"}),
    # Instance-wide rows of enumerations only; the per-project override (13) is not carried.
    "priorities": ("enumerations.id where type = IssuePriority", {"10", "11"}),
    "activities": ("enumerations.id where type = TimeEntryActivity", {"12"}),
    "roles": ("roles.id", {"1", "2", "3"}),
    "users": ("users.id where type = User", {"1", "2"}),
    "groups": ("users.id where type = Group", {"3"}),
    "projects": ("projects.id", {"1", "2", "3"}),
    # members.id; the builtin anonymous principal's row and the inherited-only one are left out.
    "memberships": ("members.id", {"1", "2", "5"}),
    "versions": ("versions.id", {"1"}),
    "categories": ("issue_categories.id", {"1"}),
    "custom_fields": ("custom_fields.id where type = IssueCustomField", {"1", "2"}),
    "issues": ("issues.id", {"1", "2", "3", "4"}),
    "journals": ("journals.id", {"1", "2", "3"}),
    # issue_relations.id, also for a relation stored under its reverse name.
    "relations": ("issue_relations.id", {"1", "2", "3"}),
    "wiki_pages": ("wiki_pages.id", {"1", "2"}),
    # The file is missing for attachment 4, and 3 hangs off a version.
    "attachments": ("attachments.id", {"1", "2"}),
    "time_entries": ("time_entries.id", {"1", "2"}),
}

LOOKUPS = {"trackers", "statuses", "priorities", "activities", "roles"}


def _refs(bundle: Bundle, name: str) -> set[str]:
    records = bundle.lookups.get(name, []) if name in LOOKUPS else bundle.get(name)
    return {record["source_ref"] for record in records}


@pytest.mark.parametrize("name", sorted(DIRECT_ADAPTER_REFS))
def test_source_refs_match_the_direct_adapter_ids(export, name):
    bundle = export()
    _, expected = DIRECT_ADAPTER_REFS[name]
    assert _refs(bundle, name) == expected


def test_wiki_versions_are_keyed_by_their_history_row(export):
    """wiki_content_versions.id, never the page id and the version number."""
    pages = export().by_ref("wiki_pages")
    assert [version["source_ref"] for version in pages["1"]["versions"]] == ["1", "2"]
    assert [version["source_ref"] for version in pages["2"]["versions"]] == ["3"]


def test_references_between_records_use_the_same_refs(export):
    bundle = export()
    issue = bundle.by_ref("issues")["1"]
    assert (issue["project_ref"], issue["tracker_ref"], issue["status_ref"], issue["priority_ref"]) == (
        "1",
        "1",
        "2",
        "11",
    )
    assert (issue["author_ref"], issue["assigned_to_ref"], issue["category_ref"], issue["fixed_version_ref"]) == (
        "1",
        "2",
        "1",
        "1",
    )
    assert bundle.by_ref("issues")["2"]["parent_ref"] == "1"
    assert {(w["container_kind"], w["container_ref"], w["user_ref"]) for w in bundle.get("watchers")} == {
        ("issue", "1", "2"),
        ("wiki_page", "1", "1"),
    }
    assert bundle.get("wiki_redirects") == [{"project_ref": "1", "title": "Old_Home", "redirects_to": "Home"}]
    reviewer = {value["key"]: value for value in issue["custom_values"]}["reviewer"]
    assert (reviewer["value"], reviewer["value_kind"]) == ("2", "user_ref")


def test_a_reverse_relation_is_stored_canonically(export):
    relation = export().by_ref("relations")["2"]
    assert (relation["from_ref"], relation["to_ref"], relation["relation_type"]) == ("4", "3", "blocks")
