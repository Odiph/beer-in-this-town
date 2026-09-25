# Agent contract

This tool is built to be driven by a coding agent. This file is the contract.
If you are an agent, read this and nothing else is required.

The flow it drives, for a human, is [docs/FLOW.md](docs/FLOW.md). This file is
the same flow for an agent with no UI: what you run, what only a person can
do, and what to say to them when you reach it.

## The loop

```
python -m beer_in_this_town status --json      →  read `next_actions`
run the first action                      →  read the envelope
repeat until `next_actions` is empty
```

`status` is free, has no side effects, and always tells you where the pipeline
is. Never infer progress from logs — ask.

`next_actions` only ever contains `status`, `verify`, `sweep`, `enrich`,
`filter`, `export`, `ui --detach` (when a sign-in is needed or no city has
been chosen, and no dashboard is already serving), and `score` after
`label`. None of them writes to an account. `status` never offers `doctor`:
doctor changes nothing, so offering it would loop. `pin`, `notes`, `closures`
and `bootstrap` never appear there; when they are the next step, their exact
command text is in `hints`, for the human.

An empty `next_actions` means one of these, and `data.blocked_on` says which:

| `blocked_on` | Meaning | You |
|---|---|---|
| `null` | Finished: every read-side stage is done for the city | Report, then give the human the `hints` (list creation, `pin`, `notes`). |
| `"sign_in"` | No session file, or `verify` found an account signed out | Open the dashboard and ask the human to sign in (step 3). |
| `"choose_city"` | Nobody has named a city | Ask the human which city. |
| `"emulator"` | A `--method map` sweep is next and the emulator checks fail | Give the human the failing checks' `remedy` (in `data.emulator` and `hints`; see the playbook, step 2). |

When several apply, `blocked_on` reports the first in this order: `sign_in`,
then `choose_city`, then `emulator`. The emulator is probed only when `sweep`
is the next stage and `data.method` is `"map"`, so it never blocks a search
sweep, `enrich`, `filter` or `export`.

(`"app_screen"` is not reported by `status`; it is what a `sweep` failure with
`app_screen_unexpected` means. See step 5b.)

## The envelope

Every command with `--json` prints exactly one object on stdout. Logs go to
stderr. Parse stdout; ignore stderr unless debugging.

```json
{
  "command": "filter",
  "ok": true,
  "schema_version": "2.0",
  "data": {
    "city": "Tel Aviv",
    "input": "/path/to/beer-in-this-town/data/tel-aviv/2_enriched.csv",
    "csv": "/path/to/beer-in-this-town/data/tel-aviv/3_venues.csv",
    "excluded_csv": "/path/to/beer-in-this-town/data/tel-aviv/3_excluded.csv",
    "kept": 81,
    "excluded": 7,
    "excluded_by_reason": { "restaurant-only": 4, "no beer category: Hotel": 3 },
    "flagged": { "possibly_closed": 2 }
  },
  "warnings": ["2 kept venue(s) are flagged (possibly_closed: 2). Flagged, not dropped: removing one is your decision."],
  "next_actions": ["python -m beer_in_this_town export --city \"Tel Aviv\" --json"],
  "hints": ["Optional, paid (Google Places key): a human can check closures with: python -m beer_in_this_town closures --csv \"/path/to/beer-in-this-town/data/tel-aviv/3_venues.csv\" --limit 3 --json"],
  "error": null
}
```

Paths in `data` are absolute. The counts and reason strings are
illustrative; the keys are the ones `filter` returns.

- `ok` — did the command achieve its purpose. Exit code matches (`0` / `1`).
- `next_actions` — **literal runnable commands**, best first. Not hints.
  Only read-only, safe commands appear here; it never contains `pin`, `notes`,
  `closures` or `bootstrap`, and never contains a `#` comment. An **empty list
  is the end of the loop**, not an error.
- `hints` — prose for a human: what only a person can decide to do next.
  Nothing executes this, and an agent must not treat it as a next action.
