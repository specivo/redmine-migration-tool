"""Wiki history is read a few pages at a time, never the whole wiki in one query.

A wiki's revisions are its biggest table: every revision is a full copy of the
page. Reading them all at once would hold the whole history in memory, so the
export reads the revisions of a small batch of pages, writes those pages, and
moves on. The test counts the queries that read revisions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from specivo_redmine_migration.cli import main
from specivo_redmine_migration.source import RedmineSource
from tests import sample_redmine
from tests.conftest import read_bundle


@pytest.fixture
def revision_queries():
    seen: list[tuple[str, Any]] = []

    def record(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if "FROM wiki_content_versions" in statement:
            seen.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", record)
    yield seen
    event.remove(Engine, "before_cursor_execute", record)


def test_revisions_are_read_a_batch_of_pages_at_a_time(tmp_path: Path, monkeypatch, revision_queries):
    monkeypatch.setattr(RedmineSource, "wiki_page_batch", 1)
    sample = sample_redmine.build(tmp_path / "source")
    out = tmp_path / "bundle"
    assert main(["--db-url", sample.url, "--out", str(out), "--quiet"]) == 0

    # The sample wiki has two pages: one query each.
    assert len(revision_queries) == 2
    pages = read_bundle(out).by_ref("wiki_pages")
    assert [len(pages[ref]["versions"]) for ref in ("1", "2")] == [2, 1]


def test_the_default_batch_is_small():
    assert 1 <= RedmineSource.wiki_page_batch <= 50
