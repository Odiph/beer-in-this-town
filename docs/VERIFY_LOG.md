# /verify-change run record

One line per run. The failure-mode table in the skill stays evidence-backed
only while it is still being fed; when a row stops earning its place, delete
it from both.

| Date | Mode | Commit | Steps | Caught |
|---|---|---|---|---|
| 2026-09-20 | full | `aa2f386` | 0–10 | Step 7: `SCHEMA_VERSION` policy was nowhere stated while five envelope fields had been added; `version` still `0.1.0` after 14 behaviour-changing commits. Step 10: subject-line-only grep reported "no issues referenced" — a false finding; bodies close #10, #12, #13, #14, #15. Steps 0–6 clean. |
