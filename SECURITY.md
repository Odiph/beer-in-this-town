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

## What it does not do

- No credentials are handled in code. The login happens in a real browser window
  that you drive; the tool only reads cookie *names* to detect whether a session
  exists (see `chrome_launch.py`).
- No telemetry, no network calls other than to Untappd, Google Maps, and your
  chosen geocoder.
