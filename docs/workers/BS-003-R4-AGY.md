# BS-003-R4-AGY — Offline Cancellation, Cleanup Ownership & Smoke Enforcement

TASK_ID: BS-003-R4-AGY
WORKER: AGY (acting as senior backend developer)
MODEL: Gemini 3.8 Flash High (gemini-3.8-flash-high)
EFFORT: HIGH
STATUS: READY_FOR_REVIEW
BRANCH: worker/agy/BS-003-R4
BASE_COMMIT: fd18c28

## Preflight: Model & Worker Transfer

- **Worker takeover:** Per user instruction, AGY took over `BS-003-R4` offline corrections after Claude hit `BLOCKED_USAGE_CLAUDE`. Claude's partial work was inspected and retained rather than discarded.
- **Model / Effort verification:** Exact `gemini-3.8-flash-high` / `HIGH` verified from active runtime metadata and user task assignment.
- **Zero new inference:** ZERO new model calls, ZERO external socket connections, ZERO downloads, ZERO spend. All 16 cumulative attempts spent across the project remain preserved.

## Acceptance Criteria & Implementation Matrix

| Criterion | Requirement | Implementation & Proof | Status |
|---|---|---|---|
| **1. Approved Cancellation** | Signal AND cancel owned tasks on timeout/cancellation/shutdown; bounded drain; native stage-two extraction stops locally; check cancellation before accepting result. | `InferenceRegistry.drain` signals and hard-cancels tasks; `app.py` timeout and caller cancellation stop run and boundedly settle; `strands_interpreter.py` checks `cancel.is_set()` before accepting extraction result; tests in `test_cancellation.py` verify stage 1 stream close, stage 2 native extraction cancellation, and blocked reads that ignore threading Event. | PASS |
| **2. Preserve Cleanup Ownership** | Keep closeable client reference on close failure; finished task with failed cleanup does not unconditionally release slot; refuse subsequent inference while unresolved; retain ownership for shutdown cleanup; repeated cancellation handled. | `OwnedOllamaModel.aclose` only clears client reference after successful close; `StrandsOllamaInterpreter` retains unclosed models in `_unresolved`, marks `cleanup="close_failed"`, returns failure; `InferenceRegistry.settle()` prevents slot release if cleanup unresolved; HTTP route returns 503 `ASSISTANT_UNAVAILABLE` on subsequent requests without opening new client; `resolve_cleanup` retried at shutdown. Proven in `test_cancellation.py`. | PASS |
| **3. Smoke Proof Fail-Fast** | Fail immediately after ANY case failure (HTTP, assertions, diagnostics, per-case snapshot mutation); save failed attempt to ledger before returning; no next interpretation request; exact assertions (expected fields, normalized ISO date, model provenance, ordered missing_fields, missing pickup and explicit WALKER in relative case). | `assistant_smoke.py` takes snapshots before and after each case (`case_snapshot_unchanged`); validates exact draft, ordered missing_fields, and diagnostic header in `_check_case`; appends `_ledger_end` immediately and fails fast on ANY failure, writing evidence and exiting with code 1 without sending subsequent requests. Proven in `test_assistant_smoke.py` verifying exact POST count 1 on case 1 failure and exact POST count 2 on case 2 failure. | PASS |
| **4. Canonical Ledger & Ceiling Enforcement** | Enforce canonical ledger (`test-evidence/probe-ledger.jsonl`); fixed cumulative ceiling of 16; fail closed on missing/alternate history, incomplete attempts, stopped R3 state, or missing authorization; optional ledger and raised ceiling must not bypass controls. | Default `--ledger` set to `CANONICAL_LEDGER`; `main()` validates `AUTHORIZATION_ENV` (`BS_LIVE_PROOF_AUTHORIZATION`), rejects `ceiling > CUMULATIVE_CEILING (16)`, requires ledger, detects incomplete attempts (starts without ends), and blocks attempts when exhausted. Proven offline with temporary ledgers in `test_assistant_smoke.py`. | PASS |
| **5. Safe Terminal Diagnostic Correlation** | Correlate each attempt with safe terminal diagnostic (correlation_id, stage, outcome, reason, sends, tool_attempts, tool_successes, recovery_used, cleanup, elapsed_ms); expose via HTTP header `X-Assistant-Diagnostic` on 200 and CodedErrors; record in ledger and completion evidence; no public schema changes or normal log leakage. | `_Telemetry` captures exact metrics and reasons; `Interpretation.diagnostic` holds dictionary; `app.py` passes `X-Assistant-Diagnostic` HTTP header on 200 responses and in `_coded_error`; `Client` extracts header; recorded in ledger `end` entries and `assistant-live-proof.json`. Public JSON body schema remains byte-for-byte unchanged. Proven in unit tests. | PASS |
| **6. Correct Reports** | Three R3 calls ran despite failure of the first; 16 cumulative attempts spent; withdraw 'all code criteria met' until verified; schema wording as cause of omitted equipment is a hypothesis. | Dated append-only correction added to `docs/workers/BS-003-R3.md`. Ledger history documented accurately. Zero new inference performed in R4. | PASS |

## Tests & Verification

- **Full test suite:** 237 tests passed in 6.78s (zero failures).
- **Ruff format check:** 44 source files clean.
- **Ruff lint check:** 44 source files clean.
- **Mypy strict check:** 44 source files clean (`src`, `tests`, `scripts`).
- **Whitespace diff check:** `git diff --check` completely clean.

## Checkpoint & Handover

- **STATUS:** `READY_FOR_REVIEW` (offline R4 corrections complete; M2A live proof remains BLOCKED).
- **BRANCH:** `worker/agy/BS-003-R4`
- **CUMULATIVE ATTEMPTS SPENT:** 16 (5 pre-ledger + 8 in R2 + 3 in R3). Zero new inference in R4.
- **NEXT_SAFE_ACTION:** Independent review of BS-003-R4-AGY offline corrections.

## Correction (2026-09-08 — BS-003-R5-AGY Takeover Review)

An offline review of BS-003-R4-AGY identified four implementation gaps that required correction in BS-003-R5-AGY:
1. **Diagnostic capture timing & snapshot HTTP status:** In `assistant_smoke.py`, `last_diagnostic` was read from the HTTP client after taking the post-case snapshot, which inadvertently cleared the header state before validation. Snapshot comparison also failed to reject HTTP error responses from `/api/snapshot`.
2. **Ledger validation order:** Canonical path, existence, and JSONL integrity checks were performed after `Client(base_url)` was instantiated, violating the fail-closed offline safety guarantee.
3. **Shutdown deadline & cancellation-resistant tasks:** `InferenceRegistry.drain()` used separate sequential timeouts rather than a single unified deadline, and did not bound `aclose()` calls during `resolve_cleanup()`, risking stalls if a cleanup task swallowed `CancelledError`. In addition, route cancellation grace could theoretically push total response time beyond the 120.0s bound.
4. **Diagnostic counter and timestamp validation:** `_check_case` accepted Python `bool` values for integer counters (as `isinstance(True, int)` evaluates to `True`), did not enforce strict UTC ISO format on completed/due dates, did not prevent replayed `correlation_id` values across cases, and did not verify counters against provenance `inventory_tool_calls`.

All four items are resolved in `BS-003-R5-AGY`. Zero live inference was conducted in R4 or R5.
