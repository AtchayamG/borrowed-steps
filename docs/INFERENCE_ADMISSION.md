# Inference Admission Storage Specification & Interface

## 1. Overview & Architectural Boundaries

Per `docs/M3_ADMISSION_DECISION.md`, inference admission follows a strict division of responsibility:
- **Groq Provider**: Enforces provider token quotas (TPM/TPD, account-specific limits, 429 status codes).
- **PostgreSQL**: Controls application request allowances across distributed serverless instances. It enforces:
  - At most **one active operation** globally across all workspaces in the database.
  - Fixed reservation of **6 sends** per operation (never refunded).
  - Rolling 60-second global cap: **6 sends** (1 operation max).
  - Rolling 24-hour global cap: **120 sends** (20 operations max).
  - Rolling 24-hour per-workspace cap: **24 sends** (4 operations max).
  - Durable **15-minute global cooldown** following a confirmed provider 429.
  - Safe stale-operation recovery requiring explicit operator proof of dead worker.

**What PostgreSQL Does NOT Do**:
- It does **not** estimate input tokens from byte counts.
- It does **not** treat output tokens as a total-token guarantee.
- It does **not** promise 20 successful interpretations per day (tokens are best-effort).
- It does **not** auto-release expired operations (fail-closed concurrency protection).

---

## 2. Schema Migration V4 (`inference_admissions`)

Migration V4 appends table `inference_admissions` to `services/agent/src/borrowed_steps/infrastructure/postgres_migrations.py`. Migrations V1–V3 are preserved byte-for-byte.

```sql
CREATE TABLE IF NOT EXISTS inference_admissions (
    reservation_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id),
    request_key_hash VARCHAR(64) NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    owner_execution_id UUID NOT NULL,
    state VARCHAR(32) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    reserved_sends INTEGER NOT NULL DEFAULT 6,
    actual_sends INTEGER,
    actual_total_tokens BIGINT,
    failure_code VARCHAR(64),
    cleanup_completed BOOLEAN NOT NULL DEFAULT FALSE,
    provider_429 BOOLEAN NOT NULL DEFAULT FALSE,
    deadline_at TIMESTAMPTZ NOT NULL,
    dispatched_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    recovered_at TIMESTAMPTZ,
    recovered_by UUID,
    recovery_reason VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    CONSTRAINT ck_inference_admissions_reserved_sends CHECK (reserved_sends = 6),
    CONSTRAINT ck_inference_admissions_active_state CHECK (
        is_active = (state IN ('RESERVED', 'DISPATCHED', 'UNCERTAIN'))
    ),
    CONSTRAINT ck_inference_admissions_state CHECK (
        state IN ('RESERVED', 'DISPATCHED', 'SUCCEEDED', 'FAILED_CONFIRMED', 'UNCERTAIN', 'RECOVERED')
    ),
    CONSTRAINT ck_inference_admissions_tokens CHECK (
        actual_total_tokens IS NULL OR actual_total_tokens >= 0
    ),
    CONSTRAINT ck_inference_admissions_actual_sends CHECK (
        actual_sends IS NULL OR (actual_sends >= 0 AND actual_sends <= 6)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_inference_admissions_single_active
    ON inference_admissions (is_active)
    WHERE is_active IS TRUE;

CREATE UNIQUE INDEX IF NOT EXISTS ux_inference_admissions_workspace_request_key
    ON inference_admissions (workspace_id, request_key_hash);
```

---

## 3. Storage Interface (`InferenceAdmissionStore`)

Located at `services/agent/src/borrowed_steps/infrastructure/inference_admission.py`.

### A. Lifecycle States (`AdmissionState`)
- `RESERVED`: Initial state created by `reserve`. Concurrency slot acquired (`is_active=True`).
- `DISPATCHED`: Transitioned via `mark_dispatched`. Authorizes provider network call.
- `SUCCEEDED`: Terminal state. Requires `cleanup_completed=True`, `actual_sends` in `1..6`. Slot released (`is_active=False`, `released_at=NOW()`).
- `FAILED_CONFIRMED`: Terminal state. Requires `cleanup_completed=True`. Slot released.
- `UNCERTAIN`: Non-terminal state. Concurrency slot retained (`is_active=True`, `released_at=None`) to prevent races when execution state is lost or cleanup fails.
- `RECOVERED`: Dead recovery state via `recover_dead`. Requires `confirmed_dead=True` and canonical operator ID. Clears slot (`is_active=False`, `released_at=NOW()`).

### B. Core Methods

