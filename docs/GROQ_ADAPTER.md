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

# Groq transport adapter (`GroqModel`) — offline verification & contract specification

**Worker Task**: BS-012 (AGY)
**Date**: 2026-09-09
**Status**: Accepted offline after direct Codex correction. See [review](BS-012_ACCEPTANCE.md). No live Groq requests performed.
**Authority**: `docs/M3_HOSTED_CONTRACT.md`, `D:/Work/Codex/Hackathon Projects/Agents For Humans/00_PROGRAM_CONTROL/AGENTS_FOR_HUMANS_MASTER_WINNING_BLUEPRINT_v5_FINAL.md`.

---

## 1. Overview & Non-Claims

`GroqModel` is a small, strictly typed transport adapter subclassing Strands `OpenAIModel` (`strands.models.openai.OpenAIModel`). It is designed for future M3 hosted mode deployment on Groq's free tier with `openai/gpt-oss-20b`.

### Non-Claims & Critical Disclaimers:
- **No live model calls performed**: ZERO live calls were made to remote Groq endpoints or local Ollama instances. All verification was executed strictly using mocked/intercepted HTTP transports (`httpx.MockTransport`).
- **No cloud spend / No account provisioning**: No accounts were provisioned, no credit cards or credentials were bound, and no network egress to provider IP addresses occurred.
- **Not production ready**: Release-blocking admission control (shared transactional rate limiting, account-level token reservations, cross-instance concurrency lease) and application HTTP route wiring remain explicitly pending in later tasks.
- **No guaranteed-free or SLA claim**: Free-tier availability, rate limits (e.g. 8,000 tokens/min), and hosting quotas are provider-governed and best-effort.

---

## 2. Pinned Interfaces & Constructor

`GroqModel` is defined in `services/agent/src/borrowed_steps/infrastructure/groq_model.py`.

```python
class GroqModel(OpenAIModel):
    def __init__(
        self,
        api_key: str,
        *,
        max_sends: int = DEFAULT_MAX_SENDS,                            # default 6, capped at 6
        operation_deadline_seconds: float = DEFAULT_OPERATION_DEADLINE_SECONDS,  # default 110.0, capped at 110.0
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,        # default 60.0, capped at 60.0
        transport: httpx.AsyncBaseTransport | None = None,            # explicit test injection only
    ) -> None:
        ...
```

### Constructor Rules:
1. **Explicit API Key Required**: Must be a non-empty string. Fails immediately with `ValueError` if empty, whitespace, or non-string. Does NOT read environment variables (`GROQ_API_KEY`, `OPENAI_API_KEY`) to prevent ambient credential leakage.
2. **Pinned Constants**:
   - `GROQ_BASE_URL = "https://api.groq.com/openai/v1"`
   - `GROQ_MODEL_ID = "openai/gpt-oss-20b"`
   - Endpoint override is not permitted.
3. **Injectable Reductions Only**:
   - `max_sends` cannot exceed 6 (`1 <= max_sends <= 6`).
   - `operation_deadline_seconds` cannot exceed 110.0s (`0.0 < operation_deadline_seconds <= 110.0`).
   - `request_timeout_seconds` cannot exceed 60.0s (`0.0 < request_timeout_seconds <= 60.0`).
   - Attempts to increase limits fail closed with `ValueError`.
4. **No Import or Construction Side Effects**: Instantiating `GroqModel` creates no background tasks, makes no network calls, and opens no connections.

---

## 3. Bounded-Send & Deadline Semantics

### Sticky 6-Send Budget (`SendBudget`):
- One fresh `GroqModel` instance represents one interpretation and is shared across:
  1. Stage 1 tool loop (initial proposal + tool execution continuations).
  2. Stage 2 structured output extraction.
  3. Any SDK/transport retry attempts.
