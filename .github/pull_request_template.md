## What and why

## How this was verified
<!-- Tests added? Run against live sites, or offline only? -->

## Checklist
- [ ] `pytest -q -m unit` passes
- [ ] `ruff check untappd_maps tests` passes
- [ ] No silent fallbacks added to the parsing path (misses must raise)
- [ ] Rate limits and guardrails not weakened
- [ ] `SCHEMA_VERSION` bumped if the JSON envelope changed
