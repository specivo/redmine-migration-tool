"""Shared helpers: a sample Redmine to export, and a way to read the bundle back."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests import sample_redmine


@dataclass
class Bundle:
    """A bundle read back from a directory: manifest, lookups and every record file."""

    root: Path
    manifest: dict[str, Any]
    lookups: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    public_access: dict[str, Any] | None = None
    records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def get(self, stem: str) -> list[dict[str, Any]]:
        return self.records.get(stem, [])

    def by_ref(self, stem: str) -> dict[str, dict[str, Any]]:
        return {record["source_ref"]: record for record in self.get(stem)}


def read_bundle(root: Path) -> Bundle:
    bundle = Bundle(root=root, manifest=json.loads((root / "manifest.json").read_text()))
    if (root / "lookups.json").exists():
        bundle.lookups = json.loads((root / "lookups.json").read_text())
    if (root / "public_access.json").exists():
        bundle.public_access = json.loads((root / "public_access.json").read_text())
    for path in sorted(root.glob("*.jsonl")):
        bundle.records[path.stem] = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return bundle


@pytest.fixture
def sample(tmp_path: Path) -> sample_redmine.SampleRedmine:
    return sample_redmine.build(tmp_path / "source")


@pytest.fixture
def export(sample: sample_redmine.SampleRedmine, tmp_path: Path):
    """Run the command over the sample and return the bundle it wrote."""
    from specivo_redmine_migration.cli import main

    counter = iter(range(1000))

    def run(*extra: str, files: bool = True) -> Bundle:
        out = tmp_path / f"bundle-{next(counter)}"
        argv = ["--db-url", sample.url, "--out", str(out), "--quiet", *extra]
        if files:
            argv += ["--files-dir", str(sample.files_dir)]
        assert main(argv) == 0
        return read_bundle(out)

    return run
