# Agent contract

This tool is built to be driven by a coding agent. This file is the contract.
If you are an agent, read this and nothing else is required.

## The loop

```
python -m beer_in_this_town status --json      →  read `next_actions`
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
  "next_actions": ["python -m beer_in_this_town label --csv \"...\" --json"],
  "hints": ["To build a Google Maps saved list, a human can run: pin --csv ..."],
  "error": null
}
```

- `ok` — did the command achieve its purpose. Exit code matches (`0` / `1`).
- `next_actions` — **literal runnable commands**, best first. Not hints.
  Only read-only, safe commands appear here; it never contains `pin`, `notes`
  or `bootstrap`, and never contains a `#` comment. An **empty list is the end
  of the loop**, not an error.
- `hints` — prose for a human: what only a person can decide to do next.
  Nothing executes this, and an agent must not treat it as a next action.
- `error` — present only on failure. Always carries `code` and `remedy`.
- `schema_version` — treat a change as breaking.

`--json` works before or after the subcommand.

## Error codes you should handle

| `error.code` | Meaning | What to do |
|---|---|---|
| `not_signed_in` | Playwright profile has no Google session | **Stop and ask the human.** Requires their password; you cannot do this. |
| `already_running` | Another process holds the write budget | Wait for it, then re-run the same command. Nothing has to elapse — this is not a cool-off, and `status` will look clear because it does not know about the lock. Do not delete the lock file. |
| `list_missing` | Target saved list does not exist | Ask the human to create it, or pick another `--list`. |
| `list_ambiguous` | `--list` does not name exactly one list — no list matches it exactly, or several do | **Stop and ask.** Nothing was saved. Do not retry with a nearby name; that is how a place lands in the wrong list. |
| `robots_disallow` | robots.txt forbids the paths | **Stop and ask.** Do not pass `--i-read-robots` on your own initiative. |
| `corpus_quality_gate` | <90% of venues parsed cleanly | Read `debug/*.html`, fix selectors in `parsers.py`, re-run. Nothing was written. |
| `selectors_stale` | `selfcheck` could not parse a known-good venue page or the search page, or neither search path parsed during `run` | Same as above. `selfcheck` is the cheap early warning and covers both surfaces; `data.search` says which shape the search page had. |
| `search_login_required` | Untappd's sign-in wall cut the search short (anonymous search stops at 5) | Ask the human to sign in to Untappd in the browser profile. `bootstrap` only detects a Google session, so it cannot confirm this one. |
| `geocoder_unavailable` | The geocoder is rejected, out of quota, or unreachable | Not per-venue — check the key and billing, or unset it for Nominatim. Nothing was written. |
| `csv_missing` | No input data | Run `run` first. |
| `labels_incomplete` | A sampled bucket came back with no labels | Ask the human to label a few rows in every bucket. The rare ones are the point. |
| `labels_unusable` | An answer is outside the accepted vocabulary, or the sheet lost its `_stratum`/`_stratum_size` columns | The message names the row and cell. Answers are `y` / `n` / `?`; a blank means unanswered. |
| `notes_failed` | The notes pass failed | Re-run; progress resumes. |
| `interrupted` | Ctrl-C | Re-run the same command; progress is journalled. |
| `unexpected_error` | Unhandled | Re-run with `-v` for a traceback. |

## Rules for agents

1. **Never pass `--i-read-robots`.** That flag overrides a site's stated wishes.
   It is a human's call, not yours.
2. **Never run `pin` without an explicit human instruction.** It automates the
   Google Maps UI, which is against Google's ToS (see README). Default to the
   supported My Maps KML path.

   This used to contradict the loop above, and the loop won: `pin` was listed
   in `next_actions` and was the only entry that ever emptied it, so following
   the contract led an agent into the write. It is now in `hints` instead —
   where nothing is instructed to run it.
3. **Trial before bulk.** First `pin` run should use `--limit 3`. Report the
   result before doing the rest.
4. **Do not lower the pacing.** `--min-gap` / `--max-gap` exist to keep the
   user's account safe. Raise them if throttled; do not lower them.
5. **A failed parse is a real finding.** The quality gate deliberately writes
   nothing when data looks degraded. Do not work around it by lowering
   `parse_strictness` — fix the selector and say what changed.
6. **Ask before anything irreversible** that touches the user's account.

## The `notes` command

Writes Untappd stats into the note on each saved place. Same rules as `pin`: it
writes to the account, so never run it without an explicit human request, and
trial it with `--limit` first.

It only annotates places already in the target list; anything else is recorded
as `not-in-list` and left alone. A note that already matches is never rewritten.

## The `label` and `score` commands

`classify.py` holds candidate heuristics for venue kind, closed venues and
private spaces. **None of them are wired into `run`**, and none should be until
they have been measured. `label` emits a stratified sample for a human to
judge; `score` reports the error rates by direction.

Both are read-only, offline and touch no account, so an agent may run them
freely. What an agent must **not** do is fill in the labels: the whole point is
a human judgement the classifier can be checked against, and a model labelling
its own classifier's output measures nothing.

Sampling takes a fixed quota per bucket, rare ones included, and `score`
divides that back out. Two consequences worth knowing:

- Labelling only the easy rows breaks the weighting. `score` refuses a bucket
  with no labels and warns about thin ones, but cannot detect cherry-picking.
- Every rate's denominator is the rows that answered **that** question. A blank
  is not an answer and `?` is not a verdict, so a rate can come back `null`
  meaning *unknown* — which is not the same as zero errors, and must not be
  reported as a clean result.
- A CSV with no `category` column makes every kind prediction `unsettled`, so
  the kind measurement says nothing. `label` warns when it sees this.

## Idempotency

- `run` — safe to repeat. Cached for 12h; re-running costs almost no requests.
- `pin` — resumable and idempotent. `state/pinned_<list>.json` records every place;
  re-running skips successes and retries failures. Places recorded `not-found`
  or `ambiguous` are retried too, since both can be transient.
- `notes` — resumable via `state/noted_<list>.json`; a matching note is skipped.
- Journals, baselines and `status` are scoped to the city or list they
  describe. `status` reports the last `run`'s query and list under
  `data.last_run`; its `next_actions` are built from those, not from
  defaults. If `recorded` is false no run has happened yet and the
  defaults are in play — check before running a write command.
- `status`, `doctor`, `selfcheck` — read-only.

## What needs a human

- The one-time `bootstrap` login (credentials).
- Creating the target saved list in Google Maps.
- Deciding whether to use `pin` at all.

## Account-safety guardrails (do not weaken these)

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