#### 1. `reserve(request: AdmissionReservationRequest) -> AdmissionRecord`
- Verifies input types, formats (UUID4, 64-char lowercase hex).
- Obtains transaction advisory lock `_ADMISSION_LOCK = 8_801_103_331_031`.
- Enforces provider 429 15-minute global cooldown (`AdmissionRefusedError(CODE_PROVIDER_COOLDOWN_ACTIVE)`).
- Enforces single active operation invariant across all workspaces (`AdmissionRefusedError(CODE_ANOTHER_OPERATION_ACTIVE)`).
- Enforces rolling 60s global cap <= 6 sends (`AdmissionRefusedError(CODE_GLOBAL_LIMIT_60S_EXCEEDED)`).
- Enforces rolling 24h global cap <= 120 sends (`AdmissionRefusedError(CODE_GLOBAL_LIMIT_24H_EXCEEDED)`).
- Enforces rolling 24h per-workspace cap <= 24 sends (`AdmissionRefusedError(CODE_WORKSPACE_LIMIT_24H_EXCEEDED)`).
- **Idempotent Replay**: If `(workspace_id, request_key_hash)` exists, exact match of `payload_hash` and `reservation_id` returns the existing record without debiting. Any change raises `AdmissionReplayConflictError`.

#### 2. `mark_dispatched(reservation_id: str, owner_execution_id: str) -> None`
- Validates reservation exists and `owner_execution_id` matches.
- Exactly one transition from `RESERVED` -> `DISPATCHED` permitted.
- Rejects if `deadline_at` has passed (`AdmissionRefusedError(CODE_DEADLINE_EXPIRED)`).
- Replay: If already `DISPATCHED` with matching owner, succeeds as no-op. If non-matching owner or terminal state, raises `AdmissionRefusedError`.

#### 3. `finish(...) -> AdmissionRecord`
- Transitions `DISPATCHED` -> `SUCCEEDED`, `FAILED_CONFIRMED`, or `UNCERTAIN`.
- `SUCCEEDED` requires `cleanup_completed=True` and `actual_sends` integer `1..6`.
- `FAILED_CONFIRMED` requires `cleanup_completed=True`.
- If `cleanup_completed=False` for a confirmed state, refuses and retains active reservation.
- If `state == UNCERTAIN`, remains active (`is_active=True`, `released_at=None`).
- Replay: Exact replay with identical evidence returns the record. Replay with conflicting fields or attempting to settle `UNCERTAIN` without recovery raises `AdmissionRefusedError`.

#### 4. `recover_dead(reservation_id: str, operator_execution_id: str, reason: RecoveryReason, confirmed_dead: bool = True) -> AdmissionRecord`
- Recovers dead or orphaned reservations from `RESERVED`, `DISPATCHED`, or `UNCERTAIN` to `RECOVERED`.
- Sets `is_active=False` and `released_at=NOW()` (evaluating current DB time for rolling window debit).
- Refuses recovery if already in a confirmed terminal state (`SUCCEEDED`, `FAILED_CONFIRMED`).
- Replay: Exact replay with same operator and reason returns record; conflicting operator/reason raises `AdmissionReplayConflictError`.

---

## 4. Usage Example

```python
from borrowed_steps.infrastructure.inference_admission import (
    InferenceAdmissionStore,
    AdmissionReservationRequest,
    AdmissionState,
    AdmissionFailureCode,
)

store = InferenceAdmissionStore(db_url="postgresql://postgres@localhost:5432/app")

# 1. Acquire Admission Reservation
req = AdmissionReservationRequest(
    reservation_id="550e8400-e29b-41d4-a716-446655440000",
    workspace_id="11111111-2222-3333-4444-555555555555",
    request_key_hash="a" * 64,
    payload_hash="b" * 64,
    owner_execution_id="22222222-3333-4444-5555-666666666666",
)
record = store.reserve(req)

# 2. Mark Dispatched before Network Transport
store.mark_dispatched(
    reservation_id=record.reservation_id,
    owner_execution_id="22222222-3333-4444-5555-666666666666",
)

# 3. Perform Inference & Teardown (Out-of-band)
# Ensure local socket/transport cleanup completes first!

# 4. Settle Reservation
finished = store.finish(
    reservation_id=record.reservation_id,
    owner_execution_id="22222222-3333-4444-5555-666666666666",
    state=AdmissionState.SUCCEEDED,
    cleanup_completed=True,
    actual_sends=3,
    actual_total_tokens=1420,
)
```

---

## 5. Explicit Limitations & Boundaries

1. **Storage Boundary Only**: `InferenceAdmissionStore` provides atomic database concurrency and allowance ledgering. It contains zero network transport, HTTP routes, or authentication mechanisms.
2. **Fail-Closed Concurrency**: Expired deadlines or failed executions **never** auto-release. If an executor crashes without completing cleanup, the reservation remains `is_active=True` until an operator issues `recover_dead`.
3. **Database Scope**: Rolling allowances and single-active constraints apply across all processes connecting to the **same PostgreSQL database**. Unrelated deployments or separate databases do not coordinate allowances.
4. **Best-Effort Availability**: Retains the verified Groq Free tier. If provider 429 occurs, a 15-minute global pause is enforced at the database level. No payment method or automatic fallback tier is permitted.
