"""Keep tests inside the sandbox, and prove it.

Two tests in this suite reached the real project directories before anyone
noticed. One wrote `state/last_run.json` into the live `state/`; the other read
whether `storage_state.json` happened to exist and passed locally for that
reason alone, then failed in CI where it does not.

Neither was a careless test. Both followed the pattern the suite already used
-- monkeypatch the module constant -- and that pattern is fragile by
construction: `LAST_RUN`, `GEOCACHE`, `JOURNAL` and friends are resolved at
import time from `config.STATE_DIR`, so patching `STATE_DIR` afterwards does
nothing to them, and patching one constant says nothing about the next one
somebody adds.

So this does not enumerate them. It walks the package for any module-level
Path that points into a real working directory and redirects it, which covers
the constants that exist today and the ones added tomorrow. The final sweep is
the part that matters: it fails a test that wrote into the real tree by any
route the redirect missed.
"""
from __future__ import annotations

import dataclasses
import importlib
import pkgutil
from pathlib import Path

import pytest

import beer_in_this_town
from beer_in_this_town import config

# The directories a test must never write into.
REAL_DIRS = ("DATA_DIR", "STATE_DIR", "CACHE_DIR", "DEBUG_DIR")

# Watched but not redirected: these sit at the project root rather than under
# one of the four directories above, so a write to them escaped the sweep
# entirely. `storage_state.json` is the user's live Google session.
EXTRA_WATCHED = ("logs", "storage_state.json")

# Watched, but never walked. `chrome-profile/` is about a gigabyte of browser
# cache; recursing it twice per test took the suite from 3s to 67s and told us
# nothing -- a test has no business touching it at all, so the directory's own
# mtime is a sufficient tripwire.
SHALLOW_WATCHED = ("chrome-profile",)


def _package_modules() -> list:
    mods = []
    for info in pkgutil.iter_modules(beer_in_this_town.__path__):
        # __main__ calls main() at import and would parse pytest's argv.
        if info.name.startswith("_"):
            continue
        try:
            mods.append(importlib.import_module(f"beer_in_this_town.{info.name}"))
        except Exception:  # optional deps (playwright) -- nothing to redirect
            continue
    return mods


def _real_roots() -> dict[Path, str]:
    return {getattr(config, name): name for name in REAL_DIRS}


def _stat(path: Path) -> tuple:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), -1, -1)


def _snapshot(paths: list[Path], shallow: list[Path] | None = None) -> set[tuple]:
    """Every watched file, with its size and mtime.

    A set of paths alone was blind to the two cases that matter most: a test
    that REWRITES an existing real file — the path set is unchanged, so the
    sweep saw nothing — and one that deletes it. Both are worse than leaving a
    stray file behind, because they damage state the user cannot regenerate.
    `state/pinned.json` records what has already been written to their Google
    account.
    """
    found: set[tuple] = set()
    for root in paths:
        if root.is_dir():
            found.update(_stat(p) for p in root.rglob("*"))
        elif root.exists():
            found.add(_stat(root))
    for root in shallow or []:
        if root.exists():
            found.add(_stat(root))
    return found


@pytest.fixture(autouse=True)
def sandbox_state(tmp_path, monkeypatch):
    """Redirect every real path into tmp_path, then prove nothing escaped."""
    # Capture before anything is redirected: config.STATE_DIR is itself one of
    # the attributes the walk rewrites, so asking afterwards returns the
    # sandbox and the guard would be checking tmp_path against itself.
    roots = _real_roots()
    watched = list(roots) + [config.ROOT / name for name in EXTRA_WATCHED]
    shallow = [config.ROOT / name for name in SHALLOW_WATCHED]
    before = _snapshot(watched, shallow)

    def redirect(original: Path) -> Path | None:
        """tmp_path equivalent of a path inside a real working directory."""
        for root, name in roots.items():
            if original == root:
                return tmp_path / name.lower()
            try:
                relative = original.relative_to(root)
            except ValueError:
                continue
            return tmp_path / name.lower() / relative
        return None

    for module in _package_modules():
        for attr, value in list(vars(module).items()):
            if not isinstance(value, Path):
                continue
            if (moved := redirect(value)) is not None:
                moved.parent.mkdir(parents=True, exist_ok=True)
                monkeypatch.setattr(module, attr, moved, raising=False)

    # Settings' path defaults are not module constants, so the walk above does
    # not reach them. Left alone, `Settings()` reports logged_in according to
    # whether this developer happens to have a session -- the exact difference
    # that passed locally and failed in CI.
    #
    # Setting the class attribute does NOT work: a dataclass bakes its defaults
    # into __init__, so the class attribute is only where they were declared,
    # never where they are read. Patch the signature instead. Doing it on the
    # shared function object is also what makes this reach tests that did
    # `from ...config import Settings` before the fixture ran.
    field_names = [f.name for f in dataclasses.fields(config.Settings)]
    defaults = list(config.Settings.__init__.__defaults__)
    for name, value in (("storage_state", tmp_path / "storage_state.json"),
                        ("profile_dir", tmp_path / "chrome-profile")):
        defaults[field_names.index(name)] = value
    monkeypatch.setattr(config.Settings.__init__, "__defaults__", tuple(defaults))

    yield tmp_path

    after = _snapshot(watched, shallow)
    added = {entry[0] for entry in after - before}
    removed = {entry[0] for entry in before - after}
    touched = sorted(added | removed)
    assert not touched, (
        "test created, modified or deleted files in the real project tree:\n  "
        + "\n  ".join(touched)
    )
