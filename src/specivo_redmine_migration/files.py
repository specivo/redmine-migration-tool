"""Locate an attachment's bytes in Redmine's storage directory.

Redmine's on-disk layout changed over the years. Files used to sit directly in
``files/``; modern instances put them in a ``YYYY/MM`` subdirectory recorded on
the row. Both are still found in the wild — often in the same instance, since
old files are not moved when the layout changes — so both are resolved.

The locator comes out of the source database, which is somebody else's system
and may have been edited by hand. Every resolved path is checked to be inside
the configured directory, so a crafted value cannot make the tool read
``/etc/passwd`` and attach it to an issue.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class AttachmentPathError(ValueError):
    """An attachment's stored location could not be turned into a safe path."""


def build_storage_key(disk_directory: str | None, disk_filename: str) -> str:
    """Combine Redmine's two location columns into one opaque locator."""
    directory = (disk_directory or "").strip().strip("/")
    filename = (disk_filename or "").strip()
    return f"{directory}/{filename}" if directory else filename


def resolve_attachment_path(files_dir: Path, storage_key: str) -> Path:
    """Return the path of *storage_key* under *files_dir*.

    Falls back to the flat layout when the dated one holds nothing, which is
    what an instance that predates the change looks like.

    Raises :class:`AttachmentPathError` when the locator is empty or would
    escape *files_dir*.
    """
    key = (storage_key or "").strip()
    if not key:
        raise AttachmentPathError("Attachment has no stored filename")

    base = files_dir.resolve()
    candidate = _within(base, key)

    if not candidate.exists():
        # An older file may sit at the top level even on an instance that has
        # since moved to dated directories.
        flat = _within(base, Path(key).name)
        if flat.exists():
            return flat

    return candidate


def _within(base: Path, relative: str) -> Path:
    """Resolve *relative* under *base*, refusing anything that escapes it."""
    candidate = (base / relative).resolve()
    if candidate != base and base not in candidate.parents:
        raise AttachmentPathError(f"Attachment path escapes the source files directory: {relative!r}")
    return candidate
