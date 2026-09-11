## BS-021 accepted locally after direct Codex HIGH repair

2026-09-11. Original AGY 820bb99 is superseded by the direct Codex repair.
Read M3_CANARY_RECEIPT_CONTRACT.md for the corrected architecture.

One operator-issued authorization is consumed once, even after failure/recovery.
Exactly one concurrent dispatch caller succeeds; replay reads grant no execution.
Canonical JSON and exact payload comparison bind candidate, owner and allowance.
Database time rejects expiry equality. Owner mismatch and changed final evidence
fail closed. Explicit operator recovery covers crashes before/after dispatch,
preserving reservations and keeping measured usage unknown when unavailable.

The prior 1024-token reservation claim was incorrect: 1024 is the per-send
output cap. Receipt retains six requests and 6144 requested OUTPUT tokens.
This is not a total/input-token bound or public quota admission. Observed total
tokens stay NULL when unknown. No automatic refund, expiry recovery or retry.
Storage is a trusted operator boundary, not authentication or a grant issuer.
Future runner must independently verify operator authority and actual execution
source/plan hashes. The original BS-020 offline plan remains live_authorized=false.

Removed the duplicate in-memory store and exercised actual PostgreSQL instead.
Schema v3 is unpublished and repaired in place; v1/v2 remain byte-identical.
Existing production source is unchanged except the additive schema migration.
Four stale schema test expectations now use current/future version arithmetic.

Independent verification on Python 3.12.10 and disposable PostgreSQL 16.10:
- Full backend run: 620 passed, 4 failed, no skips (148.33s). All four failures
  were stale schema-version test expectations. Original failed log retained.
- After fixing them: 41 passed, no skips (29.97s), covering all 37 receipt
  checks and all four failures. No remaining known test failure; this is
  combined verification, not a claim of a second all-green full-suite run.
- Receipt proof includes six-way concurrent duplicate reservation, six-way
  distinct reservation, exactly one dispatch winner, uncertainty retention,
  recovery, exact replay/conflict, owner mismatch, invalid usage, v2-to-v3
  business-data preservation and repeat migration.
- Fresh-process read and actual PostgreSQL server restart preserve receipt
  state and refuse redispatch.
- Ruff over src/tests/scripts, changed-file formatting, strict mypy 82 files
  and uv dependency check 72 packages pass. Two existing deprecation warnings.

Evidence: services/agent/test-evidence/bs021/codex-*; original worker evidence
is historical and superseded. No live Groq call, cloud activation, credential
discovery, production migration, public push, deployment or spend. Hosted
assistant stays disabled. Public global admission and live release remain open.
NEXT_CODEX_MODE: ASTRA_LIGHT
REASON: Direct repair and verification complete; routine orchestration next.
