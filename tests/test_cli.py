"""The command: where the bundle goes, what it holds, and its options."""

from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import pytest
from specivo_migration_sdk import validate_bundle

from specivo_redmine_migration.cli import main
from tests import sample_redmine


def _run(sample: sample_redmine.SampleRedmine, out: Path, *extra: str) -> int:
    return main(["--db-url", sample.url, "--files-dir", str(sample.files_dir), "--out", str(out), "--quiet", *extra])


class TestOutput:
    def test_writes_a_directory_by_default(self, sample, tmp_path):
        """Attachments stay plain files, so a large export can be copied with rsync and resumed."""
        out = tmp_path / "bundle"
        assert _run(sample, out) == 0
        assert out.is_dir()
        assert (out / "manifest.json").is_file()
        stored = sorted(path.name for path in (out / "files").iterdir())
        assert len(stored) == 2
        assert all(len(name) == 64 for name in stored)

    def test_zip_option_writes_an_archive(self, sample, tmp_path):
        out = tmp_path / "bundle.out"
        assert _run(sample, out, "--zip") == 0
        assert zipfile.is_zipfile(out)
        assert "manifest.json" in zipfile.ZipFile(out).namelist()

    def test_a_zip_suffix_writes_an_archive(self, sample, tmp_path):
        out = tmp_path / "bundle.zip"
        assert _run(sample, out) == 0
        assert zipfile.is_zipfile(out)

    def test_the_bundle_is_valid(self, sample, tmp_path):
        out = tmp_path / "bundle"
        assert _run(sample, out) == 0
        report = validate_bundle(out)
        assert report.ok, report.format_text()

    def test_validate_option_checks_the_result(self, sample, tmp_path, capsys):
        assert main(["--db-url", sample.url, "--out", str(tmp_path / "b"), "--validate"]) == 0
        assert "valid" in capsys.readouterr().out.lower()

    def test_refuses_a_target_that_is_not_empty(self, sample, tmp_path, capsys):
        out = tmp_path / "bundle"
        out.mkdir()
        (out / "something").write_text("x")
        assert _run(sample, out) == 2
        assert "already exists" in capsys.readouterr().err

    def test_progress_goes_to_stderr(self, sample, tmp_path, capsys):
        out = tmp_path / "bundle"
        assert main(["--db-url", sample.url, "--out", str(out)]) == 0
        captured = capsys.readouterr()
        assert "issues" in captured.err
        assert str(out) in captured.out

    def test_the_database_password_is_never_printed(self, sample, tmp_path, capsys):
        url = "postgresql://redmine:s3cret-value@127.0.0.1:1/redmine"
        assert main(["--db-url", url, "--out", str(tmp_path / "b"), "--instance", "x"]) == 1
        captured = capsys.readouterr()
        assert "s3cret-value" not in captured.out + captured.err

    def test_database_url_can_come_from_the_environment(self, sample, tmp_path, monkeypatch):
        monkeypatch.setenv("REDMINE_DB_URL", sample.url)
        out = tmp_path / "bundle"
        assert main(["--out", str(out), "--quiet"]) == 0
        assert (out / "manifest.json").exists()


class TestContent:
    def test_manifest_counts_and_generator(self, export):
        manifest = export().manifest
        assert manifest["generator"]["name"] == "specivo-redmine-migration"
        assert manifest["counts"]["issues"] == 4
        assert manifest["counts"]["wiki_versions"] == 3

    def test_text_is_converted_to_markdown(self, export):
        bundle = export()
        assert bundle.by_ref("issues")["1"]["description"].startswith("## Steps")
        assert bundle.by_ref("wiki_pages")["1"]["versions"][1]["text"] == "# Home\n\nSecond **draft**."

    def test_markdown_sources_are_not_converted(self, tmp_path):
        sample = sample_redmine.build(tmp_path / "source", text_formatting="common_mark")
        out = tmp_path / "bundle"
        assert main(["--db-url", sample.url, "--out", str(out), "--quiet"]) == 0
        first = json.loads((out / "issues.jsonl").read_text().splitlines()[0])
        assert first["description"].startswith("h2. Steps")

    def test_without_files_dir_attachments_are_reported_not_carried(self, export):
        bundle = export(files=False)
        assert bundle.get("attachments") == []
        assert any("attachment" in item["what"] for item in bundle.manifest["not_carried_over"])

    def test_what_is_left_behind_is_listed(self, export):
        what = [item["what"] for item in export().manifest["not_carried_over"]]
        assert "module repository" in what
        assert any("missing.png" in line for line in what)
        assert any("Version" in line for line in what)
        assert any("plugin_specific" in line for line in what)
        assert any("per-project" in line for line in what)
        assert any("custom field" in line for line in what)

    def test_public_access_is_carried(self, export):
        access = export().public_access
        assert access is not None
        assert access["anonymous_allowed"] is True
        assert access["anonymous_permissions"] == {"1": ["view_issues", "view_wiki"], "2": ["view_issues"]}

    def test_required_custom_field_with_blank_values_is_relaxed(self, export):
        severity = export().by_ref("custom_fields")["1"]
        assert severity["json_schema"] == {"type": "string", "enum": ["Low", "High"]}
        assert severity.get("is_required", False) is False


