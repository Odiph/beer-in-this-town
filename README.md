# beer in this town

[![CI](https://github.com/Odiph/beer-in-this-town/actions/workflows/ci.yml/badge.svg)](https://github.com/Odiph/beer-in-this-town/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Every good beer place in a city, on the map already in your pocket.**

Name a city. Get its beer venues as a saved list in Google Maps -- the app you
already open when you are standing on a street deciding where to go -- with
each place's Untappd numbers written into its note:

```
Untappd #45 | 2,628 check-ins | 807 unique | 10/month | as of 2026-08-20
```

Four bars on this street, and the map tells you which one people keep coming
back to. No new app, no tab to keep open, and it works on the phone in your
hand when you land somewhere new.

- **Any city.** London came back with 611 venues in 40 minutes.
- **Real numbers, not vibes.** Check-ins, unique drinkers and this month's
  activity, straight from each venue's Untappd page.
- **Yours to keep.** Also exports KML, GPX and GeoJSON, so the same map opens
  in Organic Maps, OsmAnd or anything else -- no account required.
- **Free.** No API keys, no billing; the geocoding is OpenStreetMap.
- **Built for people and for coding agents.** One command per step, every
  command speaks JSON, and `status` always says what to run next -- so you can
  follow it yourself or hand the whole thing to Claude Code.

## How it works

```
  Untappd app (Android, in BlueStacks)         untappd.com venue pages
  Discover > View Map                          (signed in)
          |                                            |
          v                                            v
   1. sweep  ----->  2. enrich  ----->  3. filter  ----->  4. export
   read the map's     match each name     keep the beer     KML / GPX / GeoJSON
   pins over adb,     to its venue        venues, set the   (backup, other
   place them with    page: id, check-    rest aside with   map apps)
   OpenStreetMap      in totals, exact    a reason
          |           coordinates                |
          v                                      v
   data/<city>/1_sweep.csv  2_enriched.csv  3_venues.csv  venues.kml ...
                                                 |
                              you create a saved list in Google Maps
                                                 |
                                                 v
                              5. pin    save each venue into the list
                              6. notes  write its numbers into the note
```

Why an Android emulator: **untappd.com has no geographic venue search.** The
map in Untappd's own app is the only geographic search Untappd has, so by
default the tool reads that map, over `adb`, from an emulator.

There is a second way, with no emulator: `sweep --method search`. Signed in,
the website's search matches a venue's name *and* its city line, and ranks by
check-ins. The tool searches each common spelling of the city's name (from
Foursquare's open places data: "tel aviv", "תל אביב", "jaffa") and keeps each
spelling's most-checked-in venues, up to the site's 1,000. Measured against
the map: London's search held 99 of the map sweep's top 100 venues, and
Tel Aviv's held all of its top 100 beer venues, where the map had 20. It is
text, not geography, so it misses what is listed under another spelling and
catches namesakes elsewhere, which `enrich` drops by position. The full
research record — what was tried, what was measured, which surfaces are traps
— is [docs/HARVESTING.md](docs/HARVESTING.md).

## Prerequisites

All of these are needed for the full flow. None is optional.

| | Why |
|---|---|
| **Python 3.11 or 3.12** | Runs the tool. CI tests both, on Linux and Windows. |
| **Google Chrome** (or Chromium) | The tool's own Chrome profile holds your Google and Untappd sessions; it reads venue pages and drives Google Maps through it. |
| **BlueStacks 5**, at its default 1600 x 900 screen, with Android Debug Bridge on (not needed with `--method search`) | Runs the Untappd Android app, which it draws at 900 x 1600 -- the size the sweep is calibrated for. Tested on Windows only. |
| **adb** (Android platform-tools) on your `PATH` (not needed with `--method search`) | How the tool reads the app's map. |
| **An Untappd account** | Signed in to the app in BlueStacks (for the map), and on untappd.com in the tool's profile (venue stats are only shown to signed-in visitors). |
| **A Google account** | The saved list lives there. You create the list by hand; the tool never creates one. |

No API keys are needed. The sweep uses OpenStreetMap's Nominatim geocoder and
Overpass API, free and keyless. Optional environment variables:

| Variable | Effect |
|---|---|
| `NOMINATIM_EMAIL` | Sent as the contact address OSM's usage policy asks for. |
| `GOOGLE_GEOCODING_KEY` | Use Google's geocoder instead of Nominatim. City names are then sent to Google; see [SECURITY.md](SECURITY.md). |
| `OVERPASS_URL` | An Overpass mirror, for when the main endpoint is busy. |
| `BEERTOWN_ADB_SERIAL` | The emulator's adb address, if not `127.0.0.1:5555`. |
| `GOOGLE_PLACES_KEY` | Enables the optional, paid closure check (`closures`). |

## Quickstart

**[docs/FLOW.md](docs/FLOW.md) is the step-by-step guide**, with every manual
step detailed: installing BlueStacks and adb, the display setting and why,
signing in, creating the saved list, and troubleshooting by error code.
The short version:

```bash
git clone https://github.com/Odiph/beer-in-this-town.git
cd beer-in-this-town
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e ".[browser]"

adb connect 127.0.0.1:5555             # BlueStacks, ADB turned on
beertown doctor --json                 # emulator, Chrome, session
beertown ui                            # sign in to Google + Untappd, pick a city
beertown verify --json                 # both accounts actually work

# in BlueStacks: Untappd > Discover > View Map, then hands off
beertown sweep  --city "Tel Aviv" --json
beertown enrich --city "Tel Aviv" --json
beertown filter --city "Tel Aviv" --json
beertown export --city "Tel Aviv" --json

# in Google Maps: Saved > New list, e.g. "Tel Aviv Beer"
beertown pin   --csv data/tel-aviv/3_venues.csv --list "Tel Aviv Beer" --limit 3
beertown notes --csv data/tel-aviv/3_venues.csv --list "Tel Aviv Beer" --limit 3
```

`python -m beer_in_this_town <command>` is equivalent to `beertown <command>`
everywhere.

### The human path

`beertown ui` (or a bare `beertown` at a terminal) opens a setup wizard on
`localhost` that walks through the same steps: the emulator, the accounts, the
city, and a copyable command for each stage with its live status. It signs you
in, then **tests both accounts with a real round-trip** and only calls them
connected once that passes. A cookie on disk means a login happened once, not
that the account works now.

The dashboard **cannot write to your Google account**: `pin` and `notes` have
no button there and no route on that server. It binds to `127.0.0.1` only and
needs the key from the URL the terminal prints; [SECURITY.md](SECURITY.md)
explains why.

### The agent path

Every command emits exactly one JSON envelope on stdout, with `next_actions`
holding literal runnable commands, best first. An agent runs `status --json`,
executes the first entry, and repeats. When a person is needed — BlueStacks,
a password, a city, the saved list — `next_actions` is empty and
`data.blocked_on` says why.

**[AGENTS.md](AGENTS.md)** is the contract: the envelope, the playbook with
the exact message to give the human at each manual step, every error code, and
the rules. The Claude Code skill is
`.claude/skills/beer-in-this-town/SKILL.md`.

## Outputs

Everything lands in `data/<slug>/`, where `<slug>` is the city lower-cased
with non-alphanumerics as `-` (`Tel Aviv` → `data/tel-aviv/`):

| File | Written by | What |
|---|---|---|
| `1_sweep.csv` | `sweep` | every venue on the app's map, with calibrated coordinates |
| `2_enriched.csv` | `enrich` | plus Untappd id, page link, check-in totals, page coordinates |
| `3_venues.csv` | `filter` | the beer venues: this is what `pin` and `notes` read |
| `3_excluded.csv` | `filter` | what was left out, and why |
| `venues.kml`, `venues.gpx`, `venues.geojson` | `export` | map files: a backup, or bookmarks in Organic Maps / OsmAnd |

A venue that could not be matched to its page stays in, with its counts
blank: unknown, never zero.

## How good is it

Measured on 2026-09-21 against the live app:

- **Tel Aviv:** 84 venues from a filtered depth-1 sweep, 2 of them
  junk-looking.
- **Singapore:** 65% recall against a known top-100 list at the default
  depth.
- **Placement:** 18 m median error against OpenStreetMap after calibration.
- **Enrichment:** 29 of 33 names matched to their venue page, whose own
  coordinates sat a median 20 m from the placed pin.

It is not a census. Each map search returns roughly the most-visited 60
venues in view, so the sweep finds the places people go and thins out towards
the quiet end, and a venue that is not on Untappd is not found at all. The
calibration was measured on one emulator setup. [docs/FLOW.md](docs/FLOW.md#limits)
has the limits in full.

## There is no API for saved lists (we checked)

Google Maps has **no legitimate way to bulk-add places to a saved list**. As
of August 2026:

- **No Maps Platform API** has any write surface for user saved places.
- **The Data Portability API** covers Maps starred places, but every Maps
  scope is export-only.
- **No documented URL parameter or Android intent** saves a place. Maps URLs
  support search, directions, display and Street View.
- **No OAuth scope** grants saved-list write, so no third-party service can
  legitimately offer it either.
- Google knows: [issue 453378725](https://issuetracker.google.com/issues/453378725)
  ("Bulk Save to Google Maps Lists", filed Oct 2025) is open and unanswered.

That is why `pin` exists, and why it drives the Maps UI. If you would rather
not, the supported alternatives are: import `venues.gpx` or `venues.geojson`
into Organic Maps or OsmAnd (bookmarks on the everyday map, no account), or
add the places to a list by hand using Maps' own multi-select on Android and
then share the list. Saved lists cap at 3000 places.

## Using other people's services

This tool works by using three services the way you would, only faster and
without getting bored: it reads the Untappd app's map, reads Untappd venue
pages, and saves places into Google Maps. It is worth a minute of your
thought, because the accounts involved are yours.

| What it touches | Commands | Worth knowing |
|---|---|---|
| The Untappd Android app, over adb | `sweep` | Untappd's terms are written for people tapping, not scripts; automated access is not something they invite. The sweep reads the map and never taps a venue, checks in or posts anything. |
| Untappd venue pages | `enrich`, `selfcheck` | Same terms, same thought. Public pages, read at reading speed, signed in as you. |
| Google Maps saved lists | `pin`, `notes` | Google's terms ask you not to use the service through automated means. [There is no API for saved lists](#there-is-no-api-for-saved-lists-we-checked), so this drives the interface the way you would. Your account, your call. |
| OpenStreetMap: Nominatim and Overpass | `sweep` | Fine within their usage policies, which the tool follows: a contact header, modest queries, results cached. Data is © OpenStreetMap contributors (ODbL). |
| Google Places API | `closures` (optional) | A supported API, billed to your own key. |
| KML / GPX / GeoJSON files | `export` | Files on your disk. Nothing to consider. |

**How the tool tries to be a good guest.** Every request is paced and
jittered, with a 600-an-hour ceiling and a 12-hour cache, so a whole city
generates less traffic than an enthusiastic evening of browsing. The sweep
waits 5-8 seconds after each search. `enrich` pauses between venues and takes
a break every twenty or so. None of this makes automated use *permitted* --
it makes it considerate, and keeps you well clear of looking like a problem.

**The account-writing parts are opt-in, and stay that way.** `pin` and `notes`
never run as part of anything else, are never offered to a coding agent as a
next step, and never create a list -- you make it yourself. They are wrapped
in guardrails that fail closed: 100 writes a day, a circuit breaker, CAPTCHA
detection, and a six-hour cool-off, all persisted to disk. Start with
`--limit 3` and look at the result before doing more.

**If you would rather not touch Google at all**, stop after `export`: the same
venues come out as KML, GPX and GeoJSON for Organic Maps, OsmAnd or any other
map app, and your Google account is never opened.

Read [Untappd's terms](https://untappd.com/terms) and [Google's](https://policies.google.com/terms)
and decide what you are comfortable running. This project is not affiliated
with Untappd, Google or BlueStacks, and nothing here is legal advice.

## Commands

| Command | What it does | Touches your accounts |
|---|---|---|
| `status` | Where each city's stages are, what to run next | no |
| `doctor` | Dependencies, emulator checks, session | no |
| `ui` | Setup wizard: emulator, accounts, city, stage commands | signs in |
| `bootstrap` | Sign-in from the terminal (opens a real Chrome) | signs in |
| `verify` | Test both accounts actually work (no window) | reads |
| `selfcheck` | One request: can a known venue page still be parsed | no |
| `sweep` | Read the Untappd app's map and place the venues; `--method search` uses Untappd's web search instead (`--sort recent` for what is popular now) | drives the app; reads |
| `enrich` | Match each venue to its Untappd page: id, stats, coordinates | reads |
| `filter` | Keep beer venues, record the rest with reasons | no |
| `export` | KML / GPX / GeoJSON map files | no |
| `pin` | Save each venue into a Google Maps list | **writes** |
| `notes` | Write stats into each saved place's note | **writes** |
| `closures` | Ask Google Places whether each venue still trades (paid) | no |
| `label` | Emit a sample to check the venue heuristics by hand | no |
| `score` | Report how accurate those heuristics are | no |

## Failing loudly

Silent wrong data is the dangerous failure mode, and this project's surfaces
almost never fail loudly on their own: a dead scroll, the wrong app in front
or a map mid-redraw each return a plausible number. So the guards manufacture
the loudness:

1. The sweep refuses to read anything but the map screen, re-searches every
   area it counts, and calls a pan dead only on a quorum of evidence.
2. Positions are fitted against OpenStreetMap on every sweep; a fit that
   cannot be trusted writes nothing (`calibration_failed`) rather than
   plausible, wrong coordinates.
3. A venue page is accepted only if its own coordinates are within 1 km of
   the pin, so a namesake across the world cannot lend its numbers.
4. Unknown counts stay blank. Nothing is ever filled in with zero.
5. Selector misses raise, and every parse failure dumps the page to `debug/`.

The pacing numbers have floors: they can be raised but not lowered, because a
guardrail you can switch off with a flag is a suggestion. Each one is
enumerated with its reasoning in
[`http_client.py`](beer_in_this_town/http_client.py) (reads) and
[`guardrails.py`](beer_in_this_town/guardrails.py) (writes). Read those before
changing any number in `config.py`.

## Tests

```bash
pytest -q -m "not integration"     # offline: no network, no browser, no emulator
```

CI runs the offline suite on Linux and Windows against Python 3.11 and 3.12,
and **never touches Untappd, Google or an emulator**. The emulator code is
tested with no device attached, against `uiautomator` XML.

## Credits

Venue positions are calibrated against, and cities geocoded with, data from
**[OpenStreetMap](https://www.openstreetmap.org/copyright)**, © OpenStreetMap
contributors, available under the Open Database Licence (ODbL). Venue data and
check-in statistics come from Untappd.

## Contributing

Bug reports, selector fixes and measurements from other emulators are welcome —
see [CONTRIBUTING.md](CONTRIBUTING.md) for the ground rules,
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for how we behave, and
[SECURITY.md](SECURITY.md) before reporting anything sensitive or pasting a
`debug/` dump into an issue.

## Licence

MIT. See [LICENSE](LICENSE).

Where this project stands with Untappd's and Google's terms is set out in
[Using other people's services](#using-other-peoples-services).
