# Reviewing this branch

22 commits, written in one session, none of them seen yet. Reading them in the
order they were written is the expensive way: they are not in order of risk,
and several later ones correct earlier ones.

Ordered by **blast radius** instead — what it costs if the change is wrong.
Tier 1 is twenty minutes and covers everything that can touch a live Google
account. Tiers 2 and 3 can be skimmed.

Two independent adversarial reviews ran against this work. Both are summarised
where relevant, because *the second one found that five fixes from the first
round had never reached the code path they claimed to fix*. That is the single
most useful thing to know before reading any of it.

---

## Tier 1 — can write to your Google account. Read these properly.

### `83cf02a` fix: identify a saved list by its whole name, not by substring
The bug worth understanding. `pin` matched the target list by **substring** at
all three places it checks one — the row it clicks, the "Saved in…" line it
reads back to verify, and the pre-flight. With "Bars" and "London Bars" in one
account, a place could be saved into the wrong list **and verified as
correct**, because `"bars" in "london bars"`. The verification could not catch
it: it was looking at the list that had actually been clicked.

What to check: `saved_in_target`, `pick_list_row`, `list_exists_in` are pure
and tested. The judgement call is `_list_key` stripping a trailing place count
(`Bars (12)` ≡ `Bars`) and **nothing else**. If a real Maps picker row renders
as more than one line, that key will not match and every place raises
`AmbiguousList`. The second review flagged this as its highest [S] finding —
unverified against a live DOM.

### `8207fc2` fix: five fixes that did not reach the code path
**Read this one even if you read nothing else.** It is the correction commit,
and it is the clearest picture of how the session actually went.

Of note: persisting the circuit breaker (my change, one commit earlier) had
created a **permanent lockout** — the count loaded from disk, `is_tripped` was
checked before any place was attempted, and the only thing that cleared it sat
after a success that could therefore never happen. Every run tripped instantly
and started a fresh 6h cool-off. `status` said `can_write: true` throughout.

### `NEW` test: the write loop, exercised offline
The loop that charges the ledger, writes the journal and feeds the breaker had
never executed — only its pure helpers were tested. This runs it with a
stubbed browser.

It found a live bug immediately: the breaker was checked only at the *top* of
the loop, so a run failing its last three places tripped nothing and set no
cool-off. Fixed in the same commit.

### `374ff04` fix: notes gets the pre-flight pin already had
Signed-out Google Maps loads happily, so `notes` either journalled a hundred
rows `not-in-list` with `ok: true` and a wrong diagnosis, or tripped the block
detector into a 6h cool-off — for a condition `pin` reports cleanly.

### `adba86d` fix: anti-ban protections enforced rather than asked for
`--min-gap 0` was accepted. `--delay 0` was ignored *because zero is falsy*, so
it looked accepted. The hourly ceiling and breaker reset on restart. Now
floored and persisted. **Check the floors are the numbers you want** — they are
the shipped defaults, and raising is always allowed.

---

## Tier 2 — correctness of the data. Read the diffs.

| Commit | What to check |
|---|---|
| `9f21ffe` | `score_labels` rewrite. On a corpus where *every label was correct* it reported 100% of drops were wrong. The fix judges each drop against the claim the classifier made about that bucket. `0467c12` and `8207fc2` both correct it further — read them together or not at all. |
| `70182f9` | State scoped per city/list. `previous_run.json` was global, so a second city destroyed the first's history. Includes a one-shot rename-adoption of your existing `pinned.json` — worth understanding before your next `pin`. |
| `cd63be5` | A bad geocoding key degraded to a KML with four pins and `ok: true`. Now aborts. |
| `5b3b5cf` | `next_actions` no longer contains `pin`/`notes`/`bootstrap`. The contract said "repeat until empty" *and* "never run pin unasked", and `pin` was the only thing that emptied it. |
| `0467c12` | `fail()` was promoting those same commands back in through remedies. Same contradiction, different door. |

---

## Tier 3 — additive or documentation. Skim.

`c95dd12` GeoJSON/GPX export · `a7c3af1` the measurement harness · `58cef4e`
`selfcheck` probes search · `0821df3` `status` sees the guardrails · `0909c2a`
read-side transport/robots fixes · `d49a00e` no plausible-404 URLs

Docs: `042e5d5` corrects a **false claim I added earlier in the same session**
(the README said `run` touches no account; it uploads to My Maps by default).
`6b84712` the "scrape" wording pass. `aa2f386`/`a5143ad` the `verify-change`
skill. `59c46db` records what the private-space signal cannot separate.

Tests: `0cfcf37` the sandbox fixture, `33738e5` a test that was reading your
machine.

---

## Decisions waiting on you

1. **`run` uploads to My Maps by default.** Flagged three times, deliberately
   not changed — it is a behaviour change and it is your call. The README now
   states the default honestly instead of denying it.
2. **`version` is still `0.1.0`** after 22 commits of behaviour change.
3. **Merge order.** #17, #18 and #19 are stacked. #19's branch contains commits
   closing issues whose own PRs are further down, so merging top-first orphans
   them. Bottom-up, or collapse the stack.
4. **The pacing floors** (Tier 1, `adba86d`) are now enforced, not advisory.

## What is still unverified

- **Nothing has run against the live site.** Search was restored by an outside
  contributor (#5) and the end-to-end path has not been exercised since.
- **No `pin` or `notes` has run against a browser** with any of these changes.
  The offline loop test covers orchestration; it cannot tell you whether a real
  picker row matches `_list_key`.
- **The labelling pass has not happened**, so #6/#7/#8 remain unmeasured and
  `classify.py` is still unvalidated guesses — which is why it is not wired
  into `run`.
