# BS-003-R8-AGY Worker Report

TASK_ID: BS-003-R8-AGY
WORKER: AGY, senior backend developer
MODEL: Gemini 3.8 Flash High (gemini-3.8-flash-high)
EFFORT: HIGH
STATUS: BLOCKED
BRANCH: worker/agy/BS-003-R8
START_COMMIT: cbaeb9c
IMPLEMENTATION_COMMIT: d52db5b29f596ade116cfa36a47036e281e557d0

## Preflight and Model Attestation

- Model: `gemini-3.8-flash-high`, observed from active session configuration and environment metadata.
- Effort: HIGH, observed from session dispatch parameters.
- Mode: Senior backend developer, manual implementation and verification only, no subagents invoked.
- Worktree: `00_PROGRAM_CONTROL/worktrees/BS-003-R8-agy`, isolated `.venv` with editable install pointing to `src`.

## Implemented Changes

### 1. Extraction Instruction Alignment (`strands_interpreter.py`)
Per approved decision `docs/M2A_EXTRACTION_ALIGNMENT.md`:
- Removed the system prompt contradiction asserting that all values must equal verbatim evidence and occur character-for-character in source.
- Defined `equipment_kind` as the ONLY enum conversion (`WHEELCHAIR`, `WALKER`, `CRUTCHES`), where `equipment_kind_evidence` is the original source phrase.
- Added explicit positive enum mapping rules and synonym rules (e.g. `walking frame` -> `WALKER`).
- Clarified that repeated synonyms for the same kind count as one kind, whereas zero or multiple distinct kinds result in `equipment_kind` and `equipment_kind_evidence` being `null`.
- Clarified that `due_at` requires an explicit, complete timezone-aware timestamp (with `Z` or an explicit offset) copied verbatim with seconds, while relative/partial dates must be `null`. Normalization is strictly reserved for server-side grounding.
- Kept all 8 fields in `_Extraction` required and nullable. Updated field descriptions for `equipment_kind`, `equipment_kind_evidence`, and `due_at`.

### 2. Smoke Test Harness Updates (`scripts/assistant_smoke.py`)
- Configured task constants: `TASK_ID = "BS-003-R8-AGY"`, `REQUIRED_AUTHORIZATION = "BS-003-R8-AGY"`, `CUMULATIVE_CEILING = 22`, `HISTORICAL_SPENT = 16`, `EVIDENCE_FILENAME = "assistant-live-proof-r8.json"`.
- Added exact authorization enforcement: `BS_LIVE_PROOF_AUTHORIZATION` must exactly match `"BS-003-R8-AGY"`; missing or mismatching tokens fail closed with exit code 1.
- Enforced permanent closure on any failed, aborted, or incomplete R8 attempt (`_check_r8_history`), while preserving historic attempts 1–11 without rejection.
- Updated case definitions to include all six controlled sequential cases per `M2A_EXTRACTION_ALIGNMENT.md`, generating dynamic timestamps once per run and reusing them across budget calculations and test executions.
- Added implementation commit tracking via `_get_implementation_commit` (`git rev-parse HEAD` or `--implementation-commit`).
- Preserved prerequisite pre-case snapshot check, start-before-send accounting, diagnostic verification, strict field equality assertions, and fail-fast termination on the first failed case.
- Isolated R8 evidence writing to `assistant-live-proof-r8.json`, leaving historical `assistant-live-proof.json` immutable.

### 3. Focused Tests
- Updated `tests/test_assistant_smoke.py` and `tests/test_smoke_prerequisite.py` to use `assistant_smoke.REQUIRED_AUTHORIZATION` and `assistant_smoke.EVIDENCE_FILENAME`.
- Created `tests/test_extraction_alignment.py` (13 tests) verifying:
  - System prompt and schema alignment.
  - Grounding behavior with uppercase candidate / lowercase evidence.
  - Grounding behavior with synonyms (`walking frame` -> `WALKER`).
  - Grounding behavior with distinct-kind ambiguity (`AMBIGUOUS_KINDS`).
  - Grounding behavior with omitted model candidate (`ABSENT_CANDIDATE`).
  - Grounding behavior with offset timestamps (normalized to UTC).
  - Exact authorization token enforcement.
  - Permanent closure on prior failed or incomplete R8 attempts.
  - Preservation of historic attempts 1–11 without triggering R8 closure.
  - Cumulative ceiling 22 and budget checks.
  - Full six-case fake HTTP acceptance run.
  - Fail-fast abortion on first case failure.

