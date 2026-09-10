# Hosted Tasks Scheduler HTTP Setup & Integration Guide

This document describes the operational HTTP endpoints and packaging for the Borrowed Steps hosted scheduler, implementing the frozen contract in `docs/M3_SCHEDULER_HTTP_CONTRACT.md` over the BS-014 foundation.

---

## 1. Overview & Architecture

In hosted environments (`BS_RUNTIME=hosted`), persistent daemon threads (`TaskRunner`) are disabled. Instead, scheduled operational runs are triggered by an external timer (such as GitHub Actions cron or an external scheduler) via authenticated HTTP POST requests.

### Key Architectural Invariants
1. **Zero-DB Cold-Start Authentication**: Operational routes inspect and validate bearer authentication strictly in-memory before opening any database connection or calling driver routines. Unauthenticated or malformed requests never consume connection pool slots or database resources.
2. **Request-Time Schema Readiness**: Factory initialization (`create_app`) performs **zero database I/O**. Standard business endpoints enforce schema readiness via a synchronous threadpool dependency (`require_hosted_schema`), while operational endpoints use `HostedSchedulerService.check_schema()` with tight scheduler timeouts (2s connect / 500ms statement / 250ms lock).
3. **Hidden & Isolated Routes**: Operational endpoints are omitted from OpenAPI schemas (`include_in_schema=False`) and are completely unregistered in `BS_RUNTIME=local` mode (yielding 404).
4. **Immutable Defaults**: Operational endpoints ignore all request query parameters and body payloads. Candidate limit (100), processing budget (20s), and lease duration (60s) cannot be modified or overridden via HTTP.
5. **No-Store Caching**: All operational HTTP responses return `Cache-Control: no-store`.

---

## 2. Operational Endpoints

### 2.1 `POST /api/internal/tasks/tick`

Triggers a bounded task execution cycle.

- **Authentication**: Strict `Authorization: Bearer <BS_TASK_TICK_TOKEN>`.
- **Query & Body**: Disregarded. Defaults are hardcoded:
  - Candidate limit: 100
  - Monotonic processing budget: 20 seconds
  - Lease duration: 60 seconds
- **Response Headers**: `Cache-Control: no-store`
- **Response Codes**:
  - `200 OK`: Outcome is `success` or `partial`.
  - `409 Conflict`: Outcome is `busy` (another instance holds active lease) or `stale_lease` (lease expired and superseded).
  - `503 Service Unavailable`: Unconfigured token, unmigrated schema, failed evidence write, or task runner error.
  - `401 Unauthorized`: Missing, empty, malformed, or duplicate `Authorization` header, or invalid bearer token.
- **Response Payload**:
  ```json
  {
    "outcome": "success",
    "capacity_limited": false,
    "report": {
      "considered": 2,
      "marked_due": 2,
      "resolved_stale": 0,
      "unchanged": 0,
      "contended": 0,
      "stopped_early": false
    }
  }
  ```
  *(Internal `run_id` and raw error/driver strings are strictly omitted).*

### 2.2 `GET /api/internal/tasks/status`

Reads current persisted scheduler control and aggregate status without running tasks.

- **Authentication**: Strict `Authorization: Bearer <BS_TASK_TICK_TOKEN>`.
- **Side Effects**: Strictly none. Never runs tasks or mutates database state.
- **Response Headers**: `Cache-Control: no-store`
- **Response Codes**:
  - `200 OK`: Successful status read.
  - `503 Service Unavailable`: Unmigrated schema, database read failure, or unconfigured token.
  - `401 Unauthorized`: Missing or invalid bearer token.
- **Response Payload**:
  ```json
  {
    "status": "success",
    "last_outcome": "success",
    "last_success_at": "2026-09-09T10:00:00+00:00",
    "last_success_recent": true,
    "stale_warning": false,
    "counts": {
      "considered": 2,
      "marked_due": 2,
      "resolved_stale": 0,
      "unchanged": 0,
      "contended": 0,
      "stopped_early": false
    },
    "counts_complete": true
  }
  ```

---

## 3. Configuration & Security

