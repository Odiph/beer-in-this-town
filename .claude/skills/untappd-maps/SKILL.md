---
name: untappd-maps
description: Scrape Untappd venue listings for a city (name, category, address, check-in stats), export CSV/KML, and get the results onto Google Maps. Use when the user wants bar/brewery/taproom data for a location, Untappd venue stats, or a map of drinking spots. Also use when asked to refresh or diff a previous pull.
---

# untappd-maps

A pipeline for turning an Untappd venue search into a map. You drive it; the
tool owns its own state.

## Before anything

Read `AGENTS.md` in the repo root. It is the contract: envelope shape, error
codes, and the rules about ToS-sensitive commands. It is short.

## The loop

Always start here:

```bash
python -m untappd_maps status --json
```

It reports every stage and gives you `next_actions` — literal commands, best
first. Run the first one, read its envelope, repeat. Stop when `next_actions`
is empty.

If something looks broken before you start, `doctor --json` checks deps,
session, and geocoder config without hitting the network.

## Typical first run

```bash
python -m untappd_maps doctor --json
python -m untappd_maps bootstrap                 # human must do this (login)
python -m untappd_maps selfcheck --json          # 1 request, are selectors alive
python -m untappd_maps run --query singapore --count 100 --no-upload --json
```

`run` produces a CSV, a KML, and a diff against the previous run. The KML is
the safe way onto Google Maps — import it at mymaps.google.com.

## Hard rules

- **Never** pass `--i-read-robots`. Human's call.
- **Never** run `pin` unless the user explicitly asked for a Google Maps *saved
  list*. It automates the Maps UI, which is against Google's ToS. The supported
  path is the KML import.
- First `pin` run always uses `--limit 3`. Report, then continue.
- Never lower `--min-gap` / `--max-gap`. They protect the user's account.
- If the corpus quality gate fires, that is a real finding: the site's markup
  changed. Read `debug/*.html`, fix `parsers.py`, say what you changed. Do not
  lower the threshold.

## Interpreting results

- `warnings` in the envelope are worth relaying to the user — especially venues
  with no coordinates, which are silently absent from the KML.
- `new_since_last_run` is the interesting number on a repeat run.
- A `not_found` place in `pin` means Google Maps had no match for that name and
  address; it is not a bug, but worth listing.