- **Charge Location**: Budget is charged at the actual physical HTTP transport boundary (`_BoundedGroqTransport.handle_async_request`), NOT merely at entry to `stream()` or `structured_output()`.
- **Sticky Exhaustion**: Once `sent >= max_sends`, `exhausted` becomes permanently `True`. Any 7th send attempt or post-exhaustion call immediately raises `GroqSendBudgetExceededError`.

### Monotonic Operation Deadline:
- An absolute monotonic deadline timestamp is established at construction: `deadline_monotonic = time.monotonic() + operation_deadline_seconds`.
- **Pre-Send Check**: If `time.monotonic() >= deadline_monotonic`, incoming calls immediately fail closed with `GroqDeadlineExpiredError`.
- **In-Flight Bound**: Effective HTTP timeout is computed as `min(request_timeout_seconds, deadline_monotonic - now)`.
- **Chunk Stream Bound**: Response byte streams are wrapped in `_DeadlineByteStream`. If reading chunks stalls past the monotonic deadline, the stream closes and raises `GroqDeadlineExpiredError`.

### Strict Target & Request Envelope Verification:
- Fails closed with `GroqTargetRefusedError` if the request URL is non-HTTPS, host is not `api.groq.com`, or path is not `/openai/v1/chat/completions`.
- Fails closed with `GroqEnvelopeRefusedError` (subclass of `GroqTargetRefusedError`) before budget debit or inner dispatch if:
  - Wire payload exceeds `MAX_REQUEST_BYTES = 16_384` bytes.
  - Body is empty, malformed UTF-8, malformed JSON, or non-object.
  - Model does not match `GROQ_MODEL_ID = "openai/gpt-oss-20b"`.
  - `max_completion_tokens` is missing, non-integer (including boolean), or not exactly `FIXED_MAX_COMPLETION_TOKENS = 1024`.
  - Deprecated `max_tokens` is present.
  - `reasoning_effort` is missing, None, or not `'low'`.
  - Field `n` is present and not integer `1`.
- **Non-Debit Invariant**: Envelope rejections do NOT call the inner transport and do NOT charge `SendBudget`.

### 429 & Retry-After Preservation:
- When the provider responds with HTTP 429, the transport records `model.last_status_code = 429` and parses the `Retry-After` header into `model.last_retry_after: float | None`.
- Does NOT perform unbounded sleeping or silent background retries. OpenAI SDK raises `RateLimitError` which Strands converts to `ModelThrottledException`.

### Resource Lifecycle & Cancellation:
- Ephemeral `httpx.AsyncClient` instances created inside `_get_client()` are closed per request.
- The underlying `_BoundedGroqTransport` stays open for the lifetime of the `GroqModel` instance and is permanently closed via `await model.aclose()` or async context manager exit (`async with GroqModel(...) as model:`).
- Cancelled stream tasks abort cleanly without leaking lingering coroutines.

---

## 4. Stage 1 & Stage 2 Wire Shapes

Both stages share the 6-send sticky budget and enforce the frozen request envelope (<= 16,384 bytes wire size, 1024 max completion tokens, low reasoning effort).

### Stage 1: Real Strands Agent Tool Loop
- Request wire JSON:
  - `model`: `"openai/gpt-oss-20b"`
  - `max_completion_tokens`: `1024`
  - `reasoning_effort`: `"low"`
  - `stream`: `true`
  - `tools`: includes function schemas (e.g. `read_inventory`)
  - `response_format`: NOT present.
  - `max_tokens`: NOT present.
- Model returns SSE chunk with `tool_calls`. The SDK executes `read_inventory`. The continuation turn sends `role: "tool"` message and receives final text.
- Tool provenance is verified: the tool is executed by the agent runtime, not called directly.