class TestPasswords:
    def test_credentials_are_left_out_by_default(self, export):
        assert all("credential" not in user for user in export().get("users"))

    def test_import_passwords_writes_redmine_sha1_credentials(self, export):
        users = export("--import-passwords").by_ref("users")
        assert users["2"]["credential"] == {
            "algorithm": "redmine_sha1",
            "fields": {"salt": sample_redmine.DEV_SALT, "hashed_password": sample_redmine.DEV_HASH},
        }

    def test_administrators_credentials_are_never_exported(self, export):
        """Specivo never keeps an administrator's old password, so the bundle does not carry it."""
        users = export("--import-passwords").by_ref("users")
        assert users["1"]["is_admin"] is True
        assert "credential" not in users["1"]

    def test_a_directory_with_passwords_is_private(self, sample, tmp_path):
        out = tmp_path / "bundle"
        assert _run(sample, out, "--import-passwords") == 0
        assert stat.S_IMODE(out.stat().st_mode) == 0o700
        for path in out.rglob("*"):
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode & 0o077 == 0, f"{path} is readable by others: {oct(mode)}"

    def test_a_zip_with_passwords_is_private(self, sample, tmp_path):
        out = tmp_path / "bundle.zip"
        assert _run(sample, out, "--import-passwords") == 0
        assert stat.S_IMODE(out.stat().st_mode) == 0o600

    def test_an_existing_empty_directory_is_made_private(self, sample, tmp_path):
        out = tmp_path / "bundle"
        out.mkdir(mode=0o755)
        assert _run(sample, out, "--import-passwords") == 0
        assert stat.S_IMODE(out.stat().st_mode) == 0o700


class TestScope:
    def test_project_option_selects_a_subtree(self, export):
        bundle = export("--project", "acme-app")
        assert set(bundle.by_ref("projects")) == {"1", "2"}
        assert set(bundle.by_ref("issues")) == {"1", "2", "3"}
        # Issue 4 is outside the scope, so the relation to it is not carried.
        assert set(bundle.by_ref("relations")) == {"1", "3"}

    def test_project_option_takes_ids_and_repeats(self, export):
        bundle = export("--project", "3", "--project", "acme-mobile")
        assert set(bundle.by_ref("projects")) == {"2", "3"}
        # The parent is outside the scope.
        assert "parent_ref" not in bundle.by_ref("projects")["2"]

    def test_unknown_project_is_an_error(self, sample, tmp_path, capsys):
        assert _run(sample, tmp_path / "b", "--project", "nope") == 2
        assert "nope" in capsys.readouterr().err

    @pytest.mark.parametrize("scope", [(), ("--project", "acme-mobile")])
    def test_scoped_bundles_are_valid(self, sample, tmp_path, scope):
        out = tmp_path / "bundle"
        assert _run(sample, out, *scope) == 0
        report = validate_bundle(out)
        assert report.ok, report.format_text()

    def test_limit_keeps_the_first_issues_of_each_project_parents_first(self, sample, tmp_path):
        out = tmp_path / "bundle"
        assert _run(sample, out, "--limit", "1") == 0
        issues = [json.loads(line) for line in (out / "issues.jsonl").read_text().splitlines()]
        assert sorted(issue["source_ref"] for issue in issues) == ["1", "3", "4"]
        report = validate_bundle(out)
        assert report.ok, report.format_text()
