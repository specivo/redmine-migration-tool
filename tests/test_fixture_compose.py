"""The Redmine fixture keeps every checkout's data to itself.

A fixture with a fixed compose project name is shared by every checkout that
starts it: a second checkout reuses the first one's containers, while a
bind-mounted files directory still points into the first checkout. So the
files directory is a named volume (copied out with ``docker compose cp``), and
the project name is derived from the checkout's path by the Makefile.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.fixture.yml"
FILES_TARGET = "/usr/src/redmine/files"


def _compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())
    return data


def _mounts(service: dict[str, Any]) -> list[tuple[str, str]]:
    mounts = []
    for volume in service.get("volumes", []):
        if isinstance(volume, str):
            source, _, rest = volume.partition(":")
            mounts.append((source, rest.split(":")[0]))
        else:
            mounts.append((volume.get("source", ""), volume["target"]))
    return mounts


def test_compose_has_no_bind_mount_for_files():
    compose = _compose()
    redmine_services = {name: s for name, s in compose["services"].items() if "redmine:" in s.get("image", "")}
    assert set(redmine_services) == {"redmine-pg", "redmine-mysql"}
    for name, service in redmine_services.items():
        sources = [source for source, target in _mounts(service) if target == FILES_TARGET]
        assert len(sources) == 1, name
        (source,) = sources
        assert not source.startswith((".", "/", "~", "$")), f"{name} bind-mounts its files directory: {source}"
        assert source in compose.get("volumes", {}), f"{name}: {source} is not a declared named volume"


def test_compose_project_name_is_not_fixed():
    """A fixed ``name:`` would make two checkouts share one fixture."""
    assert "name" not in _compose()


def test_makefile_derives_the_project_name_from_the_checkout():
    makefile = (ROOT / "Makefile").read_text()
    match = re.search(r"^FIXTURE_PROJECT \?= (.+)$", makefile, re.MULTILINE)
    assert match is not None
    assert "CURDIR" in match.group(1)
    assert "-p $(FIXTURE_PROJECT)" in makefile


def test_databases_keep_no_state_on_disk():
    services = _compose()["services"]
    assert "/var/lib/postgresql/data" in services["redmine-db"]["tmpfs"]
    assert "/var/lib/mysql" in services["redmine-mysql-db"]["tmpfs"]


def test_host_ports_default_to_the_documented_ones():
    services = _compose()["services"]
    ports = {
        name: services[name]["ports"][0] for name in ("redmine-db", "redmine-pg", "redmine-mysql-db", "redmine-mysql")
    }
    assert ports == {
        "redmine-db": "127.0.0.1:${REDMINE_TOOL_FIXTURE_PG_PORT:-5452}:5432",
        "redmine-pg": "127.0.0.1:${REDMINE_TOOL_FIXTURE_PG_HTTP_PORT:-8034}:3000",
        "redmine-mysql-db": "127.0.0.1:${REDMINE_TOOL_FIXTURE_MYSQL_PORT:-3309}:3306",
        "redmine-mysql": "127.0.0.1:${REDMINE_TOOL_FIXTURE_MYSQL_HTTP_PORT:-8035}:3000",
    }
    readme = (ROOT / "README.md").read_text()
    for port in ("5452", "8034", "3309", "8035"):
        assert port in readme
