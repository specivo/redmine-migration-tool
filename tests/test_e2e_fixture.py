"""End to end: the Redmine fixture, exported by the command, is a valid bundle.

Needs the fixture (see the README):

    make fixture-up PROFILE=pg && make fixture-seed PROFILE=pg && make fixture-files PROFILE=pg

The test is skipped when the fixture's database cannot be reached, unless
``REDMINE_FIXTURE_REQUIRED=1`` (as in CI), which turns the skip into a failure.
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from specivo_migration_sdk import validate_bundle
from sqlalchemy import text

from specivo_redmine_migration.cli import main
from tests.conftest import read_bundle

ROOT = Path(__file__).resolve().parent.parent

PROFILES = {
    "pg": {
        "url": os.environ.get("REDMINE_FIXTURE_PG_URL", "postgresql://redmine:redmine@127.0.0.1:5452/redmine"),
        "files": os.environ.get("REDMINE_FIXTURE_PG_FILES", str(ROOT / ".fixture-files" / "pg")),
    },
    "mysql": {
        "url": os.environ.get("REDMINE_FIXTURE_MYSQL_URL", "mysql://redmine:redmine@127.0.0.1:3309/redmine"),
        "files": os.environ.get("REDMINE_FIXTURE_MYSQL_FILES", str(ROOT / ".fixture-files" / "mysql")),
    },
}

pytestmark = pytest.mark.fixture


def _reachable(url: str) -> bool:
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 0), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(params=sorted(PROFILES))
def profile(request: pytest.FixtureRequest) -> dict[str, str]:
    settings = PROFILES[request.param]
    if not _reachable(settings["url"]) or not Path(settings["files"]).is_dir():
        message = f"Redmine fixture ({request.param}) is not running or its files were not copied out"
        if os.environ.get("REDMINE_FIXTURE_REQUIRED") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return settings


def test_fixture_export_is_a_valid_bundle(profile, tmp_path):
    out = tmp_path / "bundle"
    argv = ["--db-url", profile["url"], "--files-dir", profile["files"], "--out", str(out)]
    assert main([*argv, "--instance", "pytest-fixture", "--quiet"]) == 0

    report = validate_bundle(out)
    assert report.ok, report.format_text()

    bundle = read_bundle(out)
    counts = bundle.manifest["counts"]
    assert counts["projects"] == 4
    assert counts["issues"] == 5
    assert counts["attachments"] == 4
    assert counts["wiki_pages"] == 4
    assert bundle.manifest["source"] == {"system": "redmine", "instance": "pytest-fixture"}
    # The seed's "Related to #2." on the parent issue.
    parent = next(issue for issue in bundle.get("issues") if issue["subject"].startswith("Login fails"))
    assert "{{issue:" in parent["description"]
    assert "#2" not in parent["description"]


def test_fixture_export_with_passwords(profile, tmp_path):
    out = tmp_path / "bundle"
    argv = ["--db-url", profile["url"], "--files-dir", profile["files"], "--out", str(out), "--instance", "x"]
    assert main([*argv, "--import-passwords", "--quiet"]) == 0
    users = {user["login"]: user for user in read_bundle(out).get("users")}
    assert users["fixture_dev"]["credential"]["algorithm"] == "redmine_sha1"
    # A directory account's local hash means nothing to Redmine, so none is carried.
    assert "credential" not in users["fixture_ldap"]


async def _snapshot_settings(url: str) -> tuple[str, str]:
    from specivo_redmine_migration.source import RedmineSource

    source = RedmineSource(url)
    await source.connect()
    try:
        async with source._connection() as conn:
            if conn.dialect.name == "postgresql":
                read_only = (await conn.execute(text("SHOW transaction_read_only"))).scalar_one()
                isolation = (await conn.execute(text("SHOW transaction_isolation"))).scalar_one()
            else:
                isolation = (await conn.execute(text("SELECT @@transaction_isolation"))).scalar_one()
                # MySQL shows a READ ONLY transaction nowhere; a write must be refused instead.
                # Were it accepted, closing the source rolls it back.
                try:
                    await conn.execute(text("INSERT INTO settings (name, value) VALUES ('probe', 'x')"))
                    read_only = "writable"
                except Exception:  # noqa: BLE001 - the driver's own error class
                    read_only = "refused"
        return str(read_only), str(isolation)
    finally:
        await source.close()


def test_export_reads_in_one_read_only_repeatable_read_transaction(profile):
    read_only, isolation = asyncio.run(_snapshot_settings(profile["url"]))
    assert read_only in {"on", "1", "refused"}
    assert isolation.lower().replace("-", " ") == "repeatable read"
