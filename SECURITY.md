# Security

## Reporting

Please report security issues privately via GitHub's **Report a vulnerability**
button on the Security tab, rather than opening a public issue.

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
  Google. Otherwise Nominatim (OpenStreetMap) is used. No key is ever written to
  disk by this tool; it is read from the environment.
- **The closure check.** If `GOOGLE_PLACES_KEY` is set *and* you ask for the
  check -- `closures`, or `run --check-closed` -- each venue's name, address
  and city are sent to the Google Places API to read its `businessStatus`.
  Nothing is sent without that flag or that command, and a key alone does not
  start it.

  This is a deliberately separate key from `GOOGLE_GEOCODING_KEY`, and the
  separation is a privacy control rather than a billing convenience: sending an
  address to place a pin and sending it to ask Google what the business there
  is are different disclosures, and you can do the first without the second.

  The reply is cached in `state/places_cache.json`, which is gitignored. It
  holds the place id, display name, types and status for each venue queried --
  Google's description of a public business, not anything about you.

- **The setup dashboard (`ui`).** It opens a local HTTP server that can launch
  Chrome on your profile and write `storage_state.json`, so it is the most
  sensitive surface here. "Localhost" is not a boundary — any page in your
  browser can send requests to `127.0.0.1` — so it carries four independent
  protections: it binds `127.0.0.1` only; every API request needs a random
  key minted at startup and handed over in the URL fragment, which browsers
  never send in a `Referer`; the `Host` header is checked against an
  allow-list, which is what stops DNS rebinding; and a cross-site `Origin` is
  refused outright.

  The strongest protection is what is absent: there is no route on that server
  that can `pin` or write `notes`. Nothing it serves reaches off-machine, and
  its page is served with `default-src 'none'`.

## What it does not do

- No credentials are handled in code. The login happens in a real browser window
  that you drive; the tool only reads cookie *names* to detect whether a session
  exists (see `chrome_launch.py`).
- No telemetry, no network calls other than to Untappd, Google Maps, your
  chosen geocoder, and -- only when you ask for it -- the Places API.
