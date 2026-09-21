# Contributing

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
See the README for [prerequisites](README.md#prerequisites) — Python 3.11+ and,
for anything browser-driven, a system Chrome.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,browser]"
pytest -q -m "not integration"
ruff check beer_in_this_town tests
```

That is exactly what CI runs. **You do not need the emulator** (BlueStacks,
`adb`) or a signed-in Chrome for the tests: `adb` and the app are faked, and
nothing in the suite touches the network. The emulator is only needed to run
a real `sweep`. Tests marked `integration` do touch a browser or the network
and are never run in CI.

New test files set `pytestmark = pytest.mark.unit` at the top.

## Releasing

The version lives in one place, `beer_in_this_town/__init__.py`
(`__version__`); `pyproject.toml` and the User-Agent strings read it. Bump it,
add a `CHANGELOG.md` entry, and push a `vX.Y.Z` tag matching it. The release
workflow builds the sdist and wheel, checks the sdist carries nothing private,
and attaches both to a GitHub Release. Nothing is published to PyPI.

## Ground rules

**Tests are offline.** Anything not marked `integration` must run with no
network, no browser and no emulator. CI never touches Untappd or Google. If you need a fixture, paste the
markup into the test file rather than fetching it.

**Check your change reaches the code path.** The most common bug in this repo
by a wide margin is an exception raised deliberately in one function and
swallowed by a broad `except` in its caller — it has shipped five times. Before
claiming a fix works, trace the raise to its handler. The `verify-change` skill
in `.claude/skills/` is that check written down.

**Parsers fail loudly.** Never add a silent fallback to a selector. If the
markup might be missing, raise `ParseError` and dump the HTML to `debug/`. A
scraper that returns wrong numbers is worse than one that stops.

**Respect the pacing.** The rate limits in `config.py` exist to keep users'
accounts alive. PRs that raise concurrency or lower delays need a good reason.

**The envelope is a public API.** Changing the shape of the JSON in
`agent_io.py` breaks every agent driving this tool. Bump `SCHEMA_VERSION` and
say so in the PR.

## Adding a site

The parsing layer is deliberately separate from the transport layer. To support
another venue site, add a module alongside `parsers.py` with the same contract:
strict selectors, label-driven extraction, a corpus quality gate.

## Style

`ruff check beer_in_this_town tests` must pass. Type annotations on function
signatures. Immutable dataclasses for data that crosses a boundary.

## Legal

Don't add features that require forging authentication headers or replaying
undocumented internal RPCs. If a capability needs that, it doesn't belong here
— document the gap instead, as the README does for Google Maps saved lists.

## The demo GIF

`docs/demo.gif` is generated, not hand-made:

```bash
python tools/make_demo_gif.py
```

Every line in it is copied from a real run. If you change the CLI output,
update the script rather than leaving the GIF to drift — a demo that shows
output the tool no longer produces is worse than no demo.
