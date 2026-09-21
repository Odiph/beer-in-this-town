---
name: verify-change
description: Post-implementation verification for beer-in-this-town. Run after changing anything under beer_in_this_town/ and before claiming a fix works or opening a PR. Catches this repo's own repeated failure modes - an exception that never reaches its handler, a test at the wrong layer, a guardrail that cannot come down, a doc describing code that does not exist. Args - none or "quick" for the six code checks, "full" to add the four release checks.
user-invocable: true
disable-model-invocation: false
argument-hint: "[quick | full]"
---

# /verify-change: is the change wired to the thing it claims to change?

A fix that looks present but does not work is **worse than the original bug**,
because it closes the question. This repo has produced that outcome five
times, always the same way. Every check below is a bug that actually shipped
here — none of it is general hygiene.

`quick` (default) runs Steps 0–6. `full` adds Steps 7–10 and is what a PR
needs.

## When to use this

- After changing anything under `beer_in_this_town/`.
- Before saying a fix works. Especially then.
- Before opening or updating a PR — use `full`.
- When a review claims something is fixed and you want to know if it is.

## When NOT to use this

- Doc-only changes. Step 10 alone is enough.
- Mid-refactor, with the suite already red. Get Step 0 green first; every
  other step reads a green suite as its baseline.
- As a substitute for `/code-review`. This asks "does the change reach the
  code path"; that asks "is the change good".
- On the labelling artifact or anything under `.claude/` — no runtime here.

---

## Step 0: the mechanical gate

```bash
.venv/Scripts/python.exe -m pytest -q -m unit
.venv/Scripts/python.exe -m ruff check beer_in_this_town tests
```

Both green, and the suite still finishes in **seconds**. If it suddenly takes
a minute, something started walking `chrome-profile/` (≈1 GB).

This is the floor, not the answer. Every bug in the table below shipped with
this step green.

## Step 1: GATE — does the exception reach its handler?

**Stop here if this fails. Do not run Steps 2–10 on a change that fails this
one**: they will pass, and their passing is what produced two rounds of
believing a fix worked.

Five real bugs, all the same shape — a function raises deliberately, its
**caller** swallows it, and the fix looks right because the `raise` is in the
diff:

| Raised in | Swallowed by | Green suite when it shipped |
|---|---|---|
| `parse_search_page` | `collect_venue_refs` | ✅ |
| `_google` ("surface these, do not degrade") | `geocode_missing`'s `except Exception` | ✅ 130 |
| `TransportUnavailable` | `fetch_venues`'s per-venue `except Exception` | ✅ 195 |
| `AmbiguousList` | `_pin_once`'s retry loop, after the budget was charged | ✅ 229 |
| `RateLimitTripped` on robots.txt | `robots_disallows_scraping`'s bare `except` | ✅ 242 |

For every new or changed `raise`:

```bash
grep -rn "except Exception\|except RuntimeError" beer_in_this_town/
```

Walk each handler between the raise and where you intend it to land, and
answer out loud: **does the exception survive this one?** A new exception
subclassing `RuntimeError` is eaten by every single one of them.

Then check the envelope. An exception with no mapped `error.code` surfaces as
`unexpected_error`, whose remedy is "re-run" — the wrong advice for every row
in that table.

## Step 2: is the test at the layer that decides?

`test_repeated_transport_failures_stop_the_run` exercised `PoliteClient`,
passed, and never touched `fetch_venues` — the caller that swallowed the trip.
The behaviour under test was "the run stops", and no test ran the run.

Write the test where the outcome is observable, not where you typed the
change.

## Step 3: guardrails, in both directions

Every account-safety protection must survive the process ending **and** be
able to come down again. Both directions have bitten:

- **Not persisted** → `--min-gap 0` was accepted; the hourly ceiling refilled
  on restart; the breaker reset every run.
- **Persisted too well** → a stale breaker count tripped every subsequent run
  *before it attempted anything*, started a fresh 6h cool-off each time, and
  could never clear, because clearing needed a success that could not happen.
  A permanent lockout of `pin` and `notes`.

New persisted state also needs: fail **closed** on a corrupt read (the ledger
does; `ReadBudget` did not), an atomic write, and a line in
`inspect_guardrails` — or `status` reports `can_write: true` while the next
run trips instantly.

## Step 4: would an agent be walked into a write?

`next_actions` promises literal runnable commands and `AGENTS.md` says to run
the first and repeat. Anything in it is, in effect, authorised.

```bash
.venv/Scripts/python.exe -m beer_in_this_town status --json | \
  python -c "import json,sys; d=json.load(sys.stdin); print(d['next_actions'])"
```

No `pin`, no `notes`, no `bootstrap`, no `#` comments, and no bare `run`
(which uploads to My Maps unless given `--no-upload`). Check `fail()` too — it
promotes remedies into `next_actions`, which is how `pin` got back in after
being removed from `status`.

Anything a human must decide goes in `hints`, which nothing executes.

## Step 5: is a module-level `Path` a default argument?

```bash
grep -rn "path: Path = \|= LEDGER\|= BREAKER\|= READ_BUDGET" beer_in_this_town/
```

