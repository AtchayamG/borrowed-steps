# Hosted Task Scheduler Setup and Operation Guide

This document describes the hosted task scheduler foundation implemented in BS-014 for Borrowed Steps milestone M3, conforming to `docs/M3_OPERATIONS_CONTRACT.md`.

## Overview

In hosted production environments (e.g. running on serverless platforms with scheduled cron invocations), persistent background daemon threads (`TaskRunner`) are disabled. Instead, task coordination is performed by a bounded, callable scheduler service (`HostedSchedulerService`) invoked on a scheduled cadence (e.g., GitHub Actions cron or external scheduler).

The scheduler processes due task notices (such as overdue equipment pickup or return reminders), records aggregate execution evidence, and enforces strict concurrency, timeout, and lease boundaries.

## Architectural Components

### 1. Schema Migration V2 (`scheduler_control`)

Schema version 2 appends a singleton control row in PostgreSQL (`scheduler_control` with `id = 1`):
- `active_run_id`: Unique run identifier of the currently executing scheduler instance, or `NULL` if idle.
- `active_run_started_at`: Database timestamp when the current run was admitted.
- `active_run_expires_at`: Database timestamp when the current run lease expires (default: +60 seconds).
- `last_run_id`: Run ID of the most recently finalized scheduler run.
- `last_outcome`: Terminal outcome of the last run (`'never_run'`, `'success'`, `'partial'`, `'failed'`).
- `last_completed_at`: Database timestamp of the last finalization.
- `last_success_at`: Database timestamp of the last *successful* completion (preserved across failed/partial runs).
- `last_considered`, `last_marked_due`, `last_resolved_stale`, `last_unchanged`, `last_contended`, `last_stopped_early`: Persisted aggregate counts from the last execution.

**Privacy & Security Invariant**: The control row contains **zero** borrower text, intake notes, session tokens, database URLs, or workspace/entity identifiers.

### 2. Callable Service (`HostedSchedulerService`)

Located in `borrowed_steps.infrastructure.hosted_scheduler`:
- **Read-Only Schema Gate**: Constructor does not execute network or database I/O. `check_schema()` strictly requires PostgreSQL migration version 2.
- **Atomic Admission Transaction**: Claims execution lease by setting `active_run_id` and `active_run_expires_at` in a short transaction with lock/statement timeouts. If an unexpired run exists, returns `TickResult(outcome="busy")`.
- **Decoupled Task Processing**: Candidate processing runs entirely *outside* any transaction on `scheduler_control`. Workspace locks are acquired per-workspace during task processing.
- **Atomic Finalization Transaction**: Conditionally updates `scheduler_control` `WHERE id = 1 AND active_run_id = run_id`. If another instance superseded an expired lease, the stale update affects 0 rows and returns `stale_lease`, preventing stale tokens from corrupting newer evidence.

### 3. Execution Bounds & Monotonic Processing Budget

- **Candidate Discovery Limit**: Discovers at most 100 candidates ordered by `due_at, id`.
- **Monotonic Processing Budget (20s)**: Candidates cease starting once the local monotonic deadline expires (`MonotonicDeadlineStopSignal`). In-flight candidate transactions complete safely.
- **Scheduler-specific SQL limits**: connection 2 seconds, each statement 500ms,
  lock wait 250ms; ordinary Store defaults are unchanged. Constructor overrides
  may tighten these ceilings. The processing budget must be finite, 0..20s;
  the scheduler lease is fixed at60s. Injected stores must use matching limits.
- **Conditional timing calculation**: statement timeout bounds each statement,
  not the transaction. Count BEGIN, timeout setup and COMMIT too. Readiness:
  2 + 7*0.5 =5.5s; admission:2 + 6*0.5 =5s; processing budget20s;
  one last candidate:2 + 9*0.5 =6.5s; finalization:2 + 6*0.5 =5s.
  The resulting42s database-path envelope fits the45s target under these SQL
  and connection conditions. Rollback replaces commit on failure. Discovery
  is inside the20s budget. Tests exercise accumulating successful slow SQL,
  statement cancellation and lock contention. This is not an absolute wall
  clock guarantee against arbitrary OS, DNS, network or platform suspension,
  nor production latency proof. Hosted connection behavior still needs checking.

### 4. Outcome Semantics

| Outcome | Condition |
| :--- | :--- |
| `success` | All discovered candidates processed without contention, early stop, or reaching candidate limit. |
| `partial` | Task execution encountered workspace lock contention (`StorageBusyError`), stopped early due to budget expiry, or hit candidate limit (>= 100 capacity-limited). |
| `busy` | Another scheduler instance holds an active, unexpired lease (`db_now < active_run_expires_at`). |
| `failed` | Task processing raised an unexpected error. Prior `last_success_at` is preserved. |
| `expired` | Active run lease passed (`db_now >= active_run_expires_at`) without finalization (e.g. process hard kill). |

### 5. Read Status Function (`read_status()`)

Returns `SchedulerStatus` containing:
- `status`: `'never_run'`, `'running'`, `'success'`, `'partial'`, `'failed'`, or `'expired'`.
- `last_outcome`: Outcome of the most recent completed run.
- `last_success_at`: Timestamp of the last successful run.
- `last_success_recent`: `True` if `last_success_at` is between zero and 30 minutes before current DB time; `False` triggers `stale_warning = True`.
- `counts`: Aggregate counters, or `None` when processing failed and partial counts are unknown.
- `counts_complete`: False for a failed run. Earlier candidate commits may survive; never interpret stored zero placeholders as exact failed-run counts.
- Public `to_dict()` omits internal run IDs. Errors are generic and exclude driver details.

`read_status()` is strictly read-only and never runs tasks or mutates database state.

---

## Local Verification & Testing

### 1. Requirements
- Python 3.11 with virtual environment initialized (`services/agent/.venv`).
- Local PostgreSQL 16 server running (e.g., on port 54340).

### 2. Running Migrations
To bring a database to schema version 2:
```bash
set BS_POSTGRES_ADMIN_URL=postgresql://postgres@127.0.0.1:54340/your_db
python services/agent/scripts/postgres_migrate.py
```
Output:
```
PostgreSQL schema version: 2
```

### 3. Running Scheduler Tests
Set the test database URL and run pytest:
```bash
set BS_POSTGRES_TEST_URL=postgresql://postgres@127.0.0.1:54340/postgres
pytest services/agent/tests/test_hosted_scheduler.py -v
```

### 4. Direct Python Usage Example
```python
from borrowed_steps.infrastructure.hosted_scheduler import HostedSchedulerService

database_url = "postgresql://user:pass@host/db?sslmode=require"
scheduler = HostedSchedulerService(database_url)

# Read status without executing work
status = scheduler.read_status()
print("Scheduler status:", status.status)
print("Last outcome:", status.last_outcome)
print("Stale warning:", status.stale_warning)

# Execute one bounded tick
result = scheduler.tick(limit=100, processing_budget_seconds=20.0)
print("Tick outcome:", result.outcome)
if result.report:
    print(f"Considered: {result.report.considered}, Marked due: {result.report.marked_due}")
```

---

## Boundaries & Non-Goals in BS-014

1. **No New HTTP Route**: `POST /api/internal/tasks/tick` is deferred to subsequent operational packaging.
2. **No Background Runner in Hosted Mode**: `BS_TASKS_ENABLED=0` remains strictly enforced for hosted runtime.
3. **No Model Inference**: Zero inference calls or Groq spend executed; assistant remains disabled (`BS_ASSISTANT_ENABLED=0`).
4. **No Public Cloud Activation**: Local PostgreSQL loopback testing only.
