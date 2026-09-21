# The whole flow, step by step

This is the canonical walkthrough: from a machine with nothing installed to a
Google Maps saved list of a city's craft-beer venues, each with its Untappd
check-in numbers written into the place's note.

It is written for someone who has never seen this project. Every manual step
is spelled out. If you are a coding agent, read [AGENTS.md](../AGENTS.md)
instead: it is the same flow, with the parts only a person can do marked as
such and the exact message to give them.

```
 once                      each city                                    into Google Maps
 ----                      ---------                                    ----------------
 install tools      ->     open app on          ->  sweep  -> enrich ->  create a saved list
 set up emulator           Discover > View Map      filter -> export     pin --limit 3, check
 sign in (app,                                                           pin the rest (nights)
   Google, Untappd)                                                      notes
```

| # | Step | Who | Command | Writes |
|---|---|---|---|---|
| 1 | [Install the tools](#1-install-the-tools) | you, once | | |
| 2 | [Set up the emulator](#2-set-up-the-emulator-bluestacks-5) | you, once | `beertown doctor --json` checks it | |
| 3 | [Sign in to Google and Untappd](#3-sign-in-to-google-and-untappd-in-the-tools-chrome-profile) | you | `beertown ui`, or `beertown bootstrap`; then `beertown verify --json` | `chrome-profile/`, `storage_state.json` |
| 4 | [Choose a city](#4-choose-a-city) | you | `ui` city step, or `--city` | `state/intent.json` (ui only) |
| 5 | [Open the map in the app](#5-open-discover--view-map-in-the-untappd-app) | you, before each sweep | | |
| 6 | [Sweep](#6-sweep) | command | `beertown sweep --city "<city>" --json` | `data/<slug>/1_sweep.csv` |
| 7 | [Enrich](#7-enrich) | command | `beertown enrich --city "<city>" --json` | `data/<slug>/2_enriched.csv` |
| 8 | [Filter](#8-filter) | command | `beertown filter --city "<city>" --json` | `data/<slug>/3_venues.csv`, `3_excluded.csv` |
| 9 | [Export](#9-export-map-files) | command | `beertown export --city "<city>" --json` | `data/<slug>/venues.{kml,gpx,geojson}` |
| 10 | [Create the saved list](#10-create-the-saved-list-in-google-maps) | you | | (in Google Maps) |
| 11 | [Pin](#11-pin-three-check-then-the-rest) | you trigger it | `beertown pin --csv data/<slug>/3_venues.csv --list "<name>" --limit 3` | the saved list; `state/pinned_<list>.json` |
| 12 | [Notes](#12-notes) | you trigger it | `beertown notes --csv data/<slug>/3_venues.csv --list "<name>" --limit 3` | each place's note; `state/noted_<list>.json` |
| opt | [Closure check](#optional-closure-check-paid) | you trigger it | `beertown closures --csv data/<slug>/3_venues.csv` | `data/checked_3_venues.csv` (or `--out`) |

`<slug>` is the city in lower case with every run of non-alphanumeric
characters turned into `-`: `"Tel Aviv"` becomes `tel-aviv`. Each stage finds
the previous stage's file from `--city`; `--in PATH` points it at a
different file instead. Re-running a stage overwrites its own output and
nothing else.

At any point, `beertown status --json` tells you which stages are done for
which city and what to run next. It has no side effects.

---

## 1. Install the tools

You need all of these. None of them is optional for the full flow.

| Tool | Why | Where |
|---|---|---|
| **Python 3.11 or 3.12** | runs the tool | python.org, or your package manager |
| **Google Chrome** (or Chromium) | the tool signs in, reads Untappd venue pages and drives Google Maps through a dedicated Chrome profile | google.com/chrome |
| **BlueStacks 5** | runs the Untappd Android app, whose map is the only geographic venue search Untappd has | bluestacks.com |
| **adb** (Android platform-tools) | how the tool reads the app's map | see below |
| **An Untappd account** | signed in to the app (for the map) and to the web (for venue stats, which are login-gated) | untappd.com |
| **A Google account** | the saved list lives there | |

### The tool itself

```bash
git clone https://github.com/Odiph/beer-in-this-town.git
cd beer-in-this-town

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e ".[browser]"
playwright install chrome          # only if no Google Chrome is installed
```

That puts `beertown` on your `PATH`. `python -m beer_in_this_town <command>`
is equivalent everywhere.

### adb (Android platform-tools)

`adb` must be on your `PATH`: the tool runs it by name.

- **Windows.** Download "SDK Platform-Tools for Windows" from
  developer.android.com/tools/releases/platform-tools, unzip it somewhere
  permanent (for example `C:\platform-tools`), and add that folder to your
  `PATH`: Start, type "environment variables", *Edit the system environment
  variables*, *Environment Variables...*, select `Path` under your user, *Edit*,
  *New*, paste the folder, OK. Open a **new** terminal afterwards.
  `winget install Google.PlatformTools` does the same in one line.
- **macOS.** `brew install --cask android-platform-tools`, or download the
  macOS zip from the same page and add the folder to `PATH` in `~/.zshrc`.
- **Linux.** `sudo apt install adb` (Debian/Ubuntu), `sudo dnf install
  android-tools` (Fedora), or the Linux zip from the same page.

Check it:

```bash
adb version
```

BlueStacks ships its own `HD-Adb.exe`. You do not need it and should not put
it on your `PATH`: two different adb servers on one machine fight over the
port and each keeps killing the other.

---

## 2. Set up the emulator (BlueStacks 5)

BlueStacks 5 runs on Windows. On macOS, BlueStacks' current Mac build may work
the same way but has not been tested with this project. On Linux there is no
BlueStacks; an Android Studio emulator set to the same 900 x 1600 portrait
screen is the likely route, and is also untested. The sweep has only ever
been run on BlueStacks 5 on Windows.

### 2a. Install BlueStacks 5

Download it from bluestacks.com and run the installer. Choose the default
Android instance (the installer's "Pie 64-bit" or newer is fine). Let it
finish, open it once, and sign in to the Google Play Store inside it with any
Google account (it does not have to be the one your saved list will live in).

### 2b. Display: 900 x 1600, portrait

Open BlueStacks **Settings** (the gear icon in the right-hand sidebar), then
**Display**:

- **Display orientation:** Portrait
- **Display resolution:** 900 x 1600
- **Pixel density:** leave it at the default. The tool does not check it,
  and which density the calibration runs used was not recorded.

Save, and let BlueStacks restart the instance if it asks.

**Why this exact size.** The sweep reads positions off the screen: where the
map area starts and ends, where the venue card overlays it, how far a swipe
moves the map. Those were measured on a 900 x 1600 portrait screen and are
constants in the code. On any other size the pans land in the wrong place and
the sweep either stops with `app_pan_failed` or, worse, sweeps an area that is
not the one it reports. `doctor` refuses to call the emulator ready until the
screen is 900 x 1600.

### 2c. Turn on Android Debug Bridge

In BlueStacks **Settings**, open **Advanced** and switch on **Android Debug
Bridge (ADB)**. BlueStacks then shows the address to connect to, normally:

```
127.0.0.1:5555
```

If you run more than one BlueStacks instance, each gets its own port (5555,
5565, 5575, ...). Use the one shown for the instance you will sweep with.

### 2d. Connect adb

```bash
adb connect 127.0.0.1:5555
adb devices
```

`adb devices` should list `127.0.0.1:5555    device`. If it says
`offline` or `unauthorized`, run `adb kill-server`, then `adb connect` again.

The tool talks to `127.0.0.1:5555` by default. If BlueStacks gave you a
different port, set it before running anything:

```bash
export BEERTOWN_ADB_SERIAL=127.0.0.1:5565        # macOS / Linux
$env:BEERTOWN_ADB_SERIAL = "127.0.0.1:5565"      # Windows PowerShell
```

The connection does not survive a BlueStacks or computer restart. Run
`adb connect` again each time you start BlueStacks.

### 2e. Install Untappd and sign in

In BlueStacks, open the **Play Store**, search for **Untappd**, install it,
open it, and sign in to your Untappd account. Let it ask for location
permission and allow it.

### 2f. Check

```bash
beertown doctor --json
```

`doctor` checks, in order: `adb` is on your `PATH`, a device is connected at
the serial above, the Untappd app is installed on it, and the screen is 900 x
1600 (`adb_on_path`, `device_connected`, `untappd_installed`, `screen_size`
under `data.emulator`, with `data.emulator_ready` true once all pass). Each
failing check says what to do. No sweep is worth running until all four
pass; while a sweep is the next stage, `status` reports
`blocked_on: "emulator"` until they do.

---

## 3. Sign in to Google and Untappd in the tool's Chrome profile

The tool keeps its own Chrome profile in `chrome-profile/`, separate from
your everyday browser. It needs two sessions there:

- **Untappd (web).** Venue pages show check-in totals only to a signed-in
  visitor. `enrich` reads them.
- **Google.** `pin` and `notes` save places into your saved list through that
  session.

Two ways to sign in; pick one.

**The dashboard (recommended).**

```bash
beertown ui
```

It opens a page on `localhost`. The **Accounts** step opens a Chrome window
on the tool's profile; sign in to Google there, then to untappd.com, and come
back to the page. It tests both accounts with a real round-trip and only calls
them connected when that passes. Stop the dashboard with Ctrl-C when you are
done.

**The terminal.**

```bash
beertown bootstrap
```

It opens a Chrome window on the tool's profile. Sign in to Google, then to
untappd.com, and close the window. The session is captured when Chrome
closes.

**Either way, confirm it:**

```bash
beertown verify --json
```

Read `data.ran` before `ok`. `ok: true` means both accounts work.
`ok: false, ran: true` means one is signed out, and `data.accounts` says
which. `ran: false` means the check itself could not run (usually no Chrome),
which is a different problem: do not sign in again, fix what the message
names.

A session that worked this morning can expire by evening. `verify`'s verdict
is kept for 12 hours; after that `status` asks for it again.

---

## 4. Choose a city

There is no default city. Either press **Use this city** on the last step of
`beertown ui` (it records your choice in `state/intent.json`, which `status`
reads), or pass `--city "<city>"` to each command.

Use the name the Untappd app's map search understands: an ordinary city name
like `Tel Aviv`, `Lisbon` or `Portland, OR`. The same string names the output
folder (`data/tel-aviv/`), so keep it the same across stages.

---

## 5. Open Discover -> View Map in the Untappd app

Before **every** sweep:

1. Bring BlueStacks to the front and open the Untappd app.
2. Tap **Discover** in the bottom bar.
3. Tap **View Map**.
4. Close anything covering the map: a venue card, a dialog, the keyboard.

Leave it there. The sweep checks that the map is in front before it starts and
refuses with `app_screen_unexpected` if it is not. Do not use the list view
next to the map, and do not use "Nearby Venues": both show a fraction of what
the map holds (see [HARVESTING.md](HARVESTING.md), section 3).

Do not touch the emulator while a sweep is running. A tap or a swipe from you
is indistinguishable from one of the sweep's, and it will either stop with
`app_pan_failed` or record the wrong area.

---

## 6. Sweep

```bash
beertown sweep --city "Tel Aviv" --json
```

What it does:

1. Places the city with the geocoder (OpenStreetMap's Nominatim unless you set
   `GOOGLE_GEOCODING_KEY`) and types it into the app's map search.
2. Sets the app's category filter to drinking places, then searches. The
   result set is capped at about 60 venues per search, so it splits the map
   into overlapping quarters and searches each, and splits again wherever a
   quarter still comes back full. By default it always splits once
   (`--min-depth 1`) and never more than three times (`--max-depth 3`).
3. Reads every map pin (name and screen position) from the app with one
   `uiautomator dump` per view. No screenshots, no tapping on venues.
4. **Places** the pins: matches swept names to OpenStreetMap venues fetched
   from Overpass, fits the map's real scale from those matches, and converts
   every pin to a latitude and longitude. If the fit cannot be trusted it
   refuses with `calibration_failed` rather than writing plausible, wrong
   coordinates.

Writes `data/<slug>/1_sweep.csv`. The sweep journals its progress per city,
so an interrupted sweep resumes rather than restarting.

Options:

- `--here` centres on the emulator's own GPS position (it taps the map's
  **Reset location** control) instead of searching the city by name. Use it
  when the geocoder places the city somewhere the app does not, or for a
  neighbourhood rather than a city. Set the position first with BlueStacks'
  own location setting (this guide does not name the menu: it was not
  recorded and differs between BlueStacks versions). You still pass
  `--city`; it names the output folder. If the Reset location control is not
  on screen, the sweep stops with `app_screen_unexpected`.
- `--title "<list name>"` records the Google Maps list the results are meant
  for, so later hints name it.
- `--format kml,gpx,geojson` also writes `1_sweep.<fmt>` map files next to
  the CSV. `export` is the usual way to get map files.
- `--min-depth` / `--max-depth` bound the splitting. Going shallower is
  faster and loses mostly the less-visited venues; see
  [Measured quality](#measured-quality).

**Time.** Measured once: 507 s (about 8.5 minutes) at depth 1 for Tel Aviv,
before the filter pass was tuned. Every further level can multiply the number
of searches by up to four, so a large, dense city at depth 3 can take an hour
or more; that figure is an estimate, not a measurement. The waits between
gestures (5 to 8 seconds after every search) are deliberate: a map redrawing
mid-read returns fewer pins, and that reads exactly like a finished area.

The envelope reports `data.venues`, `data.truncated_cells` and
`data.hit_depth_limit` (cells still full when the depth limit stopped them),
and `data.calibration.median_residual_m`, the fit's error in metres.

**Network.** The geocoder (unless `--here`) and Overpass, both
OpenStreetMap services, no key. Overpass is volunteer-run and sometimes
returns 504 under load; that is `overpass_unavailable`, and waiting fixes it.

---

## 7. Enrich

```bash
beertown enrich --city "Tel Aviv" --json
```

For each swept venue, looks its name up on untappd.com (in a visible Chrome
window on the tool's profile), ranks the results so those whose card names
the city come first, opens at most two candidate venue pages, and accepts a
page **only if the coordinates the venue published on it are within 1 km of
where the map put the pin**. When no result names the city, one more search
with the city added (`Mike's Place Tel Aviv`) is tried first. An accepted
venue gets its Untappd id, page link, check-in totals (total, unique, this
month, yours) and the page's own coordinates, which are more precise than the
pin.

A name that does not resolve stays in the output with its counts blank:
unknown, never zero, never dropped. The `resolution` column and the
envelope's `data.statuses` say why (`too_far`, `no_match`, `unverified`,
`unlocated`, `fetch_failed`, `search_failed`, `duplicate`).

Writes `data/<slug>/2_enriched.csv`. Needs the Untappd web session from step
3: signed out, the pages hide their totals. If Untappd's robots.txt
disallows the venue pages, it stops with `robots_disallow` and fetches
nothing.

**Time.** One to four paced requests per venue (a name search, sometimes a
city-qualified search, and up to two page fetches), 2 to 4.5 seconds apart.
Searches and page fetches count against the same limit of 600 requests an
hour, persisted across restarts; a large city can hit it and stop with
`rate_limited`, in which case run the same command again later. Searches and
pages are cached for 12 hours, so a re-run is nearly free. Roughly 10 to 20
minutes for 90 venues is an estimate, not a measurement.

---

## 8. Filter

```bash
beertown filter --city "Tel Aviv" --json
```

Keeps the beer venues and sets the rest aside, each with the reason. Even with
the app's drinking filter on, a sweep picks up the odd supermarket, highway
or hotel that people log beers at.

Writes `data/<slug>/3_venues.csv` (the map) and `data/<slug>/3_excluded.csv`
(everything left out). Both carry three added columns: `kind` (`brewery`,
`bottle_shop`, `beer_bar`, `bar`, `uncategorised` or `excluded`), `reason`
(why it was kept or left out) and `flag` (`closed`, `possibly_closed` or
empty). The rules are the category vocabulary in
`beer_in_this_town/classify.py`; a venue that looks private (many check-ins
from very few people) is left out, and a venue with no category is kept.
A flagged venue is kept: removing it is your decision. Read the excluded file
once: if a bar you know is in it, that is worth an issue. Offline, seconds.

---

## 9. Export map files

```bash
beertown export --city "Tel Aviv" --json
beertown export --city "Tel Aviv" --format gpx --json
```

Writes `data/<slug>/venues.kml`, `venues.gpx` and `venues.geojson`: all
three by default, or only the formats named in `--format`. These are a backup of the map and a way onto
other map apps: Organic Maps and OsmAnd import GPX or GeoJSON as bookmarks on
their everyday map, with no account. They are not how the venues reach Google
Maps; that is steps 10 to 12. Offline, seconds.

---

## 10. Create the saved list in Google Maps

The tool never creates a list; it only saves places into one you made.

- **On a phone (Google Maps app):** tap **You** (or **Saved**) in the bottom
  bar, then **+ New list**. Name it, choose Private or Shared, **Save**.
- **On a computer (google.com/maps):** open the menu, **Saved**, then
  **+ New list**. Name it and save.

Use the Google account you signed in with in step 3. Pick a name that no
other list of yours contains as part of its name: `--list` must match exactly
one list, and `pin` refuses with `list_ambiguous` rather than guess.
Use only letters, digits and spaces: the commands the tool prints for you
drop shell symbols such as `&`, `|`, `$`, `;`, `!` and quotes from names, so
a list called `Beer & Bars` would be shown as `Beer  Bars`, which is not its
name.

Saved lists hold at most 3000 places.

---

## 11. Pin: three, check, then the rest

`pin` drives Google Maps in the tool's Chrome profile to save each venue into
your list. **This automates the Google Maps UI, which Google's terms of
service do not allow.** It is your choice whether to use it; see
[Where the lines are](../README.md#where-the-lines-are).

### Trial

```bash
beertown pin --csv data/tel-aviv/3_venues.csv --list "Tel Aviv Beer" --limit 3
```

A Chrome window opens and works on its own for a minute or two. Leave it
alone. When it finishes, check in Google Maps (phone or browser):

1. The list has exactly the three places the envelope names.
2. Each is the right place: the right bar at the right address, not a
   namesake across town.
3. Nothing landed in a different list.

If any of those is wrong, stop and open an issue with the envelope. Do not run
the rest.

### The rest, over several nights

```bash
beertown pin --csv data/tel-aviv/3_venues.csv --list "Tel Aviv Beer"
```

`pin` is limited to **60 saves per run and 100 per rolling 24 hours**, and
that budget is kept on disk, so restarting does not reset it. `notes`
spends the same budget. A city of 90 venues is therefore at least two
evenings of pinning and one or two more of notes. When a run says
"Trimming this run to N places", that is the budget working; run the same
command again the next day. It skips everything already saved and retries
failures.

Pacing is 8 to 16 seconds between saves, and every save is verified by
reloading the place. Roughly half a minute per place is an estimate, not a
measurement.

If Google shows a CAPTCHA, an "unusual traffic" page or signs you out, `pin`
stops at once and will not write for 6 hours. Do not work around that: it is
what keeps the account from being flagged.

A place recorded as `not-found` means Google Maps had no match for that name
and address. It is not a bug; add it by hand if you want it.

---

## 12. Notes

```bash
beertown notes --csv data/tel-aviv/3_venues.csv --list "Tel Aviv Beer" --limit 3
beertown notes --csv data/tel-aviv/3_venues.csv --list "Tel Aviv Beer"
```

Writes each place's Untappd numbers into its note in the saved list, for
example:

```
Untappd #45 | 2,628 check-ins | 807 unique | 10/month | as of 2026-08-20
```

Same terms-of-service position as `pin`, same budget, same trial-first
habit. It only touches places already in the list; a note that already
matches is left alone. Pacing is 5 to 11 seconds between notes.

---

## Optional: closure check (paid)

Untappd keeps pages for bars that shut years ago, with their check-in history.
To flag those, `closures` asks the Google Places API whether each venue still
trades:

```bash
beertown closures --csv data/tel-aviv/3_venues.csv --limit 3 --json
beertown closures --csv data/tel-aviv/3_venues.csv --json
```

It needs `GOOGLE_PLACES_KEY` with the Places API (New) enabled and billing on.
It bills the Text Search Pro SKU: 5,000 lookups a month free, then $25.60 per
1,000. It writes a **new** CSV with a `business_status` column and never
changes the input. Closed venues are flagged, not removed. A venue Places
cannot find is `unmatched`, which is not the same as closed.

---

## Measured quality

Everything here was measured on 2026-09-21 against the live app in BlueStacks.
The full record, including what did not work, is in
[HARVESTING.md](HARVESTING.md).

| What | Result |
|---|---|
| Tel Aviv, filtered to drinking places, depth 1 | **84 venues**, 2 of them junk-looking, 507 s |
| Singapore, recall against a known top-100 list | **65%** at adaptive depth up to 3 (5% with one search, 56% with one forced split) |
| Placement after calibration, against OpenStreetMap | **18 m median** (about 600 m before calibration) |
| Enrichment, Tel Aviv, 33 swept names | **29 matched** to their venue page; 3 refused as too far, 1 no match |
| Venue page coordinates vs the placed pin | **20 m median**, 90th percentile 53 m, worst 201 m |

## Limits

Read these before you trust a map this tool made.

- **One device calibration.** The screen geometry and the swipe behaviour were
  measured on one BlueStacks 5 instance at 900 x 1600. Another emulator,
  resolution or app version may need re-measuring. `calibration_failed` and
  `app_pan_failed` are the tool noticing; it cannot notice everything.
- **Popularity bias.** Each map search returns roughly the 60 most
  checked-in venues in view. Splitting finds more, but what it adds is mostly
  the less-visited tail. A shallow sweep is a good list of the places people
  actually go; it is not every place.
- **Not a census.** 65% recall in Singapore is the only external measurement,
  and it was taken against a top-100 list, so it says nothing about the
  quiet end. Venues that are not on Untappd at all are not found by any
  method here. The envelope reports cells that were still full at the depth
  limit; a sweep that stopped there is saying so.
- **Names are the weak join.** Enrichment accepts a page only when its
  published position agrees with the pin, so a wrong match is rare, but a
  venue with no published position, or a chain branch a street away, can go
  unmatched and keep blank counts.
- **Positions.** About 20 m typically; up to about 100 m where markers are
  crowded, because the app draws overlapping markers displaced. Fine for
  finding the bar; not an address.

---

## Troubleshooting

Every command prints one JSON envelope with `--json`. On failure it carries
`error.code` and `error.remedy`. By code:

| Code | What happened | What to do |
|---|---|---|
| `emulator_unavailable` | `doctor`'s emulator checks failed before a sweep | Run `beertown doctor --json` and fix the first failing check: adb on `PATH`, `adb connect`, Untappd installed, screen 900 x 1600. |
| `adb_unavailable` | `adb` could not reach the device, or returned nothing usable | Is BlueStacks running? `adb connect 127.0.0.1:5555`, then `adb devices`. Check `BEERTOWN_ADB_SERIAL` if you changed the port. |
| `app_screen_unexpected` | The Untappd app was not on the map, or (with `--here`) the map's Reset location control was not on screen | Open Discover -> View Map, close any card or dialog, and re-run. For `--here`, allow Untappd location permission. The sweep resumes where it stopped. |
| `app_pan_failed` | Swipes stopped moving the map three times in a row | Something is over the map (venue card, dialog, keyboard). Clear it and re-run. Check the screen is still 900 x 1600. |
| `calibration_failed` | Too few swept venues matched OpenStreetMap to fit positions safely | Nothing was written. Usually a very small area or a city with sparse OSM data; sweep a larger area, or retry later if Overpass was struggling. |
| `city_not_found` | The geocoder has no match for the city | Spell it the way a map would, add the country (`Portland, OR`), or use `--here`. |
| `overpass_unavailable` | OpenStreetMap's Overpass API is busy or down | Wait and re-run, or set `OVERPASS_URL` to a mirror. |
| `geocoder_unavailable` | The geocoder refused or is unreachable | Check the network; if you set `GOOGLE_GEOCODING_KEY`, check its key and billing. |
| `stage_input_missing` | The previous stage's file is not there | Run the command the remedy names (for example `enrich` before `filter`), or pass `--in PATH`. |
| `bad_format` | `--format` named something other than `kml`, `gpx`, `geojson` | Fix the list. |
| `no_city` | No city given and none chosen in the dashboard | Pass `--city`, or choose one in `beertown ui`. |
| `not_signed_in` | Google or Untappd is signed out in the tool's profile | Sign in again (step 3), then `beertown verify --json`. |
| `verify_unavailable` | The account check could not run | Usually no Chrome. Fix what the message says; you are not necessarily signed out. |
| `robots_disallow` | Untappd's robots.txt disallows the venue pages `enrich` reads | Nothing was fetched. Stop; there is no flag to override it. |
| `rate_limited` | The 600-an-hour read budget is spent, or Untappd answered with repeated 429s | Wait, then re-run the same command; finished work is cached. |
| `selectors_stale` | `selfcheck`: Untappd changed its venue page markup | Open an issue; a parser fix is needed. |
| `stats_missing` | `selfcheck`: the venue page parsed but showed no check-in stats | Usually signed out of untappd.com (step 3); otherwise a parser fix is needed. |
| `fetch_failed` | `selfcheck` could not fetch its known-good page | Check your connection. Repeated 403s mean a block: stop. |
| `network_unavailable` | Several requests in a row failed to connect | Check your connection and re-run. |
| `no_list` | `pin`/`notes` without `--list` | Pass the exact list name. |
| `list_missing` | No saved list with that name | Create it (step 10) or fix the name. |
| `list_ambiguous` | The name matches more than one list, or none exactly | Use the exact, full name, or rename a list so only one matches. Nothing was saved. |
| `already_running` | Another `pin`/`notes` holds the write budget | Wait for it to finish. Do not delete the lock. |
| `guardrail_tripped` | A write guardrail tripped: circuit breaker, block detected, or cool-off active | Stop. Wait out the cool-off that `beertown status --json` reports. |
| `pin_failed` | `pin` stopped on an unexpected error | Re-run; it resumes. If it repeats, open an issue with `-v` output. |
| `notes_failed` | `notes` stopped on an unexpected error | Re-run; it resumes. |
| `chrome_not_found`, `no_profile`, `login_timed_out`, `login_not_detected` | `bootstrap` could not find Chrome, had no profile to capture, was left open past `--timeout`, or saw no Google sign-in | Install Chrome; run `bootstrap` (not `--capture`); close Chrome and run `bootstrap --capture`; sign in fully before closing the window. |
| `places_key_missing` | `closures` without `GOOGLE_PLACES_KEY` | Set the key, or skip the closure check. |
| `places_unavailable` | Places API refused or is out of quota | Check the key, that Places API (New) is enabled, and billing. |
| `bad_arguments` | An argument was refused, often a pacing value below its floor | The floors protect your account and cannot be lowered. |
| `interrupted` | Ctrl-C | Re-run the same command; progress is journalled. |
| `unexpected_error` | A bug | Re-run with `-v` and open an issue with the output. |

A tripped write guardrail (circuit breaker, block detected, daily budget)
means stop. Wait for the time it states; never delete `state/rate_ledger.json`
to get a fresh allowance.
