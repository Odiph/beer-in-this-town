# Security

## Reporting

Please report security issues privately via GitHub's **Report a vulnerability**
button on the Security tab, rather than opening a public issue.

If that button is not available to you, open a minimal public issue asking for
a private channel -- say only that you have a security report, not what it is
-- and a maintainer will set one up with you.

## What this tool touches

Worth understanding before you run it:

- **A Google session.** `bootstrap` logs into a dedicated Chrome profile stored
  at `chrome-profile/`, and captures cookies to `storage_state.json`. Both are
  gitignored. **Neither should ever be shared or committed** — they grant access
  to the account.
- **An Untappd session**, in the same profile, used to populate the `you`
  check-in column.
- **`debug/` HTML dumps.** When parsing fails, the offending page is written
  there to make the fix easy. Those pages were fetched with your session and can
  contain your username and avatar. Do not paste them wholesale into public
  issues — quote the relevant fragment.
- **Geocoding.** If `GOOGLE_GEOCODING_KEY` is set, venue addresses are sent to
  Google. Otherwise Nominatim (OpenStreetMap) is used, identified by
  `NOMINATIM_EMAIL` if you set it -- Nominatim's policy asks for a contact, and
  the tool warns once when there is none. No key is ever written to disk by
  this tool; it is read from the environment. Results are cached in
  `state/geocache.json`; Google-derived entries expire after 30 days, as
  Google's terms require.
- **The closure check.** If `GOOGLE_PLACES_KEY` is set *and* you ask for the
  check with `closures`, each venue's name, address
  and city are sent to the Google Places API to read its `businessStatus`.
  Nothing is sent without that flag or that command, and a key alone does not
  start it.

  This is a deliberately separate key from `GOOGLE_GEOCODING_KEY`, and the
  separation is a privacy control rather than a billing convenience: sending an
  address to place a pin and sending it to ask Google what the business there
  is are different disclosures, and you can do the first without the second.

  The reply is cached in `state/places_cache.json`, which is gitignored. It
  holds the place id, display name, types and status for each venue queried --
  Google's description of a public business, not anything about you. Everything
  but the place id expires after 30 days, per Google's terms.

- **The setup dashboard (`ui`).** It opens a local HTTP server that can launch
  Chrome on your profile and write `storage_state.json`, so it is the most
  sensitive surface here. "Localhost" is not a boundary — any page in your
  browser can send requests to `127.0.0.1` — so it carries four independent
  protections: it binds `127.0.0.1` only; every API request needs a random
  key minted at startup and handed over in the URL fragment, which browsers
  never send in a `Referer`; the `Host` header is checked against an
  allow-list, which is what stops DNS rebinding; and a cross-site `Origin` is
  refused outright.

  **`ui --detach` writes that key to `state/ui.json`**, because the parent
  process has to learn the URL its child minted. The file is gitignored and
  is deleted when the dashboard stops, but while it exists anyone who can
  read your `state/` directory can drive the dashboard — the same trust
  boundary as the Chrome profile sitting next to it, which holds the actual
  session. A shared machine is the case where that matters; there, run the
  foreground `beertown ui` instead, which never writes the key anywhere.

  The strongest protection is what is absent: there is no route on that server
  that can `pin` or write `notes`, and none that can grant the consent they
  need (below). Nothing it serves reaches off-machine, and its page is served
  with `default-src 'none'`.

- **The consent record (`state/consent.json`).** `pin` and `notes` write to
  your Google account, so they refuse with `no_consent` -- before the
  pre-flight and before any browser opens -- unless you have consented for
  that exact list. You do that with `allow-writes --list "<name>"`, which
  only runs at an interactive terminal and never with `--json`: it explains
  what the writes cross and asks you to type the list's name back. The
  record holds, per list, the list's name and when consent was given and
  expires (7 days by default, 30 at most); nothing about your account. It is
  written atomically and read fail-closed: a missing, corrupt, unreadable,
  expired or over-long entry counts as no consent, and consent for one list
  never covers another. `allow-writes --revoke --list "<name>"` removes it.

  It is a check on the ordinary path, not a lock: anything that can write
  your `state/` directory can write this file, the same trust boundary as
  the rate ledger and the Chrome profile beside it. What it stops is a coding
  agent, following the tool's own instructions, drifting into a write that
  nobody at the keyboard agreed to.

## What it does not do

- No credentials are handled in code. The login happens in a real browser window
  that you drive; the tool only reads cookie *names* to detect whether a session
  exists (see `chrome_launch.py`).
- No telemetry, no network calls other than to Untappd, Google Maps, your
  chosen geocoder, OpenStreetMap's Overpass API (`overpass-api.de`, or the
  mirror in `OVERPASS_URL`: it receives the city centre and a radius, to
  calibrate the sweep), and -- only when you ask for it -- the Places API.
- `sweep --method search` adds two, once per city (the result is cached in
  `cache/city_names/`): Nominatim receives the **city name** to return its
  boundary, whatever geocoder is configured, and `data.source.coop` serves
  byte ranges of Foursquare's public places files. It receives no query,
  only which file ranges are read, which says roughly which region of the
  map you asked about. The spellings it finds are then searched on Untappd
  with your session, like any search you would type.
- The emulator is local. `sweep` talks to it over adb on this machine; with
  `--here` it reads the emulator's GPS fix, which stays on this machine and
  is only used as the map's centre.
