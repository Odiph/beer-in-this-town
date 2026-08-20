---
name: Selectors stopped working
about: Untappd or Google Maps changed their markup
labels: selectors
---

Untappd and Google Maps both change their markup without notice. This project
is built to fail loudly when that happens rather than emit wrong data, so a
`selectors_stale` or `corpus_quality_gate` error is expected behaviour, not a
crash.

**Which command**  `selfcheck` / `run` / `pin` / `notes`

**Error code from the envelope**

**What the page looks like now** — the specific element that moved, ideally with
the old and new markup for just that fragment.

> Please don't paste whole files from `debug/`; they contain your session.
