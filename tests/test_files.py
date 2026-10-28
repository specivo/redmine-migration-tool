"""Unit tests for locating an attachment's bytes in Redmine's storage.

The locator comes out of somebody else's database, so the tests cover both
on-disk layouts and what happens when the value is hostile.
"""

from __future__ import annotations

import pytest

from specivo_redmine_migration.files import AttachmentPathError, build_storage_key, resolve_attachment_path


class TestStorageKey:
    def test_dated_layout_joins_directory_and_filename(self):
        assert build_storage_key("2026/03", "abc123.pdf") == "2026/03/abc123.pdf"

    def test_flat_layout_has_no_directory(self):
        assert build_storage_key("", "abc123.pdf") == "abc123.pdf"

    def test_null_directory_is_treated_as_flat(self):
        assert build_storage_key(None, "abc123.pdf") == "abc123.pdf"

    def test_surrounding_slashes_are_dropped(self):
        assert build_storage_key("/2026/03/", "abc123.pdf") == "2026/03/abc123.pdf"


class TestResolve:
    def test_dated_layout_is_found(self, tmp_path):
        target = tmp_path / "2026" / "03"
        target.mkdir(parents=True)
        (target / "abc.pdf").write_bytes(b"x")

        assert resolve_attachment_path(tmp_path, "2026/03/abc.pdf") == (target / "abc.pdf").resolve()

    def test_flat_layout_is_found(self, tmp_path):
        (tmp_path / "abc.pdf").write_bytes(b"x")
        assert resolve_attachment_path(tmp_path, "abc.pdf") == (tmp_path / "abc.pdf").resolve()

    def test_falls_back_to_the_flat_layout(self, tmp_path):
        """Old files are not moved when an instance adopts dated directories."""
        (tmp_path / "abc.pdf").write_bytes(b"x")
        assert resolve_attachment_path(tmp_path, "2026/03/abc.pdf") == (tmp_path / "abc.pdf").resolve()

    def test_missing_file_still_returns_a_path_to_report(self, tmp_path):
        """The caller reports the path it tried, which is how a wrong mount is spotted."""
        resolved = resolve_attachment_path(tmp_path, "2026/03/gone.pdf")
        assert resolved.name == "gone.pdf"
        assert not resolved.exists()

    def test_empty_locator_is_refused(self, tmp_path):
        with pytest.raises(AttachmentPathError):
            resolve_attachment_path(tmp_path, "")

    def test_traversal_is_refused(self, tmp_path):
        """A crafted value must not make the importer read outside the directory."""
        with pytest.raises(AttachmentPathError):
            resolve_attachment_path(tmp_path, "../../etc/passwd")

    def test_deep_traversal_is_refused(self, tmp_path):
        with pytest.raises(AttachmentPathError):
            resolve_attachment_path(tmp_path, "2026/../../../etc/passwd")

    def test_absolute_path_is_refused(self, tmp_path):
        with pytest.raises(AttachmentPathError):
            resolve_attachment_path(tmp_path, "/etc/passwd")
