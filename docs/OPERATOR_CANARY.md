# Bounded Operator Canary Runner (`scripts/operator_canary.py`)

## 1. Overview & Architecture

The Bounded Operator Canary Runner implements the final execution boundary before any live Groq provider interaction. It integrates:
1. **Deterministic Execution Manifest Verification**: Binds the exact SHA-256 hashes of 9 critical files, frozen candidate plan hash, ceilings, and retry policies before any network or database mutation.
2. **Explicit Operator Grant**: An immutable, dated authorization (`OperatorGrant`) that strictly requires non-empty UUIDs, future UTC expiry, plan hash matching, and `live_authorized=True`.
3. **Two-Phase Database Boundary**: Atomically executes `store.reserve` followed by `store.mark_dispatched` against PostgreSQL. Only a successful first-time transition to `DISPATCHED` authorizes transport construction or request dispatch.
4. **Transport & Secret Isolation**: Wraps the transport in a `TransportObserver` capturing only request index, serialized wire byte count, and HTTP status code. Zero API keys, headers, prompt text, model output, or exceptions are logged or stored.
5. **No Automatic Retry**: Enforces `retry_strategy=None` on Strands Agent execution. Any 429 rate limit or invalid output settles cleanly without retrying or consuming extra sends.
6. **Conservative Settlement**:
   - `SUCCEEDED`: Successful execution of tool call and structured extraction with quoted source grounding assertions verified.
   - `FAILED_CONFIRMED`: Settled provider error (429 rate limit) or output assertion failure without retrying.
   - `UNCERTAIN`: Cancellation (`CANCELLED`), cleanup failure (`EXECUTION_UNKNOWN`), or database finalization failure. Retains `concurrency_active=True` in PostgreSQL to block any further canary runs until explicit operator recovery.

---

## 2. Operator Grant Specification

An execution requires an explicit `OperatorGrant` instance passed to `run_operator_canary`:

```python
@dataclass(frozen=True)
class OperatorGrant:
    receipt_id: str                   # Valid UUID4 string
    owner_id: str                     # Valid UUID4 string
    authorization_id: str             # Valid UUID4 string
    authorization_expires_at: datetime # Timezone-aware UTC datetime in the future
    execution_manifest_hash: str      # Exact 64-hex SHA-256 of build_execution_manifest()
    plan_hash: str = ACCEPTED_PLAN_HASH  # Frozen BS-020 candidate plan hash
    live_authorized: bool = False     # MUST be explicitly True for live execution
```

Fail-Closed Invariants:
- `receipt_id`, `owner_id`, `authorization_id` must be valid UUID4 strings.
- `authorization_expires_at` must have timezone info and must be strictly in the future (`> datetime.now(UTC)`).
- `plan_hash` must match `ACCEPTED_PLAN_HASH` (`38ec48176db21d7f947cfdc1ae1b3b211efdbdb55c976043462aea85a16a3031`).
- `execution_manifest_hash` must match `compute_execution_manifest_hash(build_execution_manifest())`.
- `live_authorized` must be explicitly `True`.

---

## 3. Execution Manifest Specification

The execution manifest hashes 9 checkout source files separate from the frozen BS-020 candidate plan:
- `services/agent/scripts/operator_canary.py`
- `services/agent/scripts/groq_canary.py`
- `services/agent/scripts/admission_probe.py`
- `services/agent/src/borrowed_steps/infrastructure/groq_model.py`
- `services/agent/src/borrowed_steps/infrastructure/strands_interpreter.py`
- `services/agent/src/borrowed_steps/infrastructure/canary_receipt.py`
- `services/agent/requirements.lock`
- `services/agent/requirements-groq.lock`
- `services/agent/requirements-postgres.lock`

Pinned ceilings:
- `max_sends`: 6
- `max_completion_tokens`: 1024
- `max_output_tokens_reservation`: 6144 (6 sends x 1024 cap)
- `max_request_bytes`: 16384
- `max_tool_attempts`: 2
- `operation_deadline_seconds`: 110.0
- `request_timeout_seconds`: 60.0
- `reasoning_effort`: "low"
- `retry_strategy`: None (`auto_retry: false`)

---

## 4. Callable Entrypoint

```python
async def run_operator_canary(
    api_key: str,
    database_url: str,
    grant: OperatorGrant,
    *,
    base_dir: Path | None = None,
    fixture_text: str = CANARY_FIXTURE_INPUT,
    inv_reader: InventoryReader | None = None,
    fixture_transport: httpx.AsyncBaseTransport | None = None,
) -> OperatorCanarySummary:
    ...
```

Parameters:
- `api_key`: Explicit provider key. Must not be empty. In offline tests, `OFFLINE_DUMMY_KEY` is accepted when `fixture_transport` is supplied.
- `database_url`: PostgreSQL DSN with schema v3 migrated. Must not be empty.
- `grant`: Validated `OperatorGrant`.
- `fixture_transport`: Optional offline mock transport. If omitted, constructs `httpx.AsyncHTTPTransport(retries=0, trust_env=False)` and records provenance as `"live_provider"`.

Return:
Returns `OperatorCanarySummary` containing sanitized non-secret execution audit data:
- `receipt_id`, `owner_id`, `authorization_id`
- `execution_manifest_hash`, `plan_hash`
- `provenance` (`"offline_fixture"` or `"live_provider"`)
- `state` (`CanaryState.SUCCEEDED`, `CanaryState.FAILED_CONFIRMED`, or `CanaryState.UNCERTAIN`)
- `actual_sends`: Integer count of observed HTTP requests.
- `actual_total_tokens`: `None` (`NULL` in PostgreSQL; Groq tokens are unmeasured).
- `failure_code`: `FailureCode` enum or `None`.
- `cleanup_completed`: Boolean indicating client connection closure.
- `receipt_row`: Serialized dictionary of the finalized PostgreSQL receipt row.
- `wire_byte_lengths`: List of request payload byte sizes.
- `status_codes`: List of response HTTP status codes.
- `field_assertions`: Mapping of verified extraction assertions.

---

## 5. CLI Commands

The CLI supports only manifest preparation and offline checkout verification:

```bash
# Generate and display execution manifest JSON
python scripts/operator_canary.py --prepare-manifest

# Save manifest to file
python scripts/operator_canary.py --prepare-manifest --output test-evidence/bs022/operator_manifest.json

# Check current checkout against deterministic manifest computation
python scripts/operator_canary.py --check-manifest
```