### 3.1 Environment Variable: `BS_TASK_TICK_TOKEN`

| Property | Value |
| :--- | :--- |
| **Allowed Characters** | ASCII URL-safe only: `[A-Za-z0-9_-]` |
| **Length Bounds** | 32 to 256 characters |
| **Whitespace Policy** | Exact matching; no trimming or stripping |
| **Logging & Redaction** | Omitted from `repr(settings)`, excluded from logs and error messages |

If `BS_TASK_TICK_TOKEN` is unset, the application starts normally, but operational routes return generic 503 `SERVICE_UNAVAILABLE` before any database connection is opened.

An explicitly empty or malformed configured token fails Settings validation; it is not treated as unset.

### 3.2 Authentication Validation Rules
1. **Header Count**: Exactly one `Authorization` header must be present. Multiple headers fail with 401.
2. **Bearer Format**: Must start with `Bearer ` (case-sensitive) followed by a non-empty token string.
3. **Constant-Time Verification**: Validated using `hmac.compare_digest` against configured `task_tick_token`.
4. **Cookie Rejection**: Cookie-based session authentication (`bs_session`) is strictly ignored on operational endpoints.

---

## 4. Verification & Testing

### 4.1 Running Operational HTTP Tests Locally

Tests require a running PostgreSQL 16 instance. Set `BS_POSTGRES_TEST_URL` and run pytest:

```bash
# Set PostgreSQL test URL
set BS_POSTGRES_TEST_URL=postgresql://postgres@127.0.0.1:54345/postgres

# Run dedicated operational HTTP test suite
python -m pytest services/agent/tests/test_hosted_scheduler_http.py -v
```

### 4.2 Verifying Zero Database Connections on Cold-Start

Run the specific connection-tracking test to verify that unauthenticated requests never open psycopg connections:

```bash
python -m pytest services/agent/tests/test_hosted_scheduler_http.py -k test_unauthenticated_cold_start_causes_zero_database_connections -v
```

---

## 5. Inactive Workflow Example

An inactive GitHub Actions workflow example is provided at:
`docs/workflows/hosted-tick.yml.example`

### Inactive Example Invariants
- **Trigger**: Schedule `7,22,37,52 * * * *` and `workflow_dispatch` only.
- **Safety Gate**: `if: vars.BS_SCHEDULER_ENABLED == 'true'`.
- **Security**: `permissions: {}`, timeout 2 minutes, concurrency group `hosted-tasks-tick` (`cancel-in-progress: false`).
- **curl Parameters**: `--connect-timeout 10`, `--max-time 55`, `--retry 0`, no redirect following (`-L` omitted), discard output (`-o /dev/null`), validate HTTP 2xx.
- **DO NOT** place this workflow into `.github/workflows/` until production deployment and live scheduling are approved.

## Codex verification

## Codex BS-016 acceptance2026-09-10
ACCEPTED_LOCAL after direct corrections to workeredd3a05.541 tests passed,
zero failed/skipped; full suite 115.354s. Ruff lint/format, strict
mypy76 files and pip check72 packages pass. Workflow YAML/Bash syntax and16 offline
shell cases pass. No live workflow, provider call, cloud activation or spend.

New regressions failed on the worker source for both operational exception log
paths and the intake schema bypass, then passed after correction. Intake now
uses the request-time schema gate and performs session lookup off the event loop.
Operational errors log generic messages without exception details. Configuration
validation is centralized in Settings; scheduler typing no longer uses Any.
Real PostgreSQL HTTP tests now cover due notices, duplicate suppression, contention
partial results, event rollback/unknown counts and durable evidence-write failure.
The status no-task test now patches the actual imported call site. Workflow curl
disables default config and URL globbing, restricts protocols, suppresses raw error
details, validates token syntax and classifies only an exact three-digit2xx as success.

Original worker report below is historical and superseded by this acceptance.
No release/live scheduler/inference admission/video/submission acceptance implied.
NEXT_CODEX_MODE: ASTRA_LIGHT
REASON: Review complete; next step is routine manual AGY dispatch.
