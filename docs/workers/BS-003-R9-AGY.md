# BS-003-R9-AGY Worker Report

**TASK_ID**: BS-003-R9-AGY
**WORKER**: AGY, senior backend developer
**MODEL**: Gemini 3.8 Flash High (`gemini-3.8-flash-high`)
**EFFORT**: HIGH
**STATUS**: READY_FOR_REVIEW (offline correction completed; M2A live proof remains BLOCKED)
**WORKSPACE**: `00_PROGRAM_CONTROL/worktrees/BS-003-R9-agy`
**BRANCH**: `worker/agy/BS-003-R9`
**START_COMMIT**: `54bc9bf`
**PRIOR_EVIDENCE_COMMIT**: `54bc9bf`

---

## 1. Preflight and Model Attestation

- **Model & Effort**: Active session configured with `gemini-3.8-flash-high` and HIGH effort.
- **Execution Mode**: Senior backend developer, manual implementation and verification only, zero subagents invoked.
- **Worktree**: Isolated worktree `BS-003-R9-agy` on branch `worker/agy/BS-003-R9`, clean start at `54bc9bf`.
- **Environment**: Dedicated `.venv` with Python 3.11.15; dependencies isolated from other worktrees; editable package pointing to `services/agent/src`.
- **Authorization & Bounds**: ZERO new inference or live endpoint calls; no models started, no server launched, no live authorization token set, no replacement probe executed.

---

## 2. Implemented Scope

### 2.1 Evidence Recovery and Causal Status (Criterion 1 & 2)
- Conducted bounded search of existing R8 session logs and background task records (`task-3087.log`).
- Identified root cause of missing grounding logs: in `services/agent/src/borrowed_steps/main.py`, `_configure_logging()` requires `$env:BS_LOG_LEVEL` to attach a handler for logger `borrowed_steps`. Because `BS_LOG_LEVEL` was unset when the uvicorn server started during R8, grounding `INFO` lines were dropped by Python's default root logger threshold.
- Recorded authentic causal status of R8 Attempt 12 as **`UNKNOWN`** in `services/agent/test-evidence/r9-evidence-review.md`.
- Clarified that the observed live failure was a **post-grounding** HTTP draft containing `null` for `equipment_kind` and `due_at`. Without grounding logs, it cannot be determined whether `llama3.2:3b` omitted the candidates (`absent_candidate`) or emitted candidates that were rejected by server-side grounding (`evidence_not_in_source`, `kind_mismatch`, etc.). Prior assertions that the model itself emitted `null` were unverified.
- Corrected accounting: Exactly 1 endpoint invocation (`POST /api/intake/interpret`), 3 model sends, 1 tool success. Cumulative attempts charged: 17 of 22 ceiling. 5 unspent slots are not permission to run inference. Allocation remains permanently closed.

### 2.2 Smoke Test Harness Verdict Honesty (`scripts/assistant_smoke.py`, Criterion 3)
- Defined module constants:
  - `REQUIRED_CASES = frozenset(str(i) for i in range(1, 7))`
  - `EXIT_PASS = 0`
  - `EXIT_FAIL = 1`
  - `EXIT_PARTIAL = 2`
- Added pre-network selection validation at step 0 of `main()`: rejects empty selection or unknown case selections with exit code 1 (`EXIT_FAIL`) before any `Client` or network connection is created.
- Updated `_write_evidence`:
  - `verdict = "PASS"` is strictly reserved for runs where all 6 required cases completed with passing verdicts, no failures, and `stopped_at is None`.
  - A proper subset that completes without failures produces `verdict = "PARTIAL"`.
  - Any failure or prerequisite stop produces `verdict = "BLOCKED"`.
- Updated `main()` exit semantics:
  - All 6 cases passing: prints `"assistant live proof passed"`, returns `EXIT_PASS` (0).
  - Proper subset passing: prints `"assistant partial run (N of 6 cases completed)"`, returns `EXIT_PARTIAL` (2); never prints `"assistant live proof passed"`.
  - Failures and prerequisite stops: print failure diagnostic, return `EXIT_FAIL` (1).

### 2.3 Cumulative Budget Test Repair (`tests/test_extraction_alignment.py`, Criterion 4)
- Fixed `test_smoke_r8_cumulative_ceiling_22_and_budget_checks`: constructed Attempt 12 with a structurally valid successful end entry (`http_status=200`, `verdict="PASS"`, `case_failures=[]`).
- Verified that `_check_r8_history` passes Attempt 12 and execution genuinely reaches the budget check branch (`charged + wanted = 17 + 6 = 23 > 22`).
- Asserted `result == 1`, `len(fake_client.calls) == 0`, and verified captured output contains `"taking the total to 23 past the ceiling of 22"` and `"The allowance is never restarted"`.

### 2.4 Focused Offline Verification (Criterion 5)
- Added 5 new tests to `tests/test_extraction_alignment.py`:
  1. `test_smoke_rejects_empty_case_selection_before_client_creation`: asserts exit 1, zero client calls, and `"no cases selected"`.
  2. `test_smoke_rejects_unknown_case_selection_before_client_creation`: asserts exit 1, zero client calls, and `"unknown case(s) selected"`.
  3. `test_smoke_proper_subset_finishes_partial_nonzero`: runs 2 cases, asserts exit 2, evidence `verdict="PARTIAL"`, zero failures, and verifies `"assistant live proof passed"` is absent from stdout.
  4. `test_smoke_real_canonical_r8_ledger_refusal_sentinel_client`: uses the real read-only canonical ledger and patches `Client` with a sentinel that raises `AssertionError` if instantiated. Proves permanent closure on failure without constructing a network client.
  5. `test_illustrative_grounding_replay_omitted_vs_rejected_candidates`: illustrative replay explicitly demonstrating how both candidate omission (`ABSENT_CANDIDATE`) and grounding rejection (`EVIDENCE_NOT_IN_SOURCE`) produce post-grounding `null`.
- Froze dynamic timestamps in test fixtures (`_cases()`) to prevent date drift across minute boundaries.
- Updated `test_smoke_full_success_offline_fake` in `test_assistant_smoke.py` to cover all 6 cases with frozen timestamps and assert `result == EXIT_PASS` (0).
- Updated single-case passing tests in `test_assistant_smoke.py` and `test_smoke_prerequisite.py` to assert `result == EXIT_PARTIAL` (2) and evidence `verdict == "PARTIAL"`.

---

## 3. Offline Verification Results

All offline checks run from `services/agent` in the isolated venv:
1. `pytest`: **295 passed**, 2 pre-existing deprecation warnings in 12.33s.
2. `ruff format --check src tests scripts`: **49 files already formatted** (clean).
3. `ruff check src tests scripts`: **All checks passed** (clean).
4. `mypy src tests scripts`: **Success: no issues found in 49 source files** (strict mode, clean).
5. `git diff --check`: **Clean** (no whitespace or conflict marker errors).
6. Production `src/**` diff: **Zero changes** (immutable).
7. Historical evidence / ledger diff: **Zero changes** (`probe-ledger.jsonl` and `assistant-live-proof-r8.json` untouched).

---

## 4. Known Limitations and Status

- **Historic R8 Causal Attribution**: Status is **`UNKNOWN`** due to missing grounding logs in R8.
- **M2A Integration Proof**: M2A live proof remains **`BLOCKED`**.
- **Cumulative Budget**: 17 attempts spent of 22 ceiling; R8 allocation permanently closed.

---

## 5. Next Safe Action

Codex reviews this offline correction. No live inference, no replacement probe, no model swap, no branch merge or push.
