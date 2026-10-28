"""MySQL's zero dates (``0000-00-00``) become empty values, and are counted.

MySQL used to accept ``0000-00-00`` and ``0000-00-00 00:00:00`` as "no date",
and old Redmine databases still hold them. The driver hands them over as
strings, since no date can hold them. The export writes them as absent, counts
them in its summary and lists the count in the manifest; a time entry, which
needs a date, falls back to the day it was logged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from specivo_migration_sdk import validate_bundle
from sqlalchemy import create_engine, text

from specivo_redmine_migration import extract
from specivo_redmine_migration.cli import main
from tests import sample_redmine


class TestConversions:
    def test_a_zero_timestamp_is_none(self):
        assert extract.as_utc("0000-00-00 00:00:00") is None

    def test_a_zero_date_is_none(self):
        assert extract.as_date("0000-00-00") is None

    def test_a_timestamp_string_is_parsed_as_utc(self):
        assert extract.as_utc("2026-01-05 09:00:00") == datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

    def test_zero_dates_are_counted(self):
        extract.zero_dates.reset()
        extract.as_utc("0000-00-00 00:00:00")
        extract.as_date("0000-00-00")
        extract.as_utc(None)
        assert extract.zero_dates.count == 2

    def test_an_issue_with_zero_dates_has_none(self):
        issue = extract.extract_issue(
            {"id": 1, "project_id": 1, "tracker_id": 1, "status_id": 1, "priority_id": 1, "subject": "S",
             "start_date": "0000-00-00", "due_date": "0000-00-00", "created_on": "0000-00-00 00:00:00",
             "updated_on": "0000-00-00 00:00:00", "closed_on": "0000-00-00 00:00:00"}
        )  # fmt: skip
        assert (issue.start_date, issue.due_date, issue.created_at, issue.updated_at, issue.closed_at) == (
            None,
            None,
            None,
            None,
            None,
        )

    def test_a_time_entry_on_a_zero_date_falls_back_to_when_it_was_logged(self):
        entry = extract.extract_time_entry(
            {"id": 1, "project_id": 1, "hours": 1.0, "spent_on": "0000-00-00",
             "created_on": datetime(2026, 1, 7, 14, 15)}
        )  # fmt: skip
        assert str(entry.spent_on) == "2026-01-07"


def _zero_some_dates(sample: sample_redmine.SampleRedmine) -> None:
    path = sample.url.removeprefix("sqlite+aiosqlite:///")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE issues SET start_date = '0000-00-00', created_on = '0000-00-00 00:00:00' WHERE id = 4")
        )
        conn.execute(text("UPDATE time_entries SET spent_on = '0000-00-00' WHERE id = 2"))
    engine.dispose()


def test_zero_dates_in_the_database_are_left_empty_and_counted(tmp_path: Path, capsys):
    sample = sample_redmine.build(tmp_path / "source")
    _zero_some_dates(sample)
    out = tmp_path / "bundle"
    assert main(["--db-url", sample.url, "--out", str(out)]) == 0
    assert "zero dates" in capsys.readouterr().err

    report = validate_bundle(out)
    assert report.ok, report.format_text()
    issues = {
        json.loads(line)["source_ref"]: json.loads(line) for line in (out / "issues.jsonl").read_text().splitlines()
    }
    assert "start_date" not in issues["4"]
    assert "created_at" not in issues["4"]
    entries = {
        json.loads(line)["source_ref"]: json.loads(line)
        for line in (out / "time_entries.jsonl").read_text().splitlines()
    }
    assert entries["2"]["spent_on"] == "2026-01-07"
    manifest = json.loads((out / "manifest.json").read_text())
    assert {"what": "dates stored as 0000-00-00 (left empty)", "count": 3} in manifest["not_carried_over"]
