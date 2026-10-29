"""History values that name a record are refs, with Redmine's name for the record.

Specivo stores its own id for a ref it imported, and shows the text otherwise:
a group as assignee, a version since deleted. Redmine keeps only ids in its
history, so the export looks the names up; a record Redmine no longer has is
named by kind and id rather than left empty, which Specivo would show as
"(none)". A user or version custom field's change is written the same way.
"""

from __future__ import annotations

from specivo_migration_sdk import validate_bundle


def _details(bundle):
    return {
        (d["property"], d["prop_key"]): d
        for d in bundle.by_ref("journals")["2"]["details"]
        if d["property"] != "attr" or d["prop_key"] != "description"
    }


def test_attribute_refs_carry_redmine_names(export):
    details = _details(export())
    status = details[("attr", "status_id")]
    assert (status["old_ref"], status["new_ref"], status["old_value"], status["new_value"]) == (
        "1",
        "2",
        "New",
        "In Progress",
    )
    assignee = details[("attr", "assigned_to_id")]
    # A group, which Specivo assigns nothing to, by its name.
    assert (assignee["old_ref"], assignee["old_value"], assignee["new_value"]) == ("3", "Platform Team", "Dev Account")


def test_a_record_redmine_no_longer_has_is_named_by_kind_and_id(export):
    version = _details(export())[("attr", "fixed_version_id")]
    assert (version["old_ref"], version["old_value"]) == ("99", "Version #99 (deleted)")
    assert "new_ref" not in version and "new_value" not in version


def test_a_user_custom_field_change_is_by_ref(export):
    details = _details(export())
    reviewer = details[("cf", "2")]
    assert (reviewer["old_ref"], reviewer["new_ref"], reviewer["old_value"], reviewer["new_value"]) == (
        "1",
        "2",
        "Sample Admin",
        "Dev Account",
    )
    # Any other field's values stay as written.
    assert details[("cf", "1")] == {"property": "cf", "prop_key": "1", "old_value": "Low", "new_value": "High"}


def test_the_bundle_is_valid(export):
    report = validate_bundle(export().root)
    assert report.ok, report.format_text()
