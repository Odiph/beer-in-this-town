# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-09-26

### Added
- **`sweep --method search`**, beside the app-map sweep, which stays the
  default (`--method map`). Untappd's signed-in web
  search, once per spelling of the city's name, most-checked-in venues
  first (`--top`, up to the site's 1,000). No emulator. Measured against
  the map sweep: London's search held 99 of the map's top 100 venues by
  check-ins for 50 requests; Tel Aviv's held all of its top 100 beer
  venues, where the map had 20. `status` offers the method the dashboard or the
  last run chose, and the map when nobody chose.
- **City name variants** (`city_names.py`): which spellings to search, from
  Foursquare's open places data inside the city's boundary -- nothing under
  2%, at most ten, cached per city. Needs the new `search` extra; without it
  the city as typed is searched, with a warning.
- **`enrich` fetches a search sweep's venues by id.** No name is guessed; the
  position comes from the page, and a page outside the city is dropped as
  the new status `outside` (a namesake: "london" also finds New London, CT).
- `status` reports `data.method`, probes the emulator only for a map sweep,
  and names the method in the `sweep` command it offers.
- **The dashboard asks how to find venues** with the city: search or the
  app's map. Only a map choice sends the person to the emulator setup, and
  the Build step's sweep command and explanation follow the choice.
- A search whose results stopped loading part-way is reported per spelling
  (`complete: false`, and a warning), never passed off as the less-visited
  tail, and never cached, so a re-run retries it.

### Fixed
- **`--json` output crashed on a Windows console** when the envelope held
  non-Latin text (a Hebrew city name, say): cp1252 cannot encode it, and
  the command raised after doing its work. Envelopes are now printed with
  non-ASCII escaped, which is the same JSON.
- **`notes` could not find Google Maps' note box, and would have written
  into the wrong list's.** Found live 2026-09-24: Maps moved the note under
  the "Saved in" row, which starts folded, with one box per list the place
  is saved in. `notes` timed out on every place; had it found a box it took
  the first, and The Rake's first could as well have been a shared list's.
  It now unfolds the row and writes only into the box whose block names the
  target list. When there is no such box it types nothing, spends no write,
  records `no-note-box` and fails the run (`data.no_note_box`), and three in
  a row trip the breaker. The folded row is found by its own button
  (`aria-expanded="false"`), because the reload that verifies a note folds
  it again: before that, a note that had landed read back as a failure.
- **`notes` no longer adopts the old unscoped `state/noted.json` by
  guessing.** It renamed a Singapore journal into the journal of "London
  Bars Test", the bug `pin` had until 2026-09-22. Same rule now: never
  adopted, and the warning says how to adopt it.

- **`verify` reported Google signed out while `pin` was signed in.** Two
  places hold the session -- the cookie snapshot `enrich` reads with, and
  the Chrome profile `pin` drives -- and Google rotates its cookies, so the
  snapshot goes stale. `verify` now asks the profile when the snapshot says
  no, refreshes the snapshot from it when the profile is signed in, and
  still treats a locked profile as "could not check" rather than a verdict.
- **The dashboard asked you to re-prove a failure.** With `verify` having
  just shown both accounts signed out, the page still offered "Test both
  accounts" and hid the sign-in button behind it. A recorded *failure* now
  seeds the page; a recorded success is still re-proved.
- **Refreshing the dashboard logged you out of it** (401): the key is read
  from the URL fragment, which is cleared from the address bar. It is kept
  for the tab now.
- The account rows said "NOT TESTED" beside the result of a test. A failed
  check reads "tested — signed out".
- `test_writing_through_a_redirected_constant_lands_in_the_sandbox` asserted
  that the real `state/last_run.json` does not exist, which is only true on
  a machine that has never run the tool. It now asserts the real file is
  untouched.

## [0.3.1] - 2026-09-22

### Fixed
- **`pin` spent about 2.5 writes per place.** After saving it waited a fixed
  1.5 s and read the "Saved in ..." line; when Maps rendered that line a
  moment later, a successful save read as "not saved" and was retried --
  against a 100-a-day budget. It now waits for the line (up to 15 s) instead
  of guessing, and the list picker and place panel get longer to appear
  (25 s and 40 s): both timed out on real London places.

### Changed
- The README leads with what the tool is for rather than how it works, and
  "Where the lines are" is now "Using other people's services": the same
  facts, framed as what to think about before running the parts that touch
  your accounts.

## [0.3.0] - 2026-09-22

### Fixed
- **`pin` could not find any saved list.** Google Maps' picker rows now
  read "<icon> / <name> / Private · 0 places", and the whole text was
  compared to the name. The name line is taken now, and still matched
  exactly.
- **`pin` could undo a save the person made themselves.** Google joins list
  names as "A & B"; split on commas alone, a correct save into a place that
  was already in another list read as a wrong-list save, and the undo path
  aimed at that other list. Joined names are read, and an undo now removes
  only a list the same attempt added.
- **A pre-scoping `pinned.json` was adopted by whichever list ran first,**
  silently relabelling one list's history as another's (live: a Singapore
  journal of 103 places became a London list's). It is never adopted now;
  `pin` says how to rename it by hand.
- **A large sweep could misplace most of a city.** Found on London: in
  dense areas the pan measurement is wrong often enough that the camera
  drifted, and from a quarter of the way in every cell was 6-15 km off. The
  global fit dropped those venues as outliers but still exported them where
  the camera thought they were. Each venue now records its cell, and each
  cell is shifted by the offset its own OpenStreetMap matches agree on
  before the global fit; sparse cells carry the last measured shift. The
  sweep result reports `cells_anchored`, `cells_carried` and
  `max_cell_shift_m`.

### Added
- **`enrich --limit N`, and a journal.** Every venue is saved as it
  resolves, so a stopped enrich continues where it stopped and looks nothing
  up twice; `--limit` runs a city in batches, each repeating itself in
  `next_actions` until `remaining` is 0. Found live: a London enrich was
  stopped three times, once at venue 391 of 629, with nothing written.

### Changed
- **`enrich` paces like a person looking venues up.** Between venues it
  pauses 6-15 s, and every 20-40 venues it takes a 2-6 minute break. It only
  slows the run; the per-request floors and the hourly ceiling are unchanged.
  A large city takes hours.

## [0.2.0] - 2026-09-22

The source and the destination both changed. Venues now come from a sweep of
the Untappd Android app's map, which is geographic, instead of Untappd's web
search, which matches names; and they go into a Google Maps saved list (see
Removed). The flow is one command per stage, documented step by step
for a person in `docs/FLOW.md` and for a coding agent in `AGENTS.md`.

### Added
- **The flow, one command per stage**, each with a `--json` envelope and each
  reading the previous stage's file from `--city` (or `--in PATH`), under
  `data/<slug>/`:
  - `sweep` reads the Untappd app's map over `adb` on an emulator, splitting
    the map wherever a search comes back at the ~60-venue cap (`--min-depth 1
    --max-depth 3` by default, the measured recommendation), with the app's
    drinking-places filter on, and places every pin by fitting the map's scale
    against OpenStreetMap. `--here` centres on the emulator's GPS instead of a
    city search. Writes `1_sweep.csv`; resumes from a per-city journal.
  - `enrich` matches each swept name to its Untappd venue page, accepting a
    page only when its published coordinates are within 1 km of the pin.
    Writes `2_enriched.csv`; unmatched venues stay, with unknown counts.
  - `filter` keeps the beer venues and writes the rest to `3_excluded.csv`
    with a reason each. `3_venues.csv` is what `pin` and `notes` read.
  - `export` writes `venues.kml`, `venues.gpx` and `venues.geojson`.
- **Emulator checks** in `doctor` -- `adb_on_path`, `device_connected`,
  `untappd_installed` (`com.untappdllc.app`), `screen_size` (900x1600) --
  each with a remedy a stranger can follow, reported as `data.emulator`
  (a list of `{name, ok, detail, remedy}`) and `data.emulator_ready`.
  `sweep` runs the same checks first and refuses with `emulator_unavailable`.
- `status` reports `blocked_on: "emulator"` when a sweep is next and the
  checks fail, with an empty `next_actions` rather than a `doctor` that
  would loop. `blocked_on` precedence is `sign_in`, then `choose_city`, then
  `emulator`. `sweep` is offered only after `verify` has passed.
- New error codes: `emulator_unavailable`, `calibration_failed`,
  `city_not_found`, `stage_input_missing`. `--here` without the
  map's Reset location control is `app_screen_unexpected`.
- Commands built from a city or list name strip shell metacharacters
  (`" ` $ ; & | < > % ^ !` and control characters) instead of escaping them,
  since the same text is pasted into PowerShell, cmd and POSIX shells. Name
  the Google Maps list with plain letters, digits and spaces.
- The setup wizard gained the emulator setup, the app sign-in, a copyable
  command per stage with its live status, and the saved-list and pin-trial
  instructions. It still has no route that can `pin` or write `notes`.
- `docs/FLOW.md`: the whole flow for a stranger, every manual step included
  (BlueStacks, the display setting and why, ADB, platform-tools on each OS,
  sign-ins, the saved list), measured quality numbers, limits, and
  troubleshooting by error code. `AGENTS.md` gained the matching playbook,
  with the exact message to give the human at each manual step.
- `docs/HARVESTING.md`: the research record behind the sweep.
- OpenStreetMap (ODbL) is credited in the README and the exports.

### Removed
- **`run`**, and with it **Untappd web search** as a source of venues. It
  matches venue names, not places: a Tel Aviv run returned 21 venues more than
  100 km away and missed bars in the centre. Venue *pages* stay; `enrich`
  reads them. `search_login_required` went with it.
- **The My Maps upload**, from the CLI and the wizard. The destination is a
  Google Maps saved list; KML is still exported as a file.
- `run_weekly.ps1`, `run_catchup.ps1` and `docs/scheduling.md`. Nothing is
  scheduled any more; each write session is the user's decision.
- `selfcheck`'s search probe. It checks a venue page only.

### Changed
- **The destination is a Google Maps saved list**, filled by `pin` and
  annotated by `notes`, both still human-triggered, trial-first and behind the
  unchanged write guardrails. They are never in `next_actions`; their exact
  command text is in `hints`.
- **Envelope `schema_version` is now `"2.0"`** (breaking): a command was
  removed, and `status`'s `data.stages` changed meaning. It is now per city
  -- `sweep`, `enrich`, `filter`, `export`, `pin`, `notes`, each with `done`,
  `count` and `detail`, the collection stages also with `stale` and `path` --
  alongside the new `data.city`, `data.city_dir`, `data.next_stage` and
  `data.emulator`.
- `next_actions` only ever offers `status`, `verify`, `sweep`, `enrich`,
  `filter`, `export`, `ui --detach` (for a sign-in or a city choice), and
  `score` after `label`.
- `filter` applies `classify.craft_beer_decision`: category vocabulary,
  `looks_private` excluded, closures only flagged.
- The README's prerequisites are honest: BlueStacks, adb and both accounts are
  required, not optional. "Where the lines are" gained a row for automating
  the Untappd app, and states plainly that it and `pin`/`notes` are against
  the respective terms of service.

### Fixed
- **Namesakes in enrichment.** A common name's far-away namesakes could fill
  the two page fetches allowed per venue, so the right page was never opened
  (Tel Aviv: `Mike's Place`, `Django`, `Oscar Wilde`). Search results are
  now ranked by the location line on each result card before any page is
  fetched: cards naming the city first, cards with no location next, cards
  naming another place last. When no card names the city, one
  city-qualified search (`Mike's Place Tel Aviv`) is tried before a fetch is
  spent.
- **CI test selection.** CI ran `-m unit`, which skipped every test that was
  never marked -- 187 of 647. It now runs `-m "not integration"`.

Found by the first live end-to-end run of this flow:

- **Stock BlueStacks was refused.** `doctor` read `wm size` (the physical
  1600 x 900) and called a working emulator not ready. Untappd is
  portrait-only, so on that screen it is drawn at exactly the 900 x 1600 the
  sweep is calibrated for. Either orientation is now accepted, and the setup
  says 1600 x 900 is the default rather than sending people to change it.
- **A placement failure cost a whole second sweep.** A complete sweep that
  then hit an Overpass 504 was walked again on re-run, because a resumed
  journal re-walks cells by design. The journal is now marked complete before
  placement; a re-run within 12h places it without touching the emulator, and
  `sweep --fresh` walks the map again. Journals older than 12h are discarded,
  so last week's venues no longer seed this week's sweep.
- **The calibration census was too big to answer.** A depth-3 sweep spread
  17 km, and the matching Overpass query timed out on two servers. It is
  capped at 8 km (anchors from the core fix the whole map) and retried once
  at half the radius on a refusal.
- **Shell syntax in commands built from names.** A city or list name went
  into `next_actions` and the dashboard's copyable commands with only double
  quotes removed. Quote, backtick, dollar, separators, redirects, percent,
  caret, bang, control characters and a trailing backslash are now all
  removed.
- **`pin` and `notes` fell back to a list name nobody typed** (the dashboard's
  "<City> Bars" suggestion), so `no_list` could not fire. They now need
  `--list`.
- `enrich`, `filter` and `export` now find the chosen city the way `sweep`
  does.

The entries below were written during the 0.2.0 cycle, before the flow above
replaced the old collection command, and describe the tool as it was at the
time. Anything they mention that is listed under Removed above is gone.


### Added
- **Removed the default city, and the default list name.** `run` used to
  default to `singapore` and `pin`/`notes` to `"Singapore Bars"`. A default
  there saves nobody a keystroke; it silently answers a question only the user
  can answer, and answers it with a scrape of somewhere they have never been —
  or, for `pin`, a write into a list they did not name. `run` now refuses with
  `no_city`, `pin`/`notes` with `no_list`, and `status` reports
  `blocked_on: "choose_city"` until somebody says where. Same reasoning as
  `logged_in`: a stand-in for a decision reads exactly like the decision
  having been made.
- **The browser search path did not encode the query.** It built the URL with
  an f-string, so `&` in a city started a new parameter and `#` turned the
  rest into a fragment — "rock & roll" searched for "rock ", returned results,
  and looked perfectly healthy. The HTTP path always passed `params=`; the
  browser path is the one that actually runs now that Untappd's search is
  client-rendered.
- The city step explains what the query decides: it is Untappd's venue search,
  it names the diff baseline for next time, and it derives the Maps list name.
  Every line is a mechanical consequence someone can check, not advice about
  what makes a good night out.
- README covers the wizard, with a screenshot of the first screen and a GIF of
  the last step.
- **The city typed in the dashboard reaches the agent.** It used to live in
  `localStorage` and nowhere else, so the wizard would hand a person a London
  command while `status` went on offering Singapore — both halves behaving
  correctly and disagreeing, with nothing able to see it. "Use this city" now
  records it in `state/intent.json`, which `status` resolves through, and the
  map title follows the city rather than aiming a London CSV at a Singapore
  list. Deliberately not `last_run.json`: `recorded` there means a run
  happened, and an intention is not a run.
- **`run` is no longer offered on an unverified session.** `status` used
  `logged_in` — which only means `storage_state.json` exists — and on a
  machine whose Google and Untappd sessions had both expired it reported
  `blocked_on: null` and handed an agent `run`. Signed out of Untappd that
  builds a five-venue corpus and reports a finished scrape. The first action
  is now `verify`, and `run` waits for it to pass. `verify` records its
  verdict in `state/verification.json` so the loop terminates; the record
  expires after 12 hours, and a probe that could not run records nothing.
- `status`'s hints are keyed off `blocked_on` rather than the session file,
  so a tested-and-expired session tells the person what to do instead of
  naming the right error code and then talking about ledger locks.
- The dashboard opens as a three-step wizard: **Ready → Accounts → City**.
  The panel has six rows because six things can be wrong; a person setting
  this up for the first time has three questions, and the panel is now
  reference behind an "All checks" disclosure rather than the interface. The
  stepper and the card are derived from the same step, because two functions
  deciding where the user is, from the same data, is two chances to disagree
  — and they did: the card counted its own list of five and read "Step 4 of
  5" under a stepper showing 2 of 3.
- `ui --detach`: start the dashboard in its own process and return at once.
  Without it, an agent-driven setup dead-ended — `ui` blocked, so AGENTS.md
  told agents never to start it, so the one thing built to tell a user what to
  do next was the one thing nothing was allowed to open. The install finished,
  the agent reported "ready", and the user was left needing to already know
  the answer. It is now the first action when `blocked_on` is `sign_in`, and
  the loop ends once a dashboard is serving. Idempotent per checkout.
- The dashboard refuses to share a port. `http.server` sets
  `allow_reuse_address`, which let a second dashboard bind a port another was
  already LISTENING on — two servers, two different keys, requests going to
  whichever. A collision now fails loudly.
- Every setup row carries links and a "Why this matters" explanation: where to
  sign in, where to create an account, what a key costs, and what actually
  breaks when that row is red.
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

[Unreleased]: https://github.com/Odiph/beer-in-this-town/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/Odiph/beer-in-this-town/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Odiph/beer-in-this-town/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Odiph/beer-in-this-town/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Odiph/beer-in-this-town/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Odiph/beer-in-this-town/releases/tag/v0.1.0
