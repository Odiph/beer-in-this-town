"""What ships is an allowlist, and the version has one source.

The repo root holds a signed-in Chrome profile, `storage_state.json`, `state/`
and `data/`. A denylist forgets the next private directory; an allowlist
cannot. This reads `pyproject.toml` rather than building, so it runs offline;
the release workflow does the real build. No network.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import beer_in_this_town

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

PRIVATE = (".claude", ".local", "state", "data", "cache", "chrome-profile",
           "storage_state.json", "debug", "logs", ".venv")


def _sdist() -> dict:
    return PYPROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]


def test_sdist_is_an_allowlist():
    assert _sdist().get("include"), "sdist must list what it includes"


@pytest.mark.parametrize("private", PRIVATE)
def test_sdist_allowlist_names_nothing_private(private):
    for entry in _sdist()["include"]:
        top = entry.strip("/").split("/")[0]
        assert top != private, f"{entry!r} would ship {private}"
        assert "*" not in top, f"{entry!r} is a wildcard at the root"


def test_version_is_dynamic_from_the_package():
    assert "version" not in PYPROJECT["project"]
    assert "version" in PYPROJECT["project"]["dynamic"]
    path = PYPROJECT["tool"]["hatch"]["version"]["path"]
    source = (ROOT / path).read_text(encoding="utf-8")
    found = re.search(r'^__version__ = "([^"]+)"$', source, re.M)
    assert found and found.group(1) == beer_in_this_town.__version__
