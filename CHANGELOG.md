# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `verify`: test both accounts for real from the CLI — headless, read-only,
  always returns. The dashboard blocks on a person, which is right for a
  sign-in and wrong for everything else: an agent still needs to know whether
  the sign-in took, and had no way to ask. `ran` is reported beside `ok`, so
  "the check could not run" is never read as "signed out".
- **Fixed a non-terminating agent loop.** `status` handed a signed-out user
  `selfcheck`, and nothing selfcheck does changes the session — so the loop in
  AGENTS.md ran it forever. It now returns an empty `next_actions`, which that
  document already defines as the end of the loop, plus a new
  `data.blocked_on` naming *why* it ended: `null` for finished, `"sign_in"`
  for waiting on a human. Additive field, no schema bump.
- A bare `beertown` opens the dashboard. Typing the program's name is the
  first thing a new user does, and it used to answer with an argparse error --
  a poor first impression from a tool whose whole first-run story is a
  dashboard that explains itself. Guarded three ways, because `ui` blocks
  forever: never with `--json`, never when output is piped, and never in place
  of a real command or a typo, which still gets the error it asked for.
- `ui` leads rather than reports: one directive at a time — install Chrome,
  sign in, test, then name your city — chosen server-side in `next_step` so
  the ordering is testable. Sign-*up* links sit beside sign-in, because
  someone with no Untappd account cannot sign in to one. The last step takes
  a city and hands over the exact `run` command, `--no-upload` included; the
  run itself stays in the terminal where it can be watched and stopped.
- `status` no longer sends a brand-new user to `run`. The old first action
  assumed a session was only needed for the YOU column; signed out of
  Untappd, search stops at 5 results, so that run built a five-venue corpus
  and reported a finished scrape — with the diff, baseline and KML all wrong
  together. `selfcheck` is the first action now, and `ui` is the hint.
- `doctor` reports a saved session as `present (untested)` and says so, rather
  than implying a file on disk is a working login.
