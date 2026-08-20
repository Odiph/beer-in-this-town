# Agent contract

This tool is built to be driven by a coding agent. This file is the contract.
If you are an agent, read this and nothing else is required.

## The loop

```
python -m untappd_maps status --json      →  read `next_actions`
run the first action                      →  read the envelope
repeat until `next_actions` is empty
```

`status` is free, has no side effects, and always tells you where the pipeline
is. Never infer progress from logs — ask.

## The envelope

Every command with `--json` prints exactly one object on stdout. Logs go to
stderr. Parse stdout; ignore stderr unless debugging.

```json
{
  "command": "run",
  "ok": true,
  "schema_version": "1.0",
  "data": { "venues": 100, "csv": "...", "kml": "..." },
  "warnings": ["4 venue(s) have no coordinates and are not pinned"],
  "next_actions": ["python -m untappd_maps pin --csv \"...\" --limit 3 --json"],
  "error": null
}
```

- `ok` — did the command achieve its purpose. Exit code matches (`0` / `1`).
- `next_actions` — **literal runnable commands**, best first. Not hints.
- `error` — present only on failure. Always carries `code` and `remedy`.
- `schema_version` — treat a change as breaking.

`--json` works before or after the subcommand.

## Error codes you should handle

| `error.code` | Meaning | What to do |
|---|---|---|
| `not_signed_in` | Playwright profile has no Google session | **Stop and ask the human.** Requires their password; you cannot do this. |
| `list_missing` | Target saved list does not exist | Ask the human to create it, or pick another `--list`. |
| `robots_disallow` | robots.txt forbids the paths | **Stop and ask.** Do not pass `--i-read-robots` on your own initiative. |
| `corpus_quality_gate` | <90% of venues parsed cleanly | Read `debug/*.html`, fix selectors in `parsers.py`, re-run. Nothing was written. |
| `selectors_stale` | `selfcheck` could not parse a known-good page | Same as above. This is the cheap early warning. |
| `csv_missing` | No input data | Run `run` first. |
| `interrupted` | Ctrl-C | Re-run the same command; progress is journalled. |
| `unexpected_error` | Unhandled | Re-run with `-v` for a traceback. |

## Rules for agents

1. **Never pass `--i-read-robots`.** That flag overrides a site's stated wishes.
   It is a human's call, not yours.
2. **Never run `pin` without an explicit human instruction.** It automates the
   Google Maps UI, which is against Google's ToS (see README). Default to the
   supported My Maps KML path.
3. **Trial before bulk.** First `pin` run should use `--limit 3`. Report the
   result before doing the rest.
4. **Do not lower the pacing.** `--min-gap` / `--max-gap` exist to keep the
   user's account safe. Raise them if throttled; do not lower them.
5. **A failed parse is a real finding.** The quality gate deliberately writes
   nothing when data looks degraded. Do not work around it by lowering
   `parse_strictness` — fix the selector and say what changed.
6. **Ask before anything irreversible** that touches the user's account.

## Idempotency

- `run` — safe to repeat. Cached for 12h; re-running costs almost no requests.
- `pin` — resumable and idempotent. `state/pinned.json` records every place;
  re-running skips successes and retries only failures.
- `status`, `doctor`, `selfcheck` — read-only.

## What needs a human

- The one-time `bootstrap` login (credentials).
- Creating the target saved list in Google Maps.
- Deciding whether to use `pin` at all.

## Anti-ban guardrails (do not weaken these)

`pin` writes to a live Google account. Four independent guardrails, layered so
defeating one still leaves the others:

| Guardrail | Behaviour |
|---|---|
| **Rate ledger** | Rolling 24h write budget, **persisted to disk**. Restarting does not reset it. Default 100/day, 60/run. |
| **Circuit breaker** | 3 consecutive failures → stop and start a cool-off. Repeated failure is when a script looks least human. |
| **Block detection** | Scans every page for CAPTCHA / "unusual traffic" / "not a robot" / forced sign-out. Any hit aborts instantly. Google serves these as HTTP 200, so text is the only signal. |
| **Cool-off** | 6h, persisted. Applied after any trip or detected block. |

Rules for agents:

1. **Never delete `state/rate_ledger.json`** to get a fresh allowance. The
   persistence is the entire point.
2. **Never raise `max_per_day` / `max_per_run`** on your own initiative.
3. **Never retry past a `Tripped` error.** It means stop, not try again.
4. If a run trims itself ("Trimming this run to N places"), that is correct
   behaviour — report it and resume later, do not work around it.
