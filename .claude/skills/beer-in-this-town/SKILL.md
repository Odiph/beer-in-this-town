---
name: beer-in-this-town
description: Make a craft-beer map of a city in a Google Maps saved list - sweep the Untappd Android app's map on an emulator (BlueStacks + adb), match each venue to its Untappd page for check-in stats, filter to beer venues, export KML/GPX/GeoJSON, and (only when the user asks) pin them into a saved list with the stats in each note. Use when the user wants bar/brewery/taproom data for a city, Untappd venue stats, or a beer map on Google Maps.
---

# beer-in-this-town

A pipeline from the Untappd app's map to a Google Maps saved list. You drive
it one command at a time; the tool owns its own state.

## Before anything

Read `AGENTS.md` in the repo root. It is the contract: envelope shape, the
playbook (including the exact message to give the human at each manual step),
error codes, and the rules about ToS-sensitive commands. `docs/FLOW.md` is the
same flow written for a person.

## The loop

Always start here:

```bash
python -m beer_in_this_town status --json
```

It reports each city's stages and gives you `next_actions` — literal commands,
best first. Run the first one, read its envelope, repeat. Stop when
`next_actions` is empty, then read `data.blocked_on`:

- `null` — done. Relay the `hints` (saved list, `pin`, `notes`) to the user.
- `"choose_city"` — ask the user which city. Never pick one.
- `"emulator"` — run `doctor --json` and give the user the first failing
  check's `remedy` (BlueStacks, 900x1600 portrait, ADB on, Untappd installed).
- `"sign_in"` — run `ui --detach --json`, give the user `data.url`, ask them
  to sign in to Google and Untappd, then `verify --json`.

## The stages

```bash
python -m beer_in_this_town doctor --json
python -m beer_in_this_town verify --json
# human: Untappd app on Discover -> View Map, hands off the emulator
python -m beer_in_this_town sweep  --city "<city>" --json   # -> data/<slug>/1_sweep.csv
python -m beer_in_this_town enrich --city "<city>" --json   # -> 2_enriched.csv
python -m beer_in_this_town filter --city "<city>" --json   # -> 3_venues.csv, 3_excluded.csv
python -m beer_in_this_town export --city "<city>" --json   # -> venues.kml/.gpx/.geojson
```

`sweep` takes minutes to an hour; give it a long timeout and do not interrupt
it. It resumes from its journal if it fails. `app_screen_unexpected` means the
app is not on the map: ask the user to open Discover -> View Map, then re-run.

## Hard rules

- **Never** run `pin` or `notes` unless the user explicitly asked for a Google
  Maps saved list. They automate the Maps UI, which is against Google's ToS.
  The user creates the list by hand first (Saved -> New list) and gives you its
  exact name.
- First `pin` / `notes` run always uses `--limit 3`. Report, wait for the user
  to check the list, then continue. The rest spans several days under the
  100/day budget; do not schedule it.
- **Never** run `closures` unasked: it bills the user's Places key.
- **Never** pass `--i-read-robots`. Human's call.
- Never lower `--min-gap` / `--max-gap` / `--delay`, never delete
  `state/rate_ledger.json`, never retry past a tripped guardrail.
- Do not tap, type, install or sign in on the emulator yourself. `sweep` is
  the only thing that drives the app. Manual steps are instructions for the
  user, not something to automate.
- If a quality gate or `calibration_failed` fires, that is a real finding.
  Report it; do not work around it.

## Interpreting results

- A sweep is not a census. Relay truncated cells / depth-limit warnings.
- Unresolved venues after `enrich` have **unknown** counts, not zero.
- `3_excluded.csv` lists what `filter` dropped and why; mention the counts.
- A `not-found` place in `pin` means Google Maps had no match for that name
  and address; it is not a bug, but worth listing.
