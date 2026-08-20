# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-21

First public release.

### Added
- **Scrape** Untappd venue listings for a location, including each venue's
  Venue Stats (total / unique / monthly / you check-ins).
- **Export** to CSV, and to KML for bulk import into Google My Maps.
- **Diff** against the previous run; only new venues are surfaced.
- **Pin** places into a real Google Maps saved list (UI automation — see the
  ToS caveat in the README).
- **Notes** pass: writes the Untappd stats into each saved place's note,
  dated by when the data was captured.
- **Agent contract**: every command speaks `--json`, emitting one envelope with
  a machine-readable error code, a remedy, and literal next actions.
  `status` and `doctor` report pipeline state so an agent never infers progress
  from logs. See [AGENTS.md](AGENTS.md).
- **Guardrails** for the account-writing paths: a rolling 24h write ledger
  persisted to disk, a circuit breaker, interstitial detection, and a
  persisted cool-off. All fail closed.
- Claude Code skill at `.claude/skills/untappd-maps/`.

### Notes on correctness
The parsing layer is built so that wrong numbers are impossible rather than
unlikely: selector misses raise, stats are matched by label rather than DOM
position, and a run aborts without writing anything if under 90% of venues
parse cleanly. Two bugs of exactly the class this guards against were found and
fixed during development — see the README and the regression tests.
