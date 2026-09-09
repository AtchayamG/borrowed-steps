# Hosted Runtime Setup (BS-013)

This document describes the configuration, startup behavior, security controls, and local verification of the Borrowed Steps hosted FastAPI runtime.

The hosted runtime wires the accepted `PostgresStore` into the FastAPI HTTP application factory (`create_app`), proving the structured human-in-the-loop equipment loan workflow over HTTP against PostgreSQL. Assistant inference and autonomous task scheduling remain explicitly disabled in this increment.

## Runtime Configuration Matrix

The application supports two mutually exclusive runtime modes: `local` and `hosted`. Invalid combinations fail closed immediately at configuration load and factory initialization boundaries.

| Setting | Local Default | Hosted Requirement | Description |
| :--- | :--- | :--- | :--- |
| `BS_RUNTIME` | `local` | `hosted` | Explicit runtime environment |
| `BS_STORE` | `sqlite` | `postgres` | Storage backend; hosted requires `postgres` |
| `BS_DATABASE_URL` | None | `postgresql://...` | Private PostgreSQL connection URI (redacted from repr/logs) |
| `BS_COOKIE_SECURE` | `0` | `1` | Enforces `Secure` flag on session cookies |
| `BS_ALLOWED_ORIGINS` | `http://localhost:5173,...` | Non-empty HTTPS list | Comma-separated list of allowed frontend HTTPS origins |
| `BS_TASKS_ENABLED` | `1` | `0` | Background task runner; explicitly disabled in hosted increment |
| `BS_ASSISTANT_ENABLED` | `0` | `0` | Assistant inference; disabled in this increment |
| `BS_ASSISTANT_PROVIDER` | `ollama` | `groq` | Provider selector; hosted reserves `groq` without loading SDK |

### Origin Allowlist Invariants

In hosted mode, each origin in `BS_ALLOWED_ORIGINS` must satisfy:
- Scheme must be `https://` (http is rejected).
- Must have a valid DNS hostname or a public IP address; malformed and legacy numeric hosts are rejected.
- Wildcards (`*`), user credentials (`user:pass@`), URL paths (`/path`), query strings (`?foo=1`), and fragments (`#bar`) are rejected.
- Localhost and loopback IPs (`127.0.0.1`, `127.0.0.2`, `[::1]`) are forbidden for hosted origins.
- Optional port must be a valid integer in range 1–65535.

## Factory Initialization & Startup Gates

When `create_app()` initializes in hosted mode:
1. **Configuration Validation**: Evaluates settings against all hosted requirements. Direct `Settings` construction cannot bypass these invariants.
2. **Lazy PostgreSQL Driver Import**: `PostgresStore` is imported dynamically inside `create_app` only when `runtime == "hosted"`, keeping the base local install free of compulsory PostgreSQL dependencies.
3. **Read-Only Schema Verification**: Invokes `store.check_schema()` before serving any request. Schema version must equal 1. Unmigrated databases (version 0) or future schemas (version > 1) fail startup with `SchemaVersionError`. No DDL or migrations are ever run at runtime or during health checks.
4. **TaskRunner Disabled**: `runner` remains `None`. No scheduler thread is spawned, and `app.state.tasks` is `None`.
5. **Assistant Disabled**: `assistant` remains `None`. The `/api/intake/interpret` endpoint returns `503 ASSISTANT_DISABLED` without performing inference or mutating state. Injected interpreters in test harnesses cannot bypass this gate.
6. **Health Endpoint**: `GET /api/health` returns:
   ```json
   {
     "status": "ok",
     "milestone": "M3",
     "agent_mode": "disabled"
   }
   ```
   Health checks are pure liveness probes and never trigger database migration checks.

## Local Installation & Verification

From `services/agent`:

### 1. Environment Setup

```powershell
rtk proxy uv venv --python 3.11 .venv
rtk proxy uv pip install --python .venv/Scripts/python.exe -r requirements-postgres.lock -r requirements-groq.lock
rtk proxy uv pip install --python .venv/Scripts/python.exe --no-deps -e .
```

### 2. Running Verification Against Local Disposable PostgreSQL

Tests require a running PostgreSQL 16 server on localhost:

```powershell
$env:BS_POSTGRES_TEST_URL = "postgresql://postgres@127.0.0.1:54339/postgres"
rtk proxy .venv/Scripts/python.exe -m pytest tests/test_hosted_config.py -v
rtk proxy .venv/Scripts/python.exe -m pytest tests/test_hosted_runtime.py -v
rtk proxy .venv/Scripts/python.exe -m pytest tests -v
rtk proxy .venv/Scripts/python.exe -m ruff check src tests scripts
rtk proxy .venv/Scripts/python.exe -m ruff format --check src tests scripts
rtk proxy .venv/Scripts/python.exe -m mypy src tests scripts
rtk proxy uv pip check --python .venv/Scripts/python.exe
```

Test fixtures use `disposable_database()` and `migrated_database()` from `tests/postgres_support.py` to create isolated `bs013_test_<uuid>` databases and drop them immediately upon test completion.

## Verified HTTP Workflows & Concurrency Guarantees

1. **Structured Human Workflow Lifecycle**:
   - `POST /api/workspaces` issues `bs_session` cookie with `Secure`, `HttpOnly`, `SameSite=lax`.
   - `POST /api/requests` creates borrower request with UTC timestamps.
   - `POST /api/reservations` enforces human approval (`human_approved: true`); `human_approved: false` yields `422 APPROVAL_REQUIRED`.
   - `POST /api/loans/{id}/pickup` transitions item to `ON_LOAN`.
   - `POST /api/loans/{id}/return` transitions item to `AWAITING_INSPECTION`.
   - `POST /api/equipment/{id}/inspection` transitions item to `QUARANTINED` or `AVAILABLE`.
   - `GET /api/snapshot` returns full workspace state.

2. **Session Isolation & Foreign Key Protection**:
   - Requests without session cookies return `401 SESSION_REQUIRED`.
   - Attempting to reserve an item from another workspace fails with `404 NOT_FOUND`.

3. **Persistence & Exact Idempotency Replay**:
   - Across distinct application instances connected to the same database, previous state persists and requests with identical `Idempotency-Key` and payload return the stored byte-for-byte response.
   - Replays with identical key but mismatched payload return `409 IDEMPOTENCY_CONFLICT`.

4. **Concurrency Proofs**:
   - Two concurrent HTTP clients attempting to allocate the same equipment item: deterministic barrier ensures concurrent arrival; PostgreSQL row locks guarantee exactly one client receives `200` and the other receives `409 STATE_CONFLICT`. Exactly one loan row is written.
   - Two concurrent identical idempotent mutation requests: barrier synchronization produces `200` for both with byte-identical responses and exactly one underlying database record.

## Remaining M3 Gates

This milestone delivers the locally verified hosted-mode FastAPI runtime. The following activities remain future gates:
- Neon cloud PostgreSQL database provisioning and migration deployment.
- Vercel frontend deployment and HTTPS platform-hostname routing; no custom domain purchase required.
- GitHub Actions cron scheduler for autonomous background task processing.
- Groq cloud provider live inference admission and E2E verification.
