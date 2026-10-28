"""Issue references become ``{{issue:N}}`` placeholders.

Redmine writes a reference to another issue as ``#N``. Specivo's direct import
rewrites those in issue descriptions and comments once every issue exists; a
bundle cannot know the display keys, so the tool writes a placeholder there
instead, with the same matching rule, never inside a fenced code block. An
issue in the bundle becomes ``{{issue:N}}``. An issue of Redmine that is not in
the bundle becomes ``{{issue:N|#N}}``: an installation exported one project at
a time refers to issues an earlier bundle carried, and Specivo looks those up
among the earlier imports of the same source, or writes the label ``#N`` when
it cannot. A number naming no issue of Redmine stays ``#N``. Wiki text keeps
``#N`` as written, as the direct import does.

When Specivo loads an issue it records the description in the issue's creation
history. The direct import leaves ``#N`` in that record (a known bug); with the
placeholder in the issue's description, the bundle import expands it there too.
"""

from __future__ import annotations

import asyncio

from specivo_migration_sdk import validate_bundle

from specivo_redmine_migration.references import issue_placeholders
from specivo_redmine_migration.source import RedmineSource


def test_creation_history_description_uses_issue_placeholders(export):
    """The description Specivo records as the issue's creation carries the placeholder, not ``#2``."""
    description = export().by_ref("issues")["1"]["description"]
    assert "{{issue:2}}" in description
    assert "#2" not in description


def test_a_number_naming_no_issue_of_redmine_is_left_as_written(export):
    description = export().by_ref("issues")["1"]["description"]
    assert "Unrelated to #9999." in description
    assert "{{issue:9999" not in description


def test_comments_use_issue_placeholders(export):
    notes = export().by_ref("journals")["1"]["notes"]
    assert "{{issue:3}}" in notes
    assert "not #9999" in notes


def test_a_project_export_refers_to_issues_of_other_projects(export):
    """Issue 3 (acme-mobile) says "See #1."; issue 1 is in acme-app, exported separately.

    It is a placeholder labelled ``#1``: the load links it to issue 1 when an
    earlier bundle brought it in, and writes ``#1`` otherwise.
    """
    bundle = export("--project", "acme-mobile")
    assert set(bundle.by_ref("issues")) == {"3"}
    assert bundle.by_ref("issues")["3"]["description"] == "See {{issue:1|#1}}."

    report = validate_bundle(bundle.root)
    assert report.ok, report.format_text()
    assert [(p.file, str(p.code)) for p in report.warnings] == [("issues.jsonl", "external_ref")]
    assert report.warnings[0].message.startswith("1 issue placeholder(s) name no issue in this bundle, e.g. '1'")


def test_wiki_text_keeps_references_as_written(export):
    text = export().by_ref("wiki_pages")["1"]["versions"][0]["text"]
    assert "#1" in text
    assert "{{issue:" not in text


def test_recorded_field_changes_are_kept_as_written(export):
    """Redmine's own description-change records are history, carried verbatim."""
    details = export().by_ref("journals")["2"]["details"]
    assert {"property": "attr", "prop_key": "description", "old_value": "Was #2", "new_value": "Related to #2."} in (
        details
    )


def test_a_numbered_list_line_is_not_a_reference(export):
    """Textile's ``# Sign in`` is a list item; it must come out as one, untouched."""
    description = export().by_ref("issues")["1"]["description"]
    assert "Sign in" in description
    assert "{{issue:Sign" not in description


def test_the_source_lists_every_issue_id(sample):
    """One id-only query over the whole instance, whatever projects are exported."""

    async def read() -> frozenset[str]:
        source = RedmineSource(sample.url)
        await source.connect()
        try:
            return await source.issue_ids()
        finally:
            await source.close()

    assert asyncio.run(read()) == frozenset({"1", "2", "3", "4"})


class TestIssuePlaceholders:
    in_bundle = frozenset({"2", "15"})
    in_redmine = frozenset({"2", "3", "7", "15", "150"})

    def _rewrite(self, text: str) -> str:
        return issue_placeholders(text, self.in_bundle, self.in_redmine)

    def test_an_issue_in_the_bundle(self):
        assert self._rewrite("See #2.") == "See {{issue:2}}."

    def test_an_issue_of_redmine_outside_the_bundle_is_labelled_with_its_reference(self):
        assert self._rewrite("See #3 and #150.") == "See {{issue:3|#3}} and {{issue:150|#150}}."

    def test_a_number_naming_no_issue_is_left_as_written(self):
        assert self._rewrite("Step #4, issue #9999, #002") == "Step #4, issue #9999, #002"

    def test_a_longer_number_is_not_a_prefix_match(self):
        assert self._rewrite("#3a #1500") == "#3a #1500"

    def test_leaves_code_blocks_alone(self):
        text = "Before #2\n\n```\necho #2\n```\n\n~~~\n#7\n~~~\nAfter #15 and #7"
        assert self._rewrite(text) == (
            "Before {{issue:2}}\n\n```\necho #2\n```\n\n~~~\n#7\n~~~\nAfter {{issue:15}} and {{issue:7|#7}}"
        )

    def test_leaves_anchors_entities_and_double_hashes_alone(self):
        for text in ("page#2", "&#15;", "##2"):
            assert self._rewrite(text) == text

    def test_text_without_a_hash_is_returned_as_is(self):
        assert self._rewrite("No references.") == "No references."
