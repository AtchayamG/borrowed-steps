# PostgreSQL foundation (BS-011)

This adapter implements the existing Store and WorkspaceUnitOfWork ports. The
application factory still uses SQLite: installing this overlay does not enable
hosted mode, change routes, run migrations or create a cloud database.

## Install and test locally

From `services/agent`, use an isolated Python 3.11 environment:

```powershell
rtk proxy uv venv --python 3.11 .venv
rtk proxy uv pip install --python .venv/Scripts/python.exe -r requirements-postgres.lock -r requirements-groq.lock
rtk proxy uv pip install --python .venv/Scripts/python.exe --no-deps -e .
```

The PostgreSQL overlay pins `psycopg==3.2.10` and `psycopg-binary==3.2.10`
alongside the existing base lock. The Groq overlay is needed to collect the
already accepted offline Groq regression tests; none calls the provider.
The root manifests remain unchanged pending hosted integration.

Set `BS_POSTGRES_TEST_URL` privately in the test process environment to an
explicit PostgreSQL URI for a **disposable loopback server**. Its test role must
be able to create/drop databases. The fixture creates a random
`bs011_test_<uuid>` database per case and drops only that database in `finally`;
it never resets or drops the database named by the supplied URI. Never point
this test variable at a production server or share its contents in evidence.

```powershell
rtk proxy .venv/Scripts/python.exe -m pytest -q tests/test_postgres_store.py
rtk proxy .venv/Scripts/python.exe -m pytest -q
rtk proxy .venv/Scripts/python.exe -m ruff check src tests scripts
rtk proxy .venv/Scripts/python.exe -m ruff format --check src tests scripts
rtk proxy .venv/Scripts/python.exe -m mypy src tests scripts
rtk proxy uv pip check --python .venv/Scripts/python.exe
```

Without that test variable, database-dependent tests explicitly skip: that is
not PostgreSQL acceptance. URL rejection checks do not need a server.
Acceptance used PostgreSQL 16.10, Python 3.11.15 and synthetic data on a separate
loopback cluster inside the Codex takeover worktree. It did not use Neon,
Docker, a public tunnel or model inference. The test service is not deployment.

## Explicit schema management

Put the intended **direct admin** connection URI in `BS_POSTGRES_ADMIN_URL`
through the environment; do not pass it as a command argument or commit it.
Run from `services/agent` with the project installed, or set `PYTHONPATH=src`:

```powershell
rtk proxy .venv/Scripts/python.exe scripts/postgres_migrate.py
```

The CLI prints only the schema version or a generic failure. Version 1 creates
the full M1/M2B schema without seeding application data. An advisory transaction
lock serializes concurrent migration attempts. DDL and the version record
commit together; failure rolls them back. Repeated runs preserve data. Future
versions append migration entries; a database newer than the build is refused.
No existing SQLite data is copied by this command.

Constructing `PostgresStore(database_url)` performs validation only. Explicit
`store.check_schema()` is read-only and requires version 1. Integration must call
readiness separately and must not run migrations during startup or requests.

URIs must have an explicit host. Outside `127.0.0.1`, `localhost` and `::1`,
`sslmode=verify-full` is required, with an appropriate trusted CA configured
for libpq. Only `sslmode`, `sslrootcert` and `channel_binding` URI query options
are accepted. Routing overrides such as `host` or `hostaddr` in the query are
refused. Keep the host/process environment operator-controlled. Credentials
are never included in application or CLI database failure messages.

## Transaction behavior

- Every write UoW locks its workspace row before reading lifecycle/idempotency
  state. Independent workspaces can write concurrently.
- Read UoWs use a repeatable, read-only snapshot. Each connection closes at the
  end of the operation, including failure and lock timeout; there is no pool.
- Application connections bound connect/lock/statement waits to 15/10/15
  seconds. Migration connections use 15/30/60 seconds. These are individual
  operation bounds, not an HTTP request deadline.
- Prepared statements are disabled conservatively for transaction poolers.
  SQL uses bound values; schema SQL is static.
- Composite foreign keys enforce workspace containment and a partial unique
  index prevents two active loans for the same equipment. Task claims and
  their audit events commit atomically. Discovery is ordered and bounded to
  at most 100 candidates; it does not claim with `SKIP LOCKED`.
- Idempotency response bodies are TEXT and returned unchanged. Timestamps
  round-trip as canonical whole-second UTC. Contention raises
  `StorageBusyError`; unrelated database failures remain generic hard errors.

## Remaining hosted gates

This is direct use-case and real local database proof, not hosted HTTP proof.
Production settings/factory wiring, roles/credentials, free-plan verification,
global inference admission, scheduler integration, hosted persistence and
public browser acceptance remain separate work under `M3_HOSTED_CONTRACT.md`.
No personal-computer public hosting, SQLite hosted fallback or cloud spend is
authorized by this setup document.