A default is evaluated **once at import**, so redirecting the module constant
afterwards never reaches it and the write lands in the real `state/`. Resolve
at call time:

```python
def __init__(self, path: Path | None = None) -> None:
    self.path = path if path is not None else LEDGER
```

This is also what makes the thing testable at all.

## Step 6: nothing escaped the sandbox

```bash
git status --short          # clean of state/, data/, logs/
```

The suite fails any test that creates, modifies or deletes a file in the real
tree. If that assertion fires, **do not silence it** — it has caught three
genuine escapes, one of them a write into `state/`, which records what has
already been written to the user's Google account.

---

# `full` — the four release checks

Run these before a PR. They are PR-time concerns, not every-edit concerns.

## Step 7: versions and the envelope contract

```bash
grep -n "^version" pyproject.toml
grep -n "SCHEMA_VERSION" beer_in_this_town/agent_io.py
```

- Added an **envelope field** or **error code**? `AGENTS.md` must state that
  additive changes do not bump `schema_version`, or an agent told "treat a
  change as breaking" has no way to read a new field safely.
- Changed **behaviour** a user depends on? `version` has sat at `0.1.0` since
  the second commit, through pacing floors, `next_actions` semantics and five
  new envelope fields. Decide deliberately; do not let it drift by default.
- Removed or renamed a field? That **is** breaking. Bump `SCHEMA_VERSION`.

## Step 8: security surface

```bash
grep -rn "dump_debug" beer_in_this_town/
git ls-files | grep -Ei "storage_state|chrome-profile|rate_ledger|pinned|noted"
```

- The second command must return **nothing**. `storage_state.json` holds live
  Google session cookies and this repo is public.
- New `dump_debug` call site? It writes **raw HTML**. Could that page be
  authenticated? `SECURITY.md` already warns about pasting dumps into issues;
  a dump of a signed-in page is the reason.
- New outbound request — a geocoder, Places, anything? Say in `SECURITY.md`
  what leaves the machine and to whom. Addresses sent to answer "is this
  someone's home" are a different disclosure from addresses sent to place a
  pin.

## Step 9: logs at the decision points

Guardrail decisions are what an incident is reconstructed from, and they only
run when something has already gone wrong. For each new one — a budget spend,
a breaker increment, a cool-off start, a block detection — check it logs at a
level the operator sees, remembering that `--json` sends logs to **stderr**.

The bar is: could you tell, from the log alone, why the run stopped and
whether it was right to?

## Step 10: GitHub completeness

```bash
git log origin/main..HEAD --format='%B' | grep -io "closes #[0-9]*" | sort -u
gh pr list --json number,headRefName -q '.[] | "#\(.number) \(.headRefName)"'
```

- `grep` the commit **bodies**, not `--oneline`. Subject-line-only grep
  returns nothing and reads as "no issues referenced".
- **Stacked PRs go stale against each other.** A commit on the top branch can
  close an issue whose own PR is still open further down. Check before merging.
- Is the PR description still true after the last four commits? It usually is
  not.
- CI green on the branch, not just locally.

---

## Why this exists instead of a generic verification loop

The ECC `verification-loop` skill checks build → typecheck → lint → tests →
security scan → diff review. Every bug found in two independent reviews of
this repo would have passed **all six**, because each one was valid Python
with a green suite. Those phases ask "did the toolchain pass". This asks
"does the change do what you said".

Run both. This one does not replace it — Step 0 *is* it, compressed.

## The guardrail, and why it is not optional

Step 1 is a gate, not a checklist item. Running the other nine steps on a
change that fails it produces nine green results and a false conclusion —
which is precisely how `TransportUnavailable` and the persisted breaker both
shipped as "fixed".

If Step 1 fails: fix it, then start again from Step 0.

## Gotchas

- **`LabelsUnusable`, `AmbiguousList`, `TransportUnavailable`, `Tripped` all
  subclass `RuntimeError`.** Every `except RuntimeError` in the tree eats them
  silently. Order handlers most-specific first.
- **The suite is ~3.5s.** If it becomes ~67s, the sandbox sweep is recursing
  `chrome-profile/`. Watch it shallowly.
- **`EnvelopeParser` reads `sys.argv` for `--json`,** so `main(argv=[...])`
  from a test or embedder prints human text rather than an envelope.
- **argparse floors reject before dispatch,** so a bad `--min-gap` never
  reaches `cmd_pin` — test it through `build_parser()`, not the command.
- **`git grep` on `--oneline` misses commit bodies.** Cost a false finding
  once already.
- **`state/rate_ledger.lock` may be stale on this machine** from a killed run.
  That is expected; it is broken automatically after 2h. Do not delete it.
- **Do not run `pin`, `notes`, `run` or `bootstrap` to verify anything.** They
  touch a live account or the network. Every check here is offline.

## The run record

Append one line to `docs/VERIFY_LOG.md` per run: date, `quick`/`full`, the
commit, which steps ran, and anything Step 1 caught. On the `devinstall`
precedent — a table of failure modes stays evidence-backed only while it is
still being fed. When a row stops earning its place, delete it.

## History

Written after two adversarial reviews found that five separate "fixes" had
never reached the code path they claimed to fix. The failure-mode table in
Step 1 is those five, in the order they were found.