## Offline Verification Results

All offline checks pass cleanly:
- `pytest`: 290 passed in 9.03s (277 baseline invariants + 13 new focused tests).
- `ruff format --check src tests scripts`: 50 files already formatted.
- `ruff check src tests scripts`: All checks passed.
- `mypy src tests scripts`: Success: no issues found in 49 source files (strict mode).
- `git diff --check`: Clean.

## Live Verification Proof

- Preflight: Local Ollama on `127.0.0.1:11434` confirmed active with `llama3.2:3b`.
- Server started: `BS_ASSISTANT_ENABLED=true`, `BS_DB_PATH=$TEMP/bs_r8_test.db`, port 8218. Health check returned `agent_mode: strands_ollama`.
- Implementation freeze commit: `d52db5b29f596ade116cfa36a47036e281e557d0`.
- Execution:
  ```powershell
  $env:BS_LIVE_PROOF_AUTHORIZATION = "BS-003-R8-AGY"
  .\.venv\Scripts\python.exe scripts/assistant_smoke.py http://127.0.0.1:8218 --label "r8-pass-1"
  ```
- Result: **BLOCKED at Case 1 (Attempt 12)**.
  - Response: HTTP 200, 5.97s.
  - Diagnostic: `stage="extraction"`, `outcome="success"`, `tool_attempts=1`, `tool_successes=1`, `sends=3`, `recovery_used=false`, `cleanup="closed"`.
  - Tool execution: Real `read_inventory` tool call executed successfully (`inventory_tool_calls: 1`).
  - Draft returned:
    ```json
    {
      "borrower_label": "Meena R",
      "equipment_kind": null,
      "pickup_location": "the Velachery equipment room",
      "due_at": null
    }
    ```
  - Failures:
    - `1-explicit-fields: equipment_kind=None, expected one of ['WHEELCHAIR']`
    - `1-explicit-fields: due_at=None, expected one of ['2026-09-15T12:38:37Z']`
    - `1-explicit-fields: missing_fields=['equipment_kind', 'due_at'], expected []`
  - Fail-fast: The smoke script immediately stopped without sending Cases 2–6.
  - Evidence: Written to `services/agent/test-evidence/assistant-live-proof-r8.json` with `verdict: "BLOCKED"`.
  - Ledger: Appended start and end records for attempt 12 with `verdict: "FAIL"`.
  - Budget accounting: 1 new attempt charged (cumulative total: 17 of 22). 5 attempts remain unspent in ceiling.
  - Permanent closure: Allocation is permanently closed; rerun verification confirmed immediate exit 1 refusing further runs.
  - Snapshot: Business snapshot verified byte-identical before and after (read-only invariant preserved).

## Criterion Evaluation

1. **Instruction Contradiction Removal**: PASS (Offline). Removed system prompt contradiction; aligned schema descriptions and positive enum mappings.
2. **Runtime Invariants & Offline Tests**: PASS (Offline). Retained all 277 existing test invariants; added 13 focused tests covering schema, grounding, authorization, and harness controls.
3. **Smoke Script Controls**: PASS (Offline & Verified). Exact auth check, cap 22, R8 permanent closure on failure, separate R8 evidence, and implementation commit tracking verified.
4. **Live Verification Proof**: BLOCKED. Model output still returned null for `equipment_kind` and `due_at` on Case 1. Fail-fast stopped immediately after 1 call; no retries or prompt tuning performed.
5. **Observation vs Hypothesis**: PASS. Documented that removing prompt contradiction did not resolve candidate omissions by `llama3.2:3b`. Recorded safe reason codes; zero personal data.

## Known Issues & Findings

- The local `llama3.2:3b` model under Ollama structured decoding continues to output `null` for `equipment_kind` and `due_at` during stage-two extraction on Case 1, despite unambiguous prompt instructions and positive enum definitions.
- Removing the instruction contradiction was necessary but insufficient for model compliance.

## Architecture Deviations

None. No model swap, no prompt tuning iterations, no deterministic fallbacks, no relaxing of grounding rules.

## Review Request

Codex / Human Reviewer:
- Inspect `services/agent/test-evidence/assistant-live-proof-r8.json` and `services/agent/test-evidence/probe-ledger.jsonl`.
- Confirm that the R8 allocation stopped cleanly at 1 send with cumulative 17 attempts charged.
- Direct next program-level action regarding `llama3.2:3b` capability limitations.

## Next Safe Action

Await program direction / review. Do not run further live probes.