- `ui`: a localhost setup dashboard for a first-time user. Connects Google and
  Untappd in one Chrome window, then **verifies both with a real round-trip**
  before calling either connected — a headless Maps load for Google, one
  authenticated request for Untappd. Detection and verification are separate
  tiers and the panel says which one a row rests on, because a cookie on disk
  is evidence a login once happened, not that the account works now. Until
  this, a stale Untappd session first announced itself as
  `search_login_required` a hundred requests into a run.

  Every slow action narrates itself as it runs — including the pacing waits,
  so a deliberate delay does not read as a hang. One job at a time: two
  sign-ins sharing one Chrome profile can corrupt it.

  `profile_has_untappd_session` is new, and answers yes/no/**unknown**: the
  cookie-name list behind it is a guess rather than something verified against
  a live login, so a miss reports as unknown instead of as "signed out".

  The server can capture a Google session, so it is locked down accordingly:
  `127.0.0.1` only, a per-start key required on every API call and delivered
  in the URL fragment, a `Host` allow-list against DNS rebinding, and
  cross-site `Origin` refused. No route on it can `pin` or write `notes` —
  that absence is the guardrail the other four back up. Stdlib only.
- `closures`, and `run --check-closed`: ask the Google Places API whether a
  venue still trades, and record it in a new `business_status` CSV column.
  Untappd's venue database is append-only in practice, so a bar that shut in
  2019 keeps its stats and outranks a good one that opened last year — the
  authoritative second tier `classify.looks_closed` was written waiting for.
  Opt-in behind `GOOGLE_PLACES_KEY`, deliberately separate from
  `GOOGLE_GEOCODING_KEY`: sending an address to place a pin and sending it to
  ask what business is there are different disclosures.

  The load-bearing rule is that **a missing match is not a closure**. Only an
  explicit `CLOSED_PERMANENTLY` or `CLOSED_TEMPORARILY` closes a venue;
  no match, a timeout, or a status Google adds later all leave it visible and
  recorded as `unmatched`. A failed lookup collapses four cases that want
  opposite outcomes, and a false closure silently deletes a real bar from the
  map — the failure the quality gate and the fail-closed guardrails exist to
  prevent. Closed venues are flagged, never dropped: `run` uploads to My Maps,
  so a silent deletion would be invisible.

  Internals are shaped for #20 rather than for #7 alone — one lookup, one
  cache, one set of failure semantics. `types` and the place id are fetched
  and cached alongside the status at no extra cost (they are Essentials
  fields; `businessStatus` is what makes the request Pro), so #6 and #8 need
  no second call and no second cache. Cached in `state/places_cache.json`,
  written even when a run aborts, because those lookups were already billed.
  Closes #7.
- `label` and `score`: a measurement harness for the venue heuristics, so
  thresholds stop being guesses with numbers attached. `classify.py` holds
  candidate rules for venue kind, closed venues and private spaces, and is
  deliberately **not** wired into `run` — a classifier merged without
  measurement produces plausible output, is wrong at an unknown rate, and
  nothing raises, which is the failure `corpus_quality_gate` and the raising
  selectors exist to prevent. Sampling takes a fixed quota per bucket, rare
  ones included, and scoring reweights by inverse sampling probability so the
  numbers describe the whole scrape. Error rates are reported by direction —
  a private space kept is someone's front door on a shared map; a real venue
  dropped costs one bar to re-add — and never averaged into one accuracy
  figure.
- `--format kml,geojson,gpx` on `run`. The everyday-map use case — the thing
  the project is actually for — had only two routes: a My Maps layer that gives
  up the everyday-map pins, or `pin`, which gets them back by automating a UI
  Google's terms say not to automate. Organic Maps, OsmAnd and every OSM-based
  client import GPX and GeoJSON as bookmarks on the everyday map, offline, with
  no account. The gap "there is no API for this" documents is real for Google,
  not for every map. Default is unchanged (`kml`), and an unknown format fails
  at the boundary with `bad_format` rather than writing no map quietly.

### Fixed
- `notes` had no pre-flight. Signed-out Google Maps loads perfectly happily, so
  a signed-out profile either read every place as "not in the list" — a hundred
  page loads, `ok: true`, and a warning telling the user to run `pin` first,
  which was the wrong diagnosis — or tripped the block detector and started a
  six-hour cool-off that also blocks `pin`, for a condition `pin` itself
  reports as `not_signed_in` with no cool-off at all. It now runs the same
  pre-flight `pin` does, and maps its errors the same way.
- `robots_disallows_scraping` swallowed a 403. That is a block already in
  progress, and swallowing it read as "robots does not forbid this", so the run
  carried on requesting into the block — the one move the module's own 403 rule
  says never to make.
- A dead network made `run` sleep instead of stopping: three retries over
  60/180/600s per venue, swallowed per venue, so a hundred venues meant roughly
  twenty-three hours of sleeping before the corpus gate failed. Three
  consecutive transport failures now stop the run with `network_unavailable`.
- Nominatim's rate limit was skipped exactly when it mattered. The pause sat
  after the call inside the `try`, so a timeout or a 429 skipped it and
  consecutive failures hit OSM back to back.
- `status` could not see the write guardrails that several error remedies send
  the caller to it to check — cool-off, budget, and whether another run holds
  the lock. It reports all three now, and the contended-lock message no longer
  ends by inviting a manual delete of the lock `AGENTS.md` says not to delete.
- The agent contract steered an agent into the account write it forbids.
  `AGENTS.md` says to run the first `next_action` and repeat until the list is
  empty, and also that `pin` must never run without a human asking. `pin` was
  listed in `next_actions` and was the only entry that ever emptied it, so
  following the contract led an agent into the ToS-crossing write. `pin`,
  `notes` and `bootstrap` are out of `next_actions` entirely; an empty list
  now genuinely means the safe work is finished.
- `next_actions` no longer carries `#` comments. It promises literal runnable
  commands, and an agent passing argv as a list got `#` as an argument while
  one that shelled out silently no-opped and looped on the same suggestion.
  Prose moved to a new additive `hints` field that nothing executes.
- `--region` defaulted to "Singapore" for every city, so a London CSV searched
  Maps for "..., London, Singapore". The guard exists precisely so a bare name
  cannot match a venue in the wrong country. It is now read from the CSV's own
  city column, and when that cannot be established the run says the guard is
  off rather than quietly appending nothing. An explicit `--region ''` still
  switches it off and is no longer collapsed back into re-derivation.
- `pin` identified the target saved list by substring, in all three places it
  checks one: picking the row in the list picker, verifying what Maps said
  afterwards, and confirming the list exists at all. An account holding both
  "Bars" and "London Bars" could therefore have a place saved into the wrong
  one **and have that verified as correct** — `"bars" in "london bars"` is
  True — after which the run journalled `ok` and never retried it. That is the
  wrong-list failure `pin_to_list.py`'s own docstring says the module exists to
  prevent. All three now compare whole names, ignoring only case, padding and a
  trailing place count. A name that matches no list, or several, raises
  `list_ambiguous` and saves nothing rather than clicking the nearest label.
  `notes` used the same substring test and is fixed with it.
- `score` reported numbers that did not mean what they said. A dropped venue
  was judged by "was this a real venue", which every café in the `non_beer`
  bucket is — so with every label correct the harness reported that 100% of
  dropped venues were dropped wrongly. A drop is now judged against the claim
  the classifier actually made about that bucket. Found by an independent
  review of the harness; caught nothing because the tests only ever built two
  buckets, neither of them `non_beer`.
- Every rate's denominator is now the rows that answered *that* question. A
  blank counted as "no error", so a sheet where only `true_kind` was filled
  reported a clean corpus nobody had looked at.
- `?` is an abstention rather than a verdict. It counted as "private",
  inflating the expensive rate, while an unrecognised value like `closed`
  counted as "not closed" and deflated the cheap one — silently, in the tool
  built to stop exactly that. Unknown values now fail `labels_unusable`
  naming the row and the cell.
- `unsettled` predictions are excluded from kind accuracy instead of scored as
  wrong, so the metric stops measuring how often the category line was blank.
- The labelling sheet survives a spreadsheet. The bucket sizes rode in a `#`
  comment on line 1; Excel and Sheets parse that as CSV and write it back
  mangled, after which `score` failed with a remedy that failed the same way.
  They now ride in a `_stratum_size` column, and a sheet saved in the system
  codepage is read as cp1252 rather than crashing.
- `selfcheck` could not see a search outage. It fetched one venue detail page,
  which is server-rendered and was unaffected when Untappd moved search to
  Algolia, so it returned `ok: true` for the whole time every `run` was
  failing. `AGENTS.md` sells it as the cheap early warning for
  `selectors_stale` and the agent loop leans on it before committing to a run;
  an early warning that cannot see the most likely failure turns "I don't know"
  into a false "fine". It now also probes the search page and asserts it is one
  of the two shapes the code handles. `--skip-search` keeps the old
  one-request behaviour.
- State artifacts were global where they describe one city or one list, so a
  second city could not be scraped without damage. `previous_run.json` was a
  single baseline: a London run diffed itself against Singapore — every venue
  "new", every Singapore venue "gone" — and then overwrote the only copy of
  the Singapore history. Baselines are now per-query, journals per-list.
- `status` answered from `Settings()` defaults, because it is not one of the
  commands that derive settings from argv. After `run --query london` it
  reported Singapore and offered a literal `pin` command aiming the London CSV
  at a Singapore list, with `--region Singapore` on the lookups — a write to a
  live account, from the command documented as read-only. `run` now records
  what it did and `status` reads it.
- A place saved into one list was recorded as done for every list, so building
  a second list from the same CSV silently skipped the overlap. The pre-scoping
  journal does not record which list it belonged to, so it is adopted by
  exactly one list, by rename, with a warning naming the assumption.
- `status` reads an unscoped journal for reporting rather than showing zero
  saved places to someone who has dozens, and labels it as pre-upgrade.
- A rejected or unbilled geocoding key degraded quietly. `_google` raised on
  REQUEST_DENIED and OVER_QUERY_LIMIT with a comment saying to surface them
  rather than quietly degrade, and its only caller wrapped every call in
  `except Exception: continue`. The result was a KML with a handful of pins
  instead of a hundred, reported as a successful run. Geocoder failures are now
  their own exception, abort before anything is written, and report
  `geocoder_unavailable`. Per-venue misses stay swallowed, as they should be.
- Every lookup failing is now treated as systemic rather than as a hundred
  unlucky addresses, so a blocked or unreachable Nominatim — the default path,
  with no key involved — fails loudly too.
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
