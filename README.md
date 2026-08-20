# beer in this town

[![CI](https://github.com/Odiph/beer-in-this-town/actions/workflows/ci.yml/badge.svg)](https://github.com/Odiph/beer-in-this-town/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Find out where to get a beer in this town — then put it on your map.**

![demo](docs/demo.gif)

Scrapes Untappd venue listings for a city, pulls each venue's **Venue Stats**
(total / unique / monthly check-ins), and exports a CSV plus a KML you can
import into Google Maps.

Designed to be **driven by a coding agent**: every command speaks JSON, reports
its own state, and tells you what to run next. It works fine as a plain CLI too.

```bash
beertown status --json      # where am I, what is next
beertown run --query singapore --count 100 --json
```

---

## Why this exists

Untappd knows which bars in a city are actually worth going to. Thousands of
check-ins per venue, updated constantly, and nobody checks into a bad taproom
twice.

Google Maps is where you actually decide where to walk tonight.

Nothing connects the two. So the data that answers *"where should I drink
here?"* lives in one app, the map you navigate with lives in another, and
moving a hundred venues across by hand is an evening you could have spent in
one of them.

This closes that gap. Point it at a city and you get every venue with its
check-in counts, landed on your own map — as a bulk-imported layer, or pinned
into a real saved list with the numbers written into each place's note, so the
map itself tells you which of the four bars on this street is the one people
keep going back to.

Useful for planning a trip, working out a city you have just moved to, spotting
which places are getting busy this month, or keeping a list worth sharing of
somewhere you already know well.

It runs in about six minutes for a hundred venues, and it is built to be
**boringly trustworthy**: it verifies every write, paces itself so it will not
get your account flagged, and refuses to emit data it is not confident in
rather than quietly handing you wrong numbers.

Along the way it turned out that Google Maps has **no legitimate way to bulk-add
places to a saved list** — no API surface, no OAuth scope, no URL parameter, no
intent. [Here is the full research](#there-is-no-api-for-this-we-checked), with
the workarounds ranked.

## Install

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev,browser]"
playwright install chromium   # only if the system Chrome channel is missing

beertown bootstrap   # one-time: log in to Untappd (and Google)
```

`bootstrap` uses a **dedicated** Chrome profile, not your everyday one —
pointing Playwright at a live profile requires Chrome to be fully closed and
can disturb its session.

## Use

```bash
beertown doctor --json      # deps, session, geocoder
beertown selfcheck --json   # 1 request: are the selectors alive
beertown run --query singapore --count 100 --no-upload --json
```

Outputs land in `data/`:

| File | What |
|---|---|
| `venues_<query>_<date>.csv` | full dataset |
| `venues_<query>_<date>.kml` | import this into Google My Maps |
| `new_venues_<date>.csv` | only venues absent from the previous run |

## Getting it onto Google Maps

This is the part everyone gets wrong, so it is worth being precise.

**There are two different things called "a map" in Google Maps:**

| | My Maps layer | Saved list |
|---|---|---|
| Lives under | Saved → **Maps** | Saved → **Lists** |
| Pins on your everyday map | No — you open the layer | **Yes** |
| Tap a pin | Info window with imported data | Full place card, one-tap directions |
| Bulk import | **Yes — supported feature** | **No mechanism exists** |

`run` produces a KML for the **My Maps** path. That is a documented, supported
bulk import: one file, up to 2000 placemarks, done.

### There is no API for this (we checked)

We researched this properly. As of August 2026:

- **No Maps Platform API** has any write surface for user saved places. The
  catalogue is business/place data, not account data.
- **The Data Portability API** (DMA) covers Maps starred places — but every
  Maps scope is export-only. There is no import counterpart.
- **No documented URL parameter or Android intent** saves or bookmarks a place.
  Maps URLs support exactly four actions: search, directions, display, Street
  View.
- **No OAuth scope** exists that grants saved-list write, so no third-party
  service can legitimately offer it either.
- Google knows: [issue 453378725](https://issuetracker.google.com/issues/453378725)
  ("Bulk Save to Google Maps Lists", filed Oct 2025) is open and unanswered.

**The legitimate alternatives, ranked:**

1. **Import the KML into My Maps.** Supported, instant, keeps the check-in
   stats on each pin. You give up the everyday-map pins.
2. **Build the list once by hand on Android**, then use Maps' documented
   [multi-select](https://support.google.com/maps/answer/3184808?co=GENIE.Platform%3DAndroid)
   ("touch and hold a place, tick the others, Select all") to move them into a
   named list in one action — then **share the list**. Anyone you share with
   gets all 100 in a single tap by following it. ~40 minutes, once, and
   unambiguously clean.
3. **Follow an existing public list**, if one covers your places. One tap — but
   it isn't your data and the owner can delete it.

Saved lists cap at **3000 entries**.

### The `pin` command

The repo ships a `pin` command that drives the Google Maps UI to build a real
saved list. **Automating the Maps UI is against Google's Terms of Service**
("do not access the Services through automated means"), unlike the My Maps
import. It exists because the gap is real and some people will want it anyway.

If you use it: it verifies every save by reloading the place page, undoes
wrong-list saves, paces itself at 8–16s, and journals progress so it resumes
after an interruption. It never creates a list — you make that by hand.

Agents are instructed not to run it without an explicit human request.

## Adding the stats to a saved list

A saved list is just pins — it carries no data of its own, which makes it
strictly less informative than the KML layer where the stats show in each pin.
The one place per-venue data can live is the note attached to a saved place:

```powershell
beertown notes --csv data\venues_singapore_2026-08-21.csv `
    --list "Singapore Bars"
```

Each note reads:

```
Untappd #45 | 2,628 check-ins | 807 unique | 10/month | as of 2026-08-20
```

The date tracks **when the data was captured**, not when the note was written,
so re-running does not rewrite every note with a new date. A note that already
matches is skipped.

## Commands

| Command | What it does | Touches your account |
|---|---|---|
| `status` | Where the pipeline is, what to run next | no |
| `doctor` | Dependencies, session, geocoder | no |
| `bootstrap` | One-time login (opens a real Chrome) | reads |
| `selfcheck` | One request: are the selectors alive | no |
| `run` | Scrape → CSV + KML + diff | no |
| `pin` | Save into a Google Maps list | **writes** |
| `notes` | Write stats into each place's note | **writes** |

## Agent-driven use

See **[AGENTS.md](AGENTS.md)** for the full contract, and
`.claude/skills/beer-in-this-town/SKILL.md` for the Claude Code skill.

The short version: run `status --json`, execute the first entry in
`next_actions`, repeat. Failures return a valid envelope with a machine-readable
`error.code` and a `remedy` — no traceback parsing.

## Politeness and failing loudly

Single connection, no concurrency, 2.0–4.5s jittered delay, 600 requests/hour
cap, 12h disk cache, and a deliberate abort after three consecutive throttle
responses. ~100 venues ≈ 6 minutes.

**Untappd's ToS prohibits automated access.** Low volume, caching and rate
limiting are mitigations, not an exemption. The script reads `robots.txt` and
refuses to start if the paths are disallowed.

Silent-wrong-data is the dangerous failure mode, so:

1. Selector misses **raise**; there is no silent `.get(default=None)`.
2. Stats match on the literal labels TOTAL/UNIQUE/MONTHLY/YOU, never DOM
   position.
3. If under 90% of venues yield full stats, the run **aborts and writes
   nothing**.
4. Every parse failure dumps the offending HTML to `debug/`.

A real bug this caught: `parse_count("136 Monthly")` returned **136,000,000**,
because the `M` of "Monthly" read as a millions suffix. It would have inflated
the Monthly column on every venue with no error at all. See
`test_label_letter_is_not_a_magnitude_suffix`.

## Tests

```bash
pytest -q -m unit     # offline, no network, no browser
```

Around 700 lines of offline unit tests. CI runs them on Linux and Windows
against Python 3.11 and 3.12, and **never touches Untappd or Google**.

## Finishing a big list over several nights

The write guardrails cap how much can be done per day, so a hundred venues is
deliberately more than one session. `run_catchup.ps1` handles that: it pins
whatever is still missing, then annotates whatever is already pinned, and both
halves trim themselves to the remaining budget. Run it nightly and the backlog
drains on its own; once everything is done it exits in seconds having done
nothing.

```powershell
$script = "$PWD
un_catchup.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
Register-ScheduledTask -TaskName "beertown catchup" -Force `
    -Action $action -Trigger (New-ScheduledTaskTrigger -Daily -At "01:15") `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```

`-StartWhenAvailable` matters: the machine is usually asleep at 01:15, and
without it a missed run is simply skipped.

## Scheduling (Windows)

```powershell
schtasks /Create /TN "Untappd Venues" `
  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File <repo>\run_weekly.ps1" `
  /SC WEEKLY /D SUN /ST 03:00 /RL LIMITED /F
```

Tick "Run task as soon as possible after a scheduled start is missed" and
"Start only if network is available".

## Licence

MIT. See [LICENSE](LICENSE).

This project is not affiliated with Untappd or Google. You are responsible for
your own use of it, including compliance with those services' terms.
