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
- Breaking an abandoned lock was itself not exclusive: two processes could both
  judge a lock stale and both take it, since the re-open could not fail. The
  break is now an atomic rename followed by an exclusive create, so exactly one
  wins and the other stops.
- Releasing a lock no longer unlinks whatever file happens to be there. A run
  whose lock had been taken over would delete the new owner's, quietly leaving
  mutual exclusion switched off. Ownership is proven by a token in the lock.
- A held lock is refreshed on every write, so its age means "idle this long"
  rather than "started this long ago". Without that, a slow but healthy run had
  its lock stolen while it was still spending budget.
- Lock contention now reports `already_running` rather than
  `guardrail_tripped`, whose remedy told the caller to wait out a cool-off that
  does not exist while `status` reported all clear — a loop an agent could spin
  on.

- The User-Agent claimed Chrome 128 while the installed Chrome was 151. The
  comment beside it said to keep it in sync because a stale UA is a cheap tell,
  and nothing made that happen. It is now derived from the Chrome actually
  installed, falling back to a constant when none can be found.
- `pin`'s pacing was declared twice and had drifted: `pin_to_list` said 4–9s
  while the CLI passed 8–16s, so the module a reader would consult to find out
  how hard the tool hits Google gave half the real figure. The module constants
  are now the single source and the CLI derives its defaults from them.

### Changed
- The measures that keep scraping unobtrusive are now enumerated in one place,
  in `http_client.py`'s module docstring: what each one defends against, why
  jitter and a hard hourly ceiling are not interchangeable with slowness, and
  what raising any of them costs. `guardrails.py` gained the matching pointer
  for the write side, and `config.py`'s knobs each say what they are for.
- Search cards with a single style line are left unassigned rather than filed
  as a city: `"Beer Bar"` and `"Singapore, Singapore"` are the same shape, and
  guessing put venue types in the city column. The last line is also checked
  for a street number before being accepted as the location line, and the
  category-vs-address judgement is logged when it fires.

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
