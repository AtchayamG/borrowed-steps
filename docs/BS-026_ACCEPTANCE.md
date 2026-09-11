# BS-026 acceptance — durable inference admission storage

Status: **accepted locally after independent Codex review**  
Date: 2026-09-12  
Canonical branch: `integration/m1`  
Integration commits: `2107a6a` (worker) and `d5633a4` (bounded repair)

BS-026 adds the PostgreSQL inference-admission ledger required by the M3 admission decision. The accepted boundary is application-wide request control across hosted instances; Groq remains responsible for provider token quota enforcement. It preserves the fixed six-send reservation, rolling global/workspace caps, one active operation, durable 429 cooldown, fail-closed uncertainty, explicit operator recovery, and short transaction boundaries.

Codex found and repaired one contract defect in the worker return: `cleanup_completed` was accepted by `finish()` but was neither persisted nor included in terminal replay comparison. Migration V4 now stores it, confirmed terminal rows require it, and replay rejects changed cleanup evidence. The focused regression is in `test_finish_uncertain_stays_active_and_blocks_reservations`.

Independent verification on an isolated PostgreSQL 16.10 server:

- 25 focused admission and migration tests passed, including additive/idempotent migration, concurrency, lifecycle, caps, recovery, cooldown, engine constraints, and cleanup-evidence replay.
- Ruff and strict mypy passed for the changed source and test files.
- `git diff --check` passed.
- The canonical checkout reran the same 25 tests successfully.
- The worker’s broader suite was not independently rerun because its review virtualenv lacks the existing `openai` dependency; this is an environment collection failure, not an admission test failure.

No provider call, credential discovery, cloud mutation, public activation, deployment, or spend occurred. The hosted assistant remains disabled. BS-026 does not wire the ledger into HTTP or provider execution; that is the next bounded integration gate.

Next: implement the hosted integration boundary in a separate worker task, preserving BS-026’s API and keeping public assistant activation disabled until fresh end-to-end, security, quota, and deployment gates pass.
