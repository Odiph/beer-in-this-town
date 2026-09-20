---
name: verify-change
description: Post-implementation check for beer-in-this-town. Run after changing anything in beer_in_this_town/ and before claiming a fix works. Catches this repo's specific, repeated failure modes — an exception that never reaches its handler, a test at the wrong layer, a guardrail that resets, a doc that describes code which does not exist. Use when finishing a change, preparing a PR, or when asked whether a fix actually works.
---

# verify-change

A fix that looks present but does not work is **worse than the original bug**,
because it closes the question. This repo has produced that outcome five times,
always the same way, so the checks below are not generic hygiene — each one is
a bug that shipped here.

Run this before saying a change is done.

## 0. The mechanical gate

```bash
.venv/Scripts/python.exe -m pytest -q -m unit
.venv/Scripts/python.exe -m ruff check beer_in_this_town tests
```

Both green, and the suite still runs in **seconds**. If it suddenly takes a
minute, something started walking `chrome-profile/` (≈1 GB).

## 1. Does the exception actually reach the handler? ← the big one

Five real bugs, all this shape: a function raises deliberately, its **caller**
swallows it, and the fix appears to work because the raise is right there in
the diff.

| Raised in | Swallowed by |
|---|---|
| `parse_search_page` | `collect_venue_refs` |
| `_google` ("surface these, do not degrade") | `geocode_missing`'s `except Exception` |
| `TransportUnavailable` | `fetch_venues`'s per-venue `except Exception` |
| `AmbiguousList` | `_pin_once`'s retry loop, after the budget was charged |
| `RateLimitTripped` on robots.txt | `robots_disallows_scraping`'s bare `except` |

For every new or changed `raise`, trace it to the code that handles it:

```bash
grep -rn "except Exception\|except RuntimeError" beer_in_this_town/
```

Then ask, for each one between the raise and the intended handler: **does the
exception survive this?** A new exception inheriting `RuntimeError` is caught
by every one of them. If the answer is "it stops here", the fix does nothing.

Then check the envelope: an exception with no `error.code` surfaces as
`unexpected_error`, whose remedy is "re-run" — the wrong advice for every
condition in the table above.

## 2. Is the test at the layer that decides?

`test_repeated_transport_failures_stop_the_run` exercised `PoliteClient` and
passed, while `fetch_venues` — the caller that swallowed the trip — was never
touched. The behaviour under test was "the run stops", and no test ran the run.

Write the test at the layer where the outcome is observable, not where the
change was typed.

## 3. Does a guardrail still guard after a restart?

Every account-safety protection must survive the process ending, and must not
outlive its purpose. Both directions have bitten:

- **Not persisted** → `--min-gap 0` was accepted, the hourly ceiling refilled
  on restart, the circuit breaker reset every run.
- **Persisted too well** → a stale breaker count tripped every subsequent run
  *before it attempted anything*, started a fresh 6h cool-off each time, and
  could never be cleared because clearing required a success that could not
  happen. That was a permanent lockout of `pin` and `notes`.

So ask both: does it survive a restart, **and** can it ever come down again?

```bash
grep -rn "_write\|json.dumps" beer_in_this_town/guardrails.py beer_in_this_town/http_client.py
```

New persisted state also needs: fail **closed** on a corrupt read (the ledger
does; `ReadBudget` did not), an atomic write, and a line in
`inspect_guardrails` — or `status` reports `can_write: true` while the next
run trips instantly.

## 4. Would an agent be walked into a write?

`next_actions` promises literal runnable commands and `AGENTS.md` says to run
the first one and repeat. So anything in it is, in effect, authorised.

```bash
.venv/Scripts/python.exe -m beer_in_this_town status --json | \
  python -c "import json,sys; d=json.load(sys.stdin); print(d['next_actions'])"
```

It must contain **no** `pin`, `notes` or `bootstrap`, no `#` comments, and no
bare `run` (which uploads to My Maps unless given `--no-upload`). Check
`fail()` too: it promotes remedies into `next_actions`, which is how `pin` got
back in after being removed from `status`.

Anything a human must decide goes in `hints`, which nothing executes.

## 5. Is a module-level `Path` being used as a default argument?

```bash
grep -rn "path: Path = \|= LEDGER\|= BREAKER\|= READ_BUDGET" beer_in_this_town/
```

A default is evaluated **once at import**, so redirecting the module constant
afterwards never reaches it — the write lands in the real `state/`. Resolve at
call time:

```python
def __init__(self, path: Path | None = None) -> None:
    self.path = path if path is not None else LEDGER
```

This is also what makes the thing testable at all.

## 6. Do the docs still describe the code?

Wrong docs have shipped here repeatedly: `AGENTS.md` documented
`list_ambiguous` while it was unreachable; the CHANGELOG claimed a dead
network stopped the run while it still slept for 23 hours; the README said
`run` touches no account while it uploads by default.

If the change added or altered an **error code**, an **envelope field**, a
**flag**, or a **guardrail**, then:

- `AGENTS.md` — error table, and the rules if the change touches ToS-sensitive
  behaviour
- `README.md` — the commands table and the pacing paragraph
- `CHANGELOG.md` — under `[Unreleased]`, saying what was wrong and why it
  mattered, not just what changed

Then re-read the claim you just wrote and ask whether you **verified** it or
merely implemented it. Those are different, and this file exists because the
difference kept mattering.

## 7. Nothing escaped the sandbox

The suite fails any test that creates, modifies or deletes a file in the real
tree. If that assertion fires, do not silence it — it has caught three genuine
escapes, including a write into `state/`, which records what has already been
written to the user's Google account.

```bash
git status --short          # must be clean of state/, data/, logs/ changes
```

## When you are done

Say which checks you **ran**, and which claims are verified versus
implemented. "Tests pass" is check 0 of 7.
