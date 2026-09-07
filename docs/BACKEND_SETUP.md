# Backend setup — services/agent (Borrowed Steps M1)

Local, persisted M1 API. No cloud service, no provider credential, no model call
takes part in any request. `agent_mode` is `not_implemented` and stays that way
until the M2 Strands gate.

## Requirements

- Windows 10/11 with Python 3.11 or newer on `PATH` (verified on 3.11.15).
- No database server: persistence is one SQLite file created on first start.
- No network access at runtime. Only the one-off dependency install reaches
  PyPI, the official registry.

## Install (Windows, PowerShell)

Run everything from `services/agent`. On a signed-script policy, start the shell
as `powershell -NoProfile -ExecutionPolicy Bypass`; do not change the machine
policy.

```powershell
cd services\agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

`pip install -e ".[dev]"` installs the package in editable mode so
`borrowed_steps` is importable from any working directory, and adds the check
tooling.

### Pinned dependencies

Direct dependencies are pinned exactly in `pyproject.toml`:

| Scope | Pin |
|---|---|
| runtime | `fastapi==0.141.1`, `pydantic==2.13.5`, `uvicorn==0.52.4` |
| dev | `httpx==0.28.1`, `mypy==2.3.1`, `pytest==9.1.1`, `ruff==0.16.6` |

`requirements.lock` is the full resolved set, including transitive packages, as
produced by `pip freeze --exclude-editable`. To reproduce that exact environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
```

## Start the server

The ASGI module path is **`borrowed_steps.main:app`** (`src/borrowed_steps/main.py`,
which calls `create_app()` once at import).

```powershell
.\.venv\Scripts\python.exe -m uvicorn borrowed_steps.main:app --host 127.0.0.1 --port 8000
```

M1 binds loopback only. TLS, abuse limits and a `Secure` cookie are release
gates handled at M3, not here.

## Configuration

All optional, all environment variables, none of them a secret.

| Variable | Default | Meaning |
|---|---|---|
| `BS_DB_PATH` | `data/borrowed_steps.db` | SQLite file, relative to the working directory. Parent directories are created. |
| `BS_ALLOWED_ORIGINS` | `http://127.0.0.1:5173,http://localhost:5173` | Comma-separated origins accepted on mutations. Both loopback spellings of the Vite dev server are allowed by default. |
| `BS_COOKIE_SECURE` | `false` | Sets `Secure` on the session cookie. Turn on only when serving over HTTPS. |

There is no credential, API key or provider setting, because nothing outside the
process is contacted.

## Migration and initialisation behaviour

- `create_app()` opens the SQLite file and runs migrations before serving.
- Migrations are a numbered list applied inside one `BEGIN IMMEDIATE`
  transaction and recorded in `schema_migrations`. Re-running is a no-op, and two
  processes starting at once are serialised by the busy timeout.
- The file uses WAL, `PRAGMA foreign_keys = ON` and a 5 s busy timeout on every
  connection.
- A partial unique index (`ux_loans_active_equipment`) makes a second `RESERVED`
  or `ON_LOAN` loan on one item impossible at the storage level.
- Nothing is seeded globally. Equipment is seeded per workspace by
  `POST /api/workspaces`.
- Deleting the database file resets everything; there is no other state.

## Checks

```powershell
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m mypy src tests scripts
.\.venv\Scripts\python.exe -m pytest -q
```

`mypy` runs in `strict` mode with `warn_unreachable`, over the package, the tests
and the smoke script.

Per `AGENTS.md`, prefix shell commands with `rtk` when running them by hand, for
example `rtk .\.venv\Scripts\python.exe -m pytest -q`.

## Live smoke

With the server running on `127.0.0.1:8000`:

```powershell
.\.venv\Scripts\python.exe scripts\smoke.py http://127.0.0.1:8000
```

`scripts/smoke.py` uses only the standard library and drives the real HTTP
surface: health, workspace creation, request, reservation, an idempotent replay
checked for identical raw bytes and content type, pickup, return, inspection, a
refused stale version, and the event history. Its `due_at` is computed as now
plus seven days, so the smoke does not expire. It exits non-zero on the first
mismatch and makes no external call.

Use a disposable database and port when another service may be running:

```powershell
$env:BS_DB_PATH = "$env:TEMP\bs-smoke\bs.db"
.\.venv\Scripts\python.exe -m uvicorn borrowed_steps.main:app --host 127.0.0.1 --port 8123
.\.venv\Scripts\python.exe scripts\smoke.py http://127.0.0.1:8123
```

## Layout

```
services/agent/
  pyproject.toml            packaging, pins, ruff/mypy/pytest configuration
  requirements.lock         fully resolved dependency set
  scripts/smoke.py          stdlib HTTP smoke against a running server
  src/borrowed_steps/
    domain/                 entities, enums and loan rules; standard library only
    application/            ports and use cases; depends only on the domain
    infrastructure/         sqlite3 store, system clock and id generator
    interfaces/http/        FastAPI adapter and strict request schemas
    config.py               environment settings
    isotime.py              UTC ISO-8601 helpers (leaf module)
    main.py                 ASGI entry point: borrowed_steps.main:app
  tests/                    domain, HTTP, persistence, idempotency, concurrency
```

Dependencies point inward only: `domain <- application <- interfaces/infrastructure`.
The domain imports nothing from FastAPI, Pydantic, sqlite3, Strands or any
provider.

## Contract notes for integration

Two details the frozen contract leaves open, recorded here so the frontend and
Codex can rely on them. Both are flagged in `docs/workers/BS-001.md` for review.

1. **Error codes for 401/403/404/500.** Accepted by Codex in the BS-001 review.
   The contract fixes the envelope `{"error":{"code","message"}}` and names
   `VALIDATION_ERROR`, `APPROVAL_REQUIRED`, `STATE_CONFLICT` and
   `IDEMPOTENCY_CONFLICT`. The remaining statuses use `SESSION_REQUIRED` (401),
   `ORIGIN_FORBIDDEN` (403) and `NOT_FOUND` (404). Any unexpected failure returns
   exactly
   `{"error":{"code":"INTERNAL_ERROR","message":"The request could not be completed."}}`
   with HTTP 500 and `Content-Type: application/json`. No exception type,
   message or traceback ever reaches the client; the detail goes to the
   `borrowed_steps.http` logger instead, and the transaction has already rolled
   back by then.
2. **`due_at` offsets.** Accepted by Codex in the BS-001 review. Timestamps must
   be strings with an explicit offset. A naive timestamp and a bare epoch number
   are both rejected. A non-UTC offset such as `+05:30` is accepted and
   normalised, so every stored and served timestamp is UTC with whole-second
   precision, `YYYY-MM-DDTHH:MM:SSZ`.

3. **`POST /api/workspaces` requires an empty JSON object.** `{}` returns 201. A
   missing body, `null`, an array, a bare scalar or any unknown field is refused
   with 422 before a workspace, session or equipment row is created.

4. **Mutation responses are byte-stable.** Every successful mutation is
   serialised once, stored with its effect in the same transaction, and served
   from that stored text. The first response and every idempotent replay are
   identical byte for byte, including after a process restart. Keys are in
   canonical sorted order and non-ASCII text is served as UTF-8 rather than
   escaped; neither is contract-significant, but both are stable.

`entity_type` on an event is `REQUEST` for `REQUEST_CREATED`, `LOAN` for
`RESERVED`, `PICKED_UP` and `RETURNED`, and `EQUIPMENT` for the three
`INSPECTED_*` actions.
