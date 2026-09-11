# Offline Groq Canary Preparation Harness

This document describes the offline-only Groq canary preparation harness implemented
in `services/agent/scripts/groq_canary.py` under `docs/M3_CANARY_PREPARATION_CONTRACT.md`
(task BS-020).

This harness is strictly offline: it authorizes zero live provider calls, requires no
credentials or accounts, and has no live CLI or network egress modes ($0 spend).

---

## 1. Objectives & Scope

1. **Immutable Candidate Plan**: Generate an immutable, deterministic canary execution
   plan (`canary_plan.json`) hashed over the candidate fixture and canonical source/lock
   inputs (`groq_model.py`, `strands_interpreter.py`, `requirements-groq.lock`,
   `requirements.lock`). Any change to runtime code or lockfiles invalidates the candidate plan.
2. **Two-Stage Execution Flow**:
   - **Stage 1**: Real Strands `Agent` selecting and executing the `read_inventory` tool
     against actual seed inventory counts (max 2 tool attempts, at most 1 recovery prompt
     if omitted, bounded turns).
   - **Stage 2**: Exactly one `structured_output` call with source-only extraction. Requires
     prior successful tool execution before extraction. Asserts exact source quotation against
     the candidate fixture.
   - **Single Shared `GroqModel`**: Both stages share a single `GroqModel` instance, strictly
     sharing the cumulative 6-send ceiling, 110s operation deadline, and 16,384-byte wire limit.
3. **Deterministic Evidence Capture**: Generate a sanitized counts-only evidence artifact
   (`canary_evidence.json`) labeled `provenance: "offline_fixture"`. No secrets, keys,
   user prompts, model reasoning, or private paths are logged.
4. **Guaranteed Cleanup**: Ensure `await model.aclose()` is executed in an owned `finally:`
   block across all success and failure branches.

---

## 2. Architecture & Components

### A. Candidate Fixture

The synthetic candidate input is:
```text
Priya S wants crutches from the Adyar centre, back by 2026-10-01T08:00:00Z.
```
This fixture exercises equipment lookup (`crutches`), location filtering (`Adyar centre`),
borrower identification (`Priya S`), and ISO 8601 timestamp extraction (`2026-10-01T08:00:00Z`).

### B. Two-Stage Execution

1. **Stage 1: Tool Selection & Execution**
   - Uses real Strands `Agent` (`from strands import Agent, tool`).
   - The tool `read_inventory` queries `SeedInventoryReader` for current stock counts.
   - Enforces a hard ceiling of 2 tool attempts (`max_tool_attempts = 2`).
   - If the model omits the tool on turn 1 and sufficient send budget remains, exactly one
     recovery prompt is dispatched (`"Please check inventory using read_inventory before proceeding."`).
   - If the tool is still omitted or if a 3rd tool call is attempted, the harness fails closed
     with `CanaryToolNotExecutedError` or `CanaryToolLimitExceededError`.
2. **Stage 2: Structured Output Extraction**
   - Requires stage 1 to have recorded at least one successful tool call.
   - Calls `model.structured_output(_Extraction, ...)` with source-only prompt history.
   - Strictly enforces that extracted fields (`borrower_label`, `equipment_kind`,
     `pickup_location`, `due_at`) match exact verbatim substrings of the candidate fixture.
   - If any extracted field is not present verbatim in the fixture, the harness fails closed
     with `CanaryGroundingAssertionError`.
   - Never writes or publishes business records or database drafts.

### C. Deterministic Plan Hashing

The plan hash is a SHA-256 digest computed over:
1. Canonical source and lockfile byte contents:
   - `services/agent/src/borrowed_steps/infrastructure/groq_model.py`
   - `services/agent/src/borrowed_steps/infrastructure/strands_interpreter.py`
   - `services/agent/requirements-groq.lock`
   - `services/agent/requirements.lock`
2. UTF-8 byte representation of the candidate fixture string.

The resulting plan hash is recorded in `canary_plan.json` under `plan_hash`. Evidence
artifacts exclude themselves from hashing and record the matching `plan_hash`.

### D. Bounded Request Ceilings

The plan and harness enforce:
- `max_sends`: 6 cumulative physical sends across both stages.
- `max_request_bytes`: 16,384 bytes per wire request.
- `max_completion_tokens`: 1024 (exact integer).
- `reasoning_effort`: `"low"`.
- `operation_deadline_seconds`: 110.0s.
- `request_timeout_seconds`: 60.0s.
- `max_retries`: 0 (zero automatic retry on provider 429, 5xx, or length limits).

### E. Token Accounting Unknowns

Per contract, token accounting is explicitly flagged as unknown until live accounting:
- `input_tokens_measured`: `false`
- `token_count_provenance`: `"unknown_until_live_accounting"`
- `server_side_enforcement_verified`: `false`
- `reasoning_token_accounting_verified`: `false`
- Measured serialized bytes do not establish input token counts or provider reasoning allowances.

---

## 3. Execution & Verification

### Running the Harness (CLI)

From `services/agent/`:

```powershell
# Run the full offline canary simulation and output artifacts
rtk .venv\Scripts\python.exe scripts/groq_canary.py --output-dir test-evidence/bs020

# Generate only the candidate plan artifact
rtk .venv\Scripts\python.exe scripts/groq_canary.py --plan-only --output-dir test-evidence/bs020
```

> **Note**: The script accepts no `--live` or provider flags. It reads no API keys from
> environment variables and constructs an explicit fail-closed offline mock transport.

### Running Quality Gates & Tests

```powershell
# Run full focused test suite (84 tests)
rtk .venv\Scripts\python.exe -m pytest -v tests/test_groq_model.py tests/test_groq_model_review.py tests/test_admission_probe.py tests/test_groq_bounds.py tests/test_groq_canary.py

# Lint and format checks
rtk .venv\Scripts\ruff.exe check scripts/groq_canary.py tests/test_groq_canary.py
rtk .venv\Scripts\ruff.exe format --check scripts/groq_canary.py tests/test_groq_canary.py

# Strict type check
rtk .venv\Scripts\mypy.exe src/borrowed_steps/infrastructure/groq_model.py scripts/groq_canary.py tests/test_groq_canary.py

# Verify package dependencies (72 locked packages)
rtk uv pip check
```

---

## 4. Artifact Reference

| Artifact | Location | Description |
|---|---|---|
| `canary_plan.json` | `services/agent/test-evidence/bs020/canary_plan.json` | Immutable execution plan, input hashes, ceilings, retry policy, and unknowns. |
| `canary_evidence.json` | `services/agent/test-evidence/bs020/canary_evidence.json` | Sanitized run evidence, per-stage sends, wire bytes, assertion results, and `provenance: "offline_fixture"`. |
