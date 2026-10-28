"""The manifest's ``source.instance``: Redmine's host name, or ``--instance``, never the database URL.

The instance scopes Specivo's identifier map, so it must be the same on every
export of one installation and different for every other installation. The
database URL is neither stable (a moved database, a replica) nor safe to
write down, so it is never used, not even as a fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from specivo_redmine_migration.cli import main
from tests import sample_redmine
from tests.conftest import read_bundle


def test_manifest_names_the_source(export):
    source = export().manifest["source"]
    assert source["system"] == "redmine"
    assert source["instance"] == "tracker.example.com"


def test_instance_option_overrides_the_host_name(export):
    assert export("--instance", "legacy-tracker").manifest["source"]["instance"] == "legacy-tracker"


def test_host_name_is_used_without_its_surrounding_whitespace(tmp_path: Path):
    sample = sample_redmine.build(tmp_path / "source", host_name="  tracker.example.com  ")
    out = tmp_path / "bundle"
    assert main(["--db-url", sample.url, "--out", str(out), "--quiet"]) == 0
    assert read_bundle(out).manifest["source"]["instance"] == "tracker.example.com"


def test_instance_is_required_when_redmine_has_no_host_name(tmp_path: Path, capsys):
    sample = sample_redmine.build(tmp_path / "source", host_name=None)
    out = tmp_path / "bundle"
    assert main(["--db-url", sample.url, "--out", str(out), "--quiet"]) == 2
    assert "--instance" in capsys.readouterr().err
    # Nothing is written, so nothing half-finished can be loaded by mistake.
    assert not (out / "manifest.json").exists()


def test_blank_host_name_counts_as_none(tmp_path: Path):
    sample = sample_redmine.build(tmp_path / "source", host_name="   ")
    assert main(["--db-url", sample.url, "--out", str(tmp_path / "bundle"), "--quiet"]) == 2


def test_database_url_never_reaches_the_bundle(tmp_path: Path):
    sample = sample_redmine.build(tmp_path / "source", host_name=None)
    out = tmp_path / "bundle"
    assert main(["--db-url", sample.url, "--out", str(out), "--instance", "legacy", "--quiet"]) == 0
    for path in out.iterdir():
        if path.is_file():
            assert "redmine.sqlite3" not in path.read_text(errors="replace")


@pytest.mark.parametrize(
    "host_name",
    [
        "localhost:3000",  # Redmine's own default
        "localhost",
        "LOCALHOST:8080/redmine",
        "127.0.0.1:3000",
        "[::1]:3000",
        "0.0.0.0:3000",
        "example.com",
        "example.net/redmine",
    ],
)
def test_a_default_or_local_host_name_is_refused(tmp_path: Path, capsys, host_name):
    """Every untouched installation has these, so they would merge unrelated Redmines in Specivo."""
    sample = sample_redmine.build(tmp_path / "source", host_name=host_name)
    out = tmp_path / "bundle"
    assert main(["--db-url", sample.url, "--out", str(out), "--quiet"]) == 2
    err = capsys.readouterr().err
    assert "--instance" in err
    assert host_name in err
    assert "Traceback" not in err
    assert not (out / "manifest.json").exists()


def test_instance_option_overrides_a_default_host_name(tmp_path: Path):
    sample = sample_redmine.build(tmp_path / "source", host_name="localhost:3000")
    out = tmp_path / "bundle"
    assert main(["--db-url", sample.url, "--out", str(out), "--instance", "legacy-tracker", "--quiet"]) == 0
    assert read_bundle(out).manifest["source"]["instance"] == "legacy-tracker"


@pytest.mark.parametrize("host_name", ["tracker.example.com", "redmine.internal.test:8443/rm", "localhosting.test"])
def test_a_real_host_name_is_accepted(tmp_path: Path, host_name):
    sample = sample_redmine.build(tmp_path / "source", host_name=host_name)
    out = tmp_path / "bundle"
    assert main(["--db-url", sample.url, "--out", str(out), "--quiet"]) == 0
    assert read_bundle(out).manifest["source"]["instance"] == host_name
