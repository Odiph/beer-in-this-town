# Contributing

## Setup

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -e ".[dev,browser]"
pytest -q -m unit
```

## Ground rules

**Tests are offline.** Anything marked `unit` must run with no network and no
browser. CI never touches Untappd or Google. If you need a fixture, paste the
markup into the test file rather than fetching it.

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