- `error` — present only on failure. Always carries `code` and `remedy`.
- `schema_version` — treat a change as breaking. **New fields are not a
  change**: `data`, `warnings`, `hints` and `next_actions` gain keys without a
  bump, so read them defensively and ignore what you do not recognise. A field
  being *removed* or *renamed*, or an existing one changing meaning, bumps it.
  It is `"2.0"` as of v0.2.0, bumped because a command was removed (see
  CHANGELOG.md) and `status`'s `data.stages` changed meaning (see
  [Setup](#setup-when-you-are-the-one-driving)).

Commands built from a name you or the human gave (a city, a list) are shown
with shell metacharacters removed, not escaped: `"`, `` ` ``, `$`, `;`, `&`,
`|`, `<`, `>`, `%`, `^`, `!` and control characters are dropped, because the
same text is pasted into PowerShell, cmd and POSIX shells. A list called
`Beer & Bars` appears in a printed command as `"Beer  Bars"`, which is not
its name. Ask the human to name the Google Maps list with plain letters,
digits and spaces.

`--json` works before or after the subcommand.

## Playbook

One command per step. There is no command that chains them; you run each, read
its envelope, and move on. Steps marked **human** cannot be done by you: stop,
give the human the message shown (adapted, not paraphrased away), and wait.

`<slug>` is the city lower-cased with runs of non-alphanumerics turned into
`-` (`Tel Aviv` → `tel-aviv`). Each stage reads the previous stage's file from
`--city`; `--in PATH` overrides. Re-running a stage overwrites only its own
output.

### 1. Install — human, once

You may run the Python half yourself if you have a shell in the checkout:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[browser]"    # .venv/bin/python elsewhere
```

The rest is theirs. Message:

> I need a few things installed that I can't install for you: Google Chrome,
> BlueStacks 5 (bluestacks.com), and Android platform-tools so that `adb` is
> on your PATH (on Windows: `winget install Google.PlatformTools`). Steps 1
> and 2 of docs/FLOW.md walk through each. Tell me when they're done.

### 2. Emulator — human, once; you check

```bash
python -m beer_in_this_town doctor --json
```

`doctor` runs four emulator checks, in order: `adb_on_path`,
`device_connected`, `untappd_installed` (package `com.untappdllc.app`),
`screen_size` (1600x900 or 900x1600). They are in `data.emulator`, a list of
`{name, ok, detail, remedy}` always in that order, and `data.emulator_ready`
is true only when all four pass. A check after a failing one is reported as
not checked rather than run. Take the **first** failing one and give the
human its `remedy`. Message, when nothing is set up yet:

> In BlueStacks: Settings → Display → resolution 1600 x 900 (the default), save and restart.
> Then Settings → Advanced → turn on Android Debug Bridge, and note the
> address it shows (usually 127.0.0.1:5555). Install Untappd from the Play
> Store inside BlueStacks and sign in to it. Tell me when that's done and
> I'll connect.

You may run `adb connect 127.0.0.1:5555` and `adb devices` yourself: they
touch the local emulator only. If the human's port is not 5555, set
`BEERTOWN_ADB_SERIAL` to the address they give you. Re-run `doctor` until
every check passes. (`status` does not offer `doctor` itself: while the
emulator is not ready and a sweep is next, its `next_actions` is empty and
`blocked_on` is `"emulator"`.)

### 3. Sign in — human; you open the page and check

`status` offers `ui --detach` first when there is no session. Run it; it starts
the dashboard in its own process and returns at once with `data.url`.

> Please open <data.url> and complete the Accounts step: sign in to Google and
> then to untappd.com in the Chrome window it opens. It needs your
> passwords, so I can't do it. Tell me when it shows both connected.

Then:

```bash
python -m beer_in_this_town verify --json
```

Read `data.ran` before `ok`. `ok: false, ran: true` is a signed-out account:
back to the message above. `ran: false` means the check could not run at all
(usually a missing browser): fix what the message names, and do **not** tell
the human they are signed out. `verify` writes `state/verification.json`,
which expires after 12 hours.

### 4. Choose a city — human

> Which city should I map?

Pass what they say as `--city`, verbatim. Never infer a city from `data/`, an
old corpus, a list name or the repo. If they chose one in the dashboard,
`data.intent` has it and the `sweep` in `next_actions` already carries it.

### 5. Choose how to sweep — the human's call, once per city

`sweep` has two methods. `data.method` in `status` says which one the next
sweep uses (`"map"` unless the human chose `search`), and the `sweep` command
in `next_actions` names it. The dashboard's city step asks for the method
with the city; only a `map` choice, the default, sends the human to the
emulator setup.

| | `--method search` | `--method map` (default) |
|---|---|---|
| Source | Untappd's web search, once per spelling of the city's name | the Untappd app's map, cell by cell |
| Needs | the Untappd web session (step 3) | the emulator (steps 1-2, 5b) |
| Finds | each spelling's most-checked-in venues first, up to `--top` (max 1,000) | what the map shows, capped ~60 per search |
| Time | ~50 requests per spelling at `--top 1000` | minutes to an hour or more |

Measured 2026-09-23: London's search held 99 of the map sweep's top 100
venues by check-ins; Tel Aviv's held all of its top 100 beer venues, where
the map had 20. The map is still the default (the user's call,
2026-09-24); search is what a person chooses. Do not switch methods on your
own initiative.

The spellings (`data.variants`) come from Foursquare's open places data,
cached per city: every spelling with at least 2% of the city's named
places, at most ten. Building the list needs the `search` extra
(`pip install -e ".[search]"`); without it the city as typed is the only
spelling, and the envelope says so in `warnings`.

### 5b. Open the map — human, before each `--method map` sweep

> In BlueStacks, open Untappd, tap Discover, then View Map, and close
> anything covering the map. Please don't touch the emulator until I say the
> sweep is done.

You cannot verify this except by running the sweep: it refuses with
`app_screen_unexpected` when the map is not in front. That is the signal to
send this message again, not to retry blindly.

### 6. Sweep — you

```bash
python -m beer_in_this_town sweep --city "<city>" --method search --json
```

Writes `data/<slug>/1_sweep.csv`. Report `data.venues`, and per spelling in
`data.variants` its reported `total`, how many were `collected`, and
`other_city` (results matched on their name, dropped). A spelling whose
`total` is above what was collected stopped at `--top`: the rest are its
less-visited venues, and `warnings` says so. A spelling with `complete:
false` is different: a page of results stopped loading, so it is short
because something failed, not because the rest is unpopular. Report it as
such and re-run the sweep; an incomplete search is never cached, so the
re-run retries it. Search rows carry each venue's
Untappd id and no position (`data.located` is 0); `enrich` adds both.

```bash
python -m beer_in_this_town sweep --city "<city>" --method map --json
```

Writes the same file. Takes minutes at depth 1 and can take an
hour or more at depth 3 on a large city; run it with a long timeout, or in the
background, and do not interrupt it. It journals per city, so a re-run after a
failure resumes. Use the defaults (`--min-depth 1 --max-depth 3`); they are
the measured recommendation in [docs/HARVESTING.md](docs/HARVESTING.md). Pass
`--here` only when the human asked for the emulator's GPS position rather than
a named city.

Report from the envelope: venues found (`data.venues`, of which
`data.located` have coordinates), cells still at the result cap when the
depth limit stopped them (`data.truncated_cells`, `data.hit_depth_limit`: the
sweep is saying it stopped short), and the calibration error
(`data.calibration.median_residual_m`). Neither method is a census; do not
describe either as "all the bars in <city>".

### 7. Enrich — you

```bash
python -m beer_in_this_town enrich --city "<city>" --json
```

Writes `data/<slug>/2_enriched.csv`. Needs the Untappd web session (step 3).
A map sweep's rows are names and pins, so enrich searches each name in a
visible Chrome window and checks the page against the pin. A search sweep's
rows already carry their venue id, so enrich fetches each page directly and
places it from the page; a page outside the city's bounds is a namesake
("london" also finds New London, CT) and is dropped as `outside`.
Report `data.resolved` out of `data.venues`, and `data.statuses` (counts of
`resolved`, `too_far`, `no_match`, `unverified`, `unlocated`, `fetch_failed`,
`search_failed`, `duplicate`, `outside`; the per-row reason is the
`resolution` column).
An unresolved venue has **unknown** counts, not zero: never report it as
having no check-ins. If most rows come back `fetch_failed` or `unverified`,
suspect the parser or the session, not the city: run `selfcheck --json`.

### 8. Filter — you

```bash
python -m beer_in_this_town filter --city "<city>" --json
```

Writes `data/<slug>/3_venues.csv` and `data/<slug>/3_excluded.csv`; both
carry `kind`, `flag` and `reason` columns. Offline. Report `data.excluded`,
the most common entries of `data.excluded_by_reason`, and `data.flagged`
(`closed`, `possibly_closed`): flagged venues are kept, not dropped.

### 9. Export — you

```bash
python -m beer_in_this_town export --city "<city>" --json
```

Writes `data/<slug>/venues.{kml,gpx,geojson}`, all three by default;
`--format` narrows it. Offline.
These are backups and non-Google maps, not the delivery.

### 10. Create the saved list — human

> In Google Maps (the same Google account you signed in with), go to Saved →
> New list and create a list for this city. Tell me its exact name. Pick a
> name no other list of yours contains, or the tool will refuse to guess,
> and use only letters, digits and spaces: symbols such as & | $ are
> stripped from the commands the tool prints.

### 11. Pin — human-triggered only

Do not run `pin` unless the human has explicitly asked you to put the venues
in their saved list, after reading that it automates Google Maps against
Google's terms. Then, trial first:

```bash
python -m beer_in_this_town pin --csv data/<slug>/3_venues.csv --list "<exact name>" --limit 3 --json
```

> I've saved 3 places into "<name>". Please check in Google Maps that they
> are the right places and in the right list before I do the rest.

Only after they confirm, run the same command without `--limit`. It trims
itself to the write budget (60 per run, 100 per 24h, shared with `notes`) and
says so; that is correct. Report where it stopped and tell the human to ask
again tomorrow. Do not schedule it.

### 12. Notes — human-triggered only

Same rules as `pin`, same trial:

```bash
python -m beer_in_this_town notes --csv data/<slug>/3_venues.csv --list "<exact name>" --limit 3 --json
```

### Optional: closures — human-triggered only

Costs the human money. See [The `closures` command](#the-closures-command).

### Where a step would need new automation

This project does not automate the emulator's setup, the app's sign-in,
BlueStacks' settings, Google Maps list creation, or anything else that is not
already a command above, and it will not. When a step is outside the commands,
you give the human instructions; you do not write a script, drive a browser or
send taps to the emulator to do it for them. The only adb commands you run
yourself are `adb connect` and `adb devices`.

## Error codes you should handle

| `error.code` | Meaning | What to do |
|---|---|---|
| `emulator_unavailable` | `doctor`'s emulator checks fail, so `sweep` refused before touching the device | Run `doctor --json`, give the human the first failing check's `remedy` (step 2). |
| `adb_unavailable` | The emulator could not be reached, or `adb` returned nothing usable | Check BlueStacks is running and `adb devices` lists it; `adb connect` again. Never read an empty result as an empty city. |
| `app_screen_unexpected` | The Untappd app was not showing the map, its category filter was not where expected, or `--here` could not find the map's Reset location control | Send the step-5b message (for `--here`, add "allow Untappd location access"). Progress is journalled per city, so the sweep resumes rather than restarting. |
| `app_pan_failed` | Gestures stopped reaching the map, three cells in a row | A marker, a dialog or the venue card is absorbing the swipe. Ask the human to bring the map to the front and dismiss anything over it, then re-run. **Do not** lower the pacing to compensate. |
| `calibration_failed` | Too few swept venues matched OpenStreetMap to trust the fitted positions | Nothing was written. Report it. Do not work around it; a sweep with guessed positions puts every venue somewhere plausible and wrong. |
| `city_not_found` | The geocoder has no match for the city, or (search) no spelling of it lists any venue | Ask the human for a fuller name (with country or state). Do not substitute a nearby city. |
| `search_unavailable` | `sweep --method search`: a search page showed neither results nor its empty state | Run `verify --json`; a signed-out Untappd session is the usual cause (step 3). Then re-run; finished searches are cached. |
| `overpass_unavailable` | OpenStreetMap's Overpass endpoint is throttled, down or unreachable | Not per-venue. Wait and re-run, or set `OVERPASS_URL` to a mirror. Overpass is volunteer-run and sheds load under pressure; a 504 is normal. |
| `geocoder_unavailable` | The geocoder is rejected, out of quota, or unreachable | Check the key and billing, or unset it for Nominatim. |
| `stage_input_missing` | The previous stage's file is not there | Run the command the `remedy` names (for `filter`, that is `enrich`). |
| `bad_format` | `export --format` named an unknown format | Use a comma-separated subset of `kml,gpx,geojson`. |
| `no_city` | A stage with no city, and none chosen | **Ask the human.** There is no default — choosing a city for someone chooses what they get. |
| `not_signed_in` | An account is signed out — from `verify`, `pin` or `notes`; or from `enrich`, which refuses without an Untappd session and stops at the first venue page that says "Log In to view Venue Stats" | **Stop and ask the human.** Requires their password; you cannot do this. `data.accounts` (from `verify`) says which one. `enrich` writes nothing when it stops this way. |
| `verify_unavailable` | The account check itself could not run | Not a signed-out account — usually a missing browser. Fix what the message names. Do **not** report this as "signed out". |
| `port_unavailable` | `ui --detach` could not start the dashboard | Another process holds the port, or the interpreter could not be spawned. Retry with `--port` set to something else. |
| `no_list` | `pin`/`notes` with no `--list`, and no list name remembered from `sweep --title` or the dashboard | **Ask the human.** Always pass `--list` with the exact name they gave you; this writes into a real Maps list and a guessed name is a guess about where. |
| `list_missing` | Target saved list does not exist | Ask the human to create it (step 10), or pick another `--list`. |
| `list_ambiguous` | `--list` does not name exactly one list — no list matches it exactly, or several do | **Stop and ask.** Nothing was saved. Do not retry with a nearby name; that is how a place lands in the wrong list. |
| `already_running` | Another process holds the write budget | Wait for it, then re-run the same command. Nothing has to elapse — this is not a cool-off. `status` reports the lock's age and whether it is stale under `data.write_guardrails.lock`. Do not delete the lock file; an abandoned one is broken automatically after 2h. |
| `robots_disallow` | `enrich`: Untappd's robots.txt disallows the venue pages | **Stop and ask.** Nothing was fetched. There is no flag to override it; see rule 1. |
| `selectors_stale` | `selfcheck` could not parse a known-good venue page | Read `debug/*.html`, fix selectors in `parsers.py`, re-run. `selfcheck` is the cheap early warning. |
| `stats_missing` | `selfcheck` parsed the page but found no check-in stats | Usually a signed-out Untappd web session (step 3); otherwise the markup changed, as above. |
| `fetch_failed` | `selfcheck` could not fetch the known-good page | Check connectivity, then retry. Repeated 403s mean a block: stop and report. |
| `rate_limited` | The hourly read budget (600 requests) is spent, or Untappd answered with repeated 429s | Wait, then re-run the same command; pages and searches already done are cached. Do not retry in a loop. |
| `guardrail_tripped` | A write guardrail tripped in `pin`/`notes` (circuit breaker, block detected, cool-off) | **Stop.** Do not re-run until the cool-off `status` reports has expired. Never delete `state/rate_ledger.json`. |
| `pin_failed` | `pin` stopped on an unexpected error | Re-run the same command; progress is journalled and resumes. |
| `no_profile` | `bootstrap --capture` with no Chrome profile yet | Human: run `bootstrap` without `--capture`. |
| `chrome_not_found` | `bootstrap` could not find or start Google Chrome | Human: install Google Chrome. |
| `login_timed_out` | `bootstrap`'s Chrome window was still open after `--timeout` | Human: close Chrome, then run `bootstrap --capture`. |
| `login_not_detected` | Chrome closed, but `bootstrap` found no Google session in the profile | Human: re-run `bootstrap` and finish the Google sign-in before closing the window. |
| `places_unavailable` | The Places API is rejected, out of quota, or unreachable | Not per-venue — check `GOOGLE_PLACES_KEY`, that the Places API (New) is enabled, and that billing is on. Nothing was written. |
| `places_key_missing` | The closure check was asked for with no `GOOGLE_PLACES_KEY` | Ask the human to set one. Do not silently carry on without the check — the venues are unchecked, not open. |
| `network_unavailable` | Several URLs in a row failed at the transport | Check connectivity, then re-run. The run stopped instead of sleeping through the backoff ladder per venue. |
| `bad_arguments` | An argument was rejected — most often a pacing value below its floor | **Do not retry with a different number.** The floors are account-safety limits; the message says which one. |
| `bad_pacing` | `--max-gap` is below `--min-gap` | Pass a real range, or omit both and take the defaults. |
| `csv_missing` | The `--csv` given to `pin`, `notes`, `closures` or `label` does not exist | Run the stages up to `filter` first; the file to pass is `data/<slug>/3_venues.csv`. |
| `labels_incomplete` | A sampled bucket came back with no labels | Ask the human to label a few rows in every bucket. The rare ones are the point. |
| `labels_unusable` | An answer is outside the accepted vocabulary, or the sheet lost its `_stratum`/`_stratum_size` columns | The message names the row and cell. Answers are `y` / `n` / `?`; a blank means unanswered. |
| `notes_failed` | The notes pass failed | Re-run; progress resumes. |
| `interrupted` | Ctrl-C | Re-run the same command; progress is journalled. |
| `unexpected_error` | Unhandled | Re-run with `-v` for a traceback. |

## Rules for agents

1. **Never switch off the robots.txt check.** `enrich` refuses with
   `robots_disallow` when Untappd's robots.txt disallows the venue pages.
   No command-line flag overrides it (it is `respect_robots` in
   `config.py`), and changing that is a human's call, not yours.
2. **Never run `pin` without an explicit human instruction.** It automates the
   Google Maps UI, which is against Google's ToS (see README). It is never in
   `next_actions`; it is in `hints`, where nothing is instructed to run it.
   Following the loop can therefore never lead you into a write.
3. **Trial before bulk.** First `pin` run should use `--limit 3`. Report the
   result before doing the rest.
4. **Do not lower the pacing.** `pin`'s and `notes`' `--min-gap` /
   `--max-gap` exist to keep the user's account safe (defaults 8–16 s for
   `pin`, 5–11 s for `notes`). Raise them if throttled; do not lower them.
   This is enforced rather than asked: a value below the default is refused
   with `bad_arguments`, and the floors are the shipped defaults. The read
   side's pacing (2–4.5 s between requests, 600 an hour) has no flag; do not
   edit `config.py` to change it.
5. **A failed parse is a real finding.** `selfcheck` fails with
   `selectors_stale` when a known-good venue page no longer parses. Do not
   work around it — fix the selector and say what changed. `enrich` has no
   aggregate parse gate: a parser break shows up there as rows marked
   `fetch_failed` or `unverified`, which you report as such, not as a thin
   city.
6. **Ask before anything irreversible** that touches the user's account.
7. **Do not drive the emulator beyond the tool.** `sweep` is the only thing
   that sends gestures to the Untappd app, and it drives a real, signed-in
   account. Do not tap, type, install or sign in on the emulator yourself,
   and do not automate a step the playbook gives to the human.
8. **Do not schedule anything.** No cron jobs, scheduled tasks or loops that
   re-run `pin` or `notes` overnight. The human decides each write session.

## The `notes` command

Writes Untappd stats into the note on each saved place. Same rules as `pin`: it
writes to the account, so never run it without an explicit human request, and
trial it with `--limit` first.

It only annotates places already in the target list; anything else is recorded
as `not-in-list` and left alone. A note that already matches is never rewritten.

A place saved in several lists has one note box per list, under its folded
"Saved in" row, and a list may be shared with other people. `notes` writes
only into the box whose block names the target list. When the place shows no
such box it types nothing, spends no write, records `no-note-box`, and the
run fails with `data.no_note_box` above 0 and a warning. Several in a row
mean Google Maps changed its note editor: stop and report it, do not retry.

Like `pin`, it runs a pre-flight first and reports `not_signed_in` or
`list_missing` rather than working through a hundred places against a
signed-out browser.

## The `label` and `score` commands

`classify.py` holds candidate heuristics for venue kind, closed venues and
private spaces. `label` emits a stratified sample for a human to judge;
`score` reports the error rates by direction.

`filter` does apply `classify.py`: it runs `craft_beer_decision`, which
excludes a venue that `looks_private`, keeps or excludes by the category
vocabulary in that module's docstring (a venue with no category is kept), and
only *flags* one that is closed per Places or `looks_closed`, never dropping
it. `label` and `score` measure the older `classify()` verdict, not
`craft_beer_decision`.

Both are read-only, offline and touch no account, so an agent may run them
freely. What an agent must **not** do is fill in the labels: the whole point is
a human judgement the classifier can be checked against, and a model labelling
its own classifier's output measures nothing.

Sampling takes a fixed quota per bucket, rare ones included, and `score`
divides that back out. Things worth knowing:

- Labelling only the easy rows breaks the weighting. `score` refuses a bucket
  with no labels and warns about thin ones, but cannot detect cherry-picking.
- Every rate's denominator is the rows that answered **that** question. A blank
  is not an answer and `?` is not a verdict, so a rate can come back `null`
  meaning *unknown* — which is not the same as zero errors, and must not be
  reported as a clean result.
- A CSV with no `category` column makes every kind prediction `unsettled`, so
  the kind measurement says nothing. `label` warns when it sees this.

## Setup, when you are the one driving

`status` empties `next_actions` for different reasons: the work is done, or it
cannot go on without a person. **`data.blocked_on` tells them apart.**

This used to loop. `next_actions` handed back `selfcheck`, nothing selfcheck
does changes the session, and the loop above ran it forever. An empty list is
the end of the loop; being blocked on a password is the end.

**`data.logged_in` is a file, not an account.** It means
`storage_state.json` exists. It was true on a machine whose Google and
Untappd sessions had both expired. So `sweep` is never offered until a
`verify` has actually passed:

The rows are checked top to bottom; the first that matches decides.

| What `status` shows | First action | Why |
|---|---|---|
| no session | `ui --detach` (nothing if a dashboard is already serving) — `blocked_on: "sign_in"` | a person must sign in |
| session, `verification: null` | `verify --json` | a cookie proves nothing |
| `verification.ok: false` | *(none)* — `blocked_on: "sign_in"` | a person must sign in |
| no city | `ui --detach` (nothing if a dashboard is already serving) — `blocked_on: "choose_city"` | a person must choose |
| sweep next, `method: "search"` | `sweep --city ... --method search --json` | only now, after `verify` passed |
| sweep next, `method: "map"`, emulator checks failing | *(none)* — `blocked_on: "emulator"` | a person must set up BlueStacks |
| sweep next, `method: "map"`, emulator ready | `sweep --city ... --method map --json` | only now, after `verify` passed |
| sweep done | `enrich`, then `filter`, then `export` | one stage at a time |
| every stage done | *(none)* — `blocked_on: null` | finished; `hints` has the rest |

`status` never offers `sweep` before a `verify` has passed.

`data.stages` is per city (the city in `data.city`, its folder in
`data.city_dir`): a list of `sweep`, `enrich`, `filter`, `export`, `pin` and
`notes`, each with `name`, `done`, `count` and `detail`. The four collection
stages also carry `stale` (its input is newer than its output: re-run it)
and `path`; `filter` adds `excluded` and `excluded_path`, `export` adds
`files`, and an interrupted sweep adds `journal_venues`. `pin` and `notes`
carry `total`, `by_status` and `list` from their per-list journals.
`data.next_stage` is the first collection stage not done, or `null`.
`data.method` is how the next (or last) sweep collects venues: the last
run's method, else the one chosen in the dashboard, else `"map"`.
`data.emulator` is the emulator checks (as in `doctor`) when a map sweep is
next, and `null` otherwise: `null` means not checked, never fine.

**The city comes from the user, not from a default.** `data.intent` is what
they asked for in the dashboard. When `intent` is null and no stage has run,
nobody has chosen one and there is nothing to fall back on. Do not pass a city
you inferred from a previous corpus or from the repo — ask.

`verify` writes `state/verification.json`, which is what lets the loop
terminate rather than re-verifying on every pass. It expires after 12 hours,
because a session that worked this morning can be dead by lunchtime.

The sequence when `blocked_on` is `"sign_in"`:

1. **Stop looping.** Report it. Do not substitute a stage — signed out of
   Untappd, venue pages hide their totals, and `enrich` would produce a map
   whose every count is unknown.
2. **Open the dashboard** with `ui --detach --json` and give the user
   `data.url`. Then ask them to sign in: it needs a password, so it is not
   yours to do.
3. **Then run `verify --json`** to find out whether it actually took.

## The `verify` command

Tests both accounts for real: a headless Maps load for Google, one request for
Untappd. Read-only, opens no window, always returns — safe for an agent, and
the only way to answer "did the sign-in work" without the dashboard.

Read `ran` before `ok`. `ok: false, ran: true` is a signed-out account.
`ran: false` means the check could not run at all, which is a different
problem with the opposite remedy, and reporting it as "signed out" sends the
human through a login that was never broken.

It does not check the Untappd **app** session on the emulator. A signed-out
app shows a sign-in screen instead of the map, which `sweep` reports as
`app_screen_unexpected`.

## The `ui` command

Serves a setup dashboard on localhost and **blocks until interrupted**. It is
for a human at a keyboard.

Run it only as `ui --detach --json`, which starts it in its own process and
returns at once with `data.url`. Never run a bare `ui`: an agent that starts
it will hang. A bare `beertown` with no subcommand opens it too — but only at
a terminal, and never with `--json` or piped output, so an agent gets the
usual `bad_arguments` envelope instead of a process that never returns.

The dashboard walks the human through the same flow as the playbook,
including the emulator setup and the saved list. It has no button and no route
that can `pin` or write `notes`.

## The `closures` command

Asks the Google Places API whether each venue in a CSV still trades, and writes
a `business_status` column into a **new** CSV.

It touches no account and opens no browser, so it is not in the `pin`/`notes`
category. But it **spends the user's money** — Places Text Search Pro, 5,000
lookups a month free, then $25.60/1000 — so it is not in the `label`/`score`
category either, and it is deliberately absent from `next_actions`.

Rules for agents:

1. **Do not run it unasked.** Billing is the user's, and so is the decision to
   send venue addresses to Google. It is in `hints`, where nothing executes it.
2. **Trial with `--limit 3` first**, as with `pin`. Report before the bulk.
3. **`unmatched` is not `closed`.** A venue Places could not find is recorded
   as `unmatched` and stays visible. Do not report it as closed, do not filter
   on "not OPERATIONAL", and do not treat a run full of `unmatched` as a
   finding — that shape usually means the query is wrong, not that a city shut.
4. **Closed venues are flagged, never dropped.** Removing them is a decision
   the user makes.
5. `places_key_missing` means stop and ask. It never means carry on unchecked
   and call the venues open.

## Idempotency

- `sweep` — resumable. Journalled per city; a re-run after a failure picks up
  where it stopped. Once every cell is walked the journal is marked complete,
  so a re-run within 12h after a placement failure (`overpass_unavailable`,
  `calibration_failed`) re-places without touching the emulator. Do not pass
  `--fresh` on your own initiative: it re-walks the whole map. Journals older
  than 12h are discarded. Overwrites `1_sweep.csv` when it completes.
- `enrich` — resumable. Every venue is journalled as it resolves
  (`state/enriched_<slug>.json`, tied to the sweep file's content), so a
  stopped run continues where it stopped and nothing is looked up twice.
  `--limit N` does at most N venues per run; while `data.remaining` is above
  0 the envelope's `next_actions` repeats the same command, and
  `data.csv` is null. Prefer batches (`--limit 25`-`50`) for a big city: each
  finishes, which a multi-hour run may not. Writes `2_enriched.csv` only when
  every venue is done.
- `filter`, `export` — offline, deterministic, overwrite their own outputs.
- `pin` — resumable and idempotent. `state/pinned_<list>.json` records every place;
  re-running skips successes and retries failures. Places recorded `not-found`
  or `ambiguous` are retried too, since both can be transient.
- `notes` — resumable via `state/noted_<list>.json`; a matching note is skipped.
- Journals, baselines and `status` are scoped to the city or list they
  describe. `status` reports per-city stage progress in `data.stages` (done,
  stale, count, path, detail); its `next_actions` are built from those, not
  from defaults.
- `status`, `doctor`, `selfcheck`, `verify` — read-only.

## What needs a human

- Installing Chrome, BlueStacks and adb, and turning on BlueStacks' Android
  Debug Bridge (the default 1600 x 900 screen is the right one).
- Installing Untappd in the emulator and signing in to it.
- Signing in to Google and Untappd in the tool's Chrome profile (`ui` or
  `bootstrap`).
- Choosing the city.
- Opening Discover → View Map before each sweep, and leaving the emulator
  alone while it runs.
- Creating the target saved list in Google Maps.
- Deciding whether to use `pin`, `notes` and `closures` at all.

## Account-safety guardrails (do not weaken these)

`pin` writes to a live Google account. Four independent guardrails, layered so
defeating one still leaves the others:

| Guardrail | Behaviour |
|---|---|
| **Rate ledger** | Rolling 24h write budget, **persisted to disk**. Restarting does not reset it. Default 100/day, 60/run. |
| **Circuit breaker** | 3 consecutive failures → stop and start a cool-off. Repeated failure is when a script looks least human. |
| **Block detection** | Scans every page for CAPTCHA / "unusual traffic" / "not a robot" / forced sign-out. Any hit aborts instantly. Google serves these as HTTP 200, so text is the only signal. |
| **Cool-off** | 6h, persisted. Applied after any trip or detected block. |

Every one of those survives a restart, and so do the read-side protections:
the hourly request ceiling and the circuit-breaker count are both on disk. A
guardrail you can clear by starting the process again is a speed bump, not a
guardrail.

The sweep has its own: jittered 5–8 s settles after every search, a quorum
before it calls a pan dead, and an abort after three dead pans. It drives a
real, signed-in Untappd account, and a fixed rhythm is a tell.

Rules for agents:

1. **Never delete `state/rate_ledger.json`** to get a fresh allowance. The
   persistence is the entire point.
2. **Never raise `max_per_day` / `max_per_run`** on your own initiative.
3. **Never retry past a `Tripped` error.** It means stop, not try again.
4. If a run trims itself ("Trimming this run to N places"), that is correct
   behaviour — report it and resume later, do not work around it.