### Stage 2: Public Structured Output Extraction
- Invoked via `model.structured_output(_Extraction, prompt=..., system_prompt=...)`.
- Request wire JSON:
  - `model`: `"openai/gpt-oss-20b"`
  - `max_completion_tokens`: `1024`
  - `reasoning_effort`: `"low"`
  - `stream`: `false`
  - `max_tokens`: NOT present.
  - `tools`: NOT present (removed unconditionally).
  - `tool_choice`: NOT present (removed unconditionally).
  - `stream_options`: NOT present (streaming-only field popped).
  - `response_format`:
    ```json
    {
      "type": "json_schema",
      "json_schema": {
        "name": "_Extraction",
        "strict": true,
        "schema": {
          "type": "object",
          "properties": {
            "borrower_label": { "anyOf": [{ "type": "string" }, { "type": "null" }] },
            "equipment_kind": { "anyOf": [{ "type": "string" }, { "type": "null" }] },
            "pickup_location": { "anyOf": [{ "type": "string" }, { "type": "null" }] },
            "due_at": { "anyOf": [{ "type": "string" }, { "type": "null" }] }
          },
          "required": ["borrower_label", "equipment_kind", "pickup_location", "due_at"],
          "additionalProperties": false
        }
      }
    }
    ```
- Parses directly via OpenAI SDK `.parse()`:
  - Valid payload parses into `_Extraction` instance.
  - Unstated fields returned as `null` map to `None`.
  - Refusals (`choice.message.refusal`) raise `ValueError`.
  - Invalid JSON raises a sanitized `ValueError`, without echoing model output.

---

## 5. Dependency Installation & Verification

### Overlay Requirements (`services/agent/requirements-groq.lock`):
Pinned on top of unchanged baseline `requirements.lock`:
```text
aws-bedrock-token-generator==1.1.0
distro==1.9.0
jiter==0.16.0
openai==2.54.0
sniffio==1.3.1
tqdm==4.70.0
```

### Installation:
```bash
# In services/agent/
uv pip install -r requirements-groq.lock
uv pip check
```

### Verification Commands:
```bash
# 1. Type checking (strict)
rtk .venv\Scripts\mypy --strict src/borrowed_steps/infrastructure/groq_model.py tests/test_groq_model.py

# 2. Linting and formatting
rtk .venv\Scripts\ruff check src/borrowed_steps/infrastructure/groq_model.py tests/test_groq_model.py
rtk .venv\Scripts\ruff format --check src/borrowed_steps/infrastructure/groq_model.py tests/test_groq_model.py

# 3. Focused Groq tests (31 tests including reviewer regressions)
rtk proxy .venv\Scripts\python.exe -m pytest -v tests/test_groq_model.py tests/test_groq_model_review.py

# 4. Full test suite (413 tests)
rtk .venv\Scripts\pytest -q
```

---

## 6. Known Compatibility Limits & Pending Work

1. **Global Shared Admission (Release-Blocking)**: Groq's 8,000 token/minute ceiling and account-level daily limits require transactional global token/request reservations with crash-safe lease expiration across serverless instances before enabling live hosted mode.
2. **Two-Stage Interpreter Integration**: `StrandsOllamaInterpreter` currently instantiates `OwnedOllamaModel`. Wiring `GroqModel` into the interpretation pipeline, settings (`BS_ASSISTANT_PROVIDER=groq`), and provenance (`strands / groq / openai/gpt-oss-20b`) belongs to a future integration task.
3. **No Live Groq Model Validation**: End-to-end token latency, reasoning overhead, and actual Groq provider response fidelity remain to be measured in an authorized live probe gate.

Reviewer amendments: timeout limits must be finite. Both headers and stalled body reads are interrupted by the earlier request/operation deadline. Model pinning is enforced at the wire even if parent configuration changes. Target checks also reject nonstandard ports, query strings, userinfo and non-POST methods. Client and transport close waits are bounded to two seconds each; failed close remains retryable, and an unresolved client blocks reuse. Integration must use the async context manager, consume or explicitly close generators, and keep the existing outer interpretation deadline around Strands orchestration (including its retry delays and tool execution).
