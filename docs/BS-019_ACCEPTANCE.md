## BS-019 accepted locally after independent MEDIUM review

2026-09-11. AGY return 29c6317; frozen base 4efa2b2. Codex accepted the
1024 max_completion_tokens / low reasoning / 16384 serialized-byte guard
after direct corrections: duplicate JSON keys and non-standard constants are
rejected before debit/dispatch; parser recursion failures become generic
refusals; target refusal no longer echoes the supplied URL path. Four regression
cases cover these failures. Parser exhaustion is injected in the final test
because recursion behavior differs by Python parser implementation; this is not
a claim that all deeply nested valid JSON must be rejected.

Independent verification: 72 focused tests passed (two dependency deprecation
warnings), ruff over src/tests/scripts passed, changed-file formatting passed,
strict mypy passed across 77 files, uv dependency check passed for 71 packages,
and the intercepted SDK serialization probe passed. Evidence is under
services/agent/test-evidence/bs019/codex-*.

Verification used a fresh Python 3.12.10 environment installed from the unchanged
requirements-groq.lock and requirements-postgres.lock. The existing worker .venv
actually uses Python 3.14; the worker's historical 3.12 claim is not independently
substantiated. The new Codex results above are verified on 3.12.10.
The original 68 focused tests passed before fixes on that existing environment.
Codex did not rerun the full backend/Postgres suite: AGY's reported 520 passed /
51 skipped remains worker-reported, not independently accepted as a full run.
Postgres fixtures explicitly skip without BS_POSTGRES_TEST_URL. Existing local
Postgres acceptance remains historical; no production database was accessed.

Probe responses and usage fields are synthetic fixtures. Measured bytes do not
establish input-token counts, global quota admission, provider reasoning
accounting, server-side enforcement or output quality. Public assistant remains
disabled. All 29 historical provider probe allocations remain closed; this
review made zero provider calls and incurred zero spend. Package registry
downloads occurred for the isolated test environment. No cloud activation,
deployment, public push, credential discovery or workflow activation occurred.

Next: establish account/credit status and verify actual account entitlements
under the saved M3 sequence before separately authorizing any provider canary.
No new implementation worker is dispatched by this acceptance.
