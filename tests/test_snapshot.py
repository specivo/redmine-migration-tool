"""The export reads Redmine through one connection, in one transaction.

Redmine may be in use while it is exported. Reading everything in one
read-only, repeatable-read transaction means every query sees the same moment
(no journal without its issue, no attachment of a page created after the pages
were read), and the session cannot write. The isolation itself is checked
against the real databases in ``test_e2e_fixture.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.pool import Pool

from specivo_redmine_migration.cli import main
from tests import sample_redmine


def test_the_whole_export_uses_one_connection(tmp_path: Path):
    checkouts: list[Any] = []

    def record(dbapi_connection: Any, connection_record: Any, connection_proxy: Any) -> None:
        checkouts.append(connection_record)

    sample = sample_redmine.build(tmp_path / "source")
    event.listen(Pool, "checkout", record)
    try:
        assert (
            main(
                ["--db-url", sample.url, "--files-dir", str(sample.files_dir), "--out", str(tmp_path / "b"), "--quiet"]
            )
            == 0
        )
    finally:
        event.remove(Pool, "checkout", record)
    assert len(checkouts) == 1
