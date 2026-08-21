# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Search-result cards were read positionally, so a venue with no category line
  had its address filed as its category and its city filed as its address — a
  CSV that looked entirely reasonable and was entirely wrong. Lines are now
  anchored on the location line, which is always last, and a field that cannot
  be established is left empty rather than guessed.
- The rate ledger had no cross-process lock. Two runs starting together — the
  nightly catch-up task and a hand-run `pin`, say — each read the full
  remaining budget and each spent it, and whichever wrote last discarded the
  other's events entirely. `pin` and `notes` now hold an exclusive lock for the
  length of a run and fail closed if one is already held.

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
- Claude Code skill at `.claude/skills/beer-in-this-town/`.
- **Prerequisites** section in the README: Python version, when a system Chrome
  is actually needed, which account each command requires, and the two optional
  geocoding environment variables (`GOOGLE_GEOCODING_KEY`, `NOMINATIM_EMAIL`).
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1) and a Dependabot config for
  pip and GitHub Actions.

### Fixed
- A scheduling snippet in the README lost the `\r` of `\run_catchup.ps1` to
  escape processing, splitting the path across two lines and breaking the
  copy-paste. Rewritten with `Join-Path`, which has no backslash to lose. The
  same block registered the task under a name that did not match the one
  documented elsewhere.
- `doctor` suggested `pip install -r requirements.txt` while the README
  documented an editable install; the two now agree.

### Notes on correctness
The parsing layer is built so that wrong numbers are impossible rather than
unlikely: selector misses raise, stats are matched by label rather than DOM
position, and a run aborts without writing anything if under 90% of venues
parse cleanly. Two bugs of exactly the class this guards against were found and
fixed during development — see the README and the regression tests.

[Unreleased]: https://github.com/Odiph/beer-in-this-town/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Odiph/beer-in-this-town/releases/tag/v0.1.0
