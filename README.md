# Borrowed Steps

A community mobility-equipment lending workspace. Volunteers register needs, allocate an available item, confirm pickup and return, and record an inspection before the item circulates again.

**Status: hosted release candidate live and verified.** The public deployment is [borrowed-steps.vercel.app](https://borrowed-steps.vercel.app) and uses React 18, FastAPI, Neon Free PostgreSQL and Groq Free. The optional Strands/Groq assistant creates advisory intake drafts; the deterministic structured workflow remains available when inference is disabled. Background coordination records in-app pickup and return reminders; it sends no email or SMS.

The hosted package is designed for free Vercel Hobby + Neon Free + Groq Free operation independently of the developer's computer. Hosted mode requires explicit configuration (`BS_RUNTIME=hosted`, `BS_STORE=postgres`, `BS_DATABASE_URL`, `BS_ALLOWED_ORIGINS`, `BS_COOKIE_SECURE=1`, and `BS_TASKS_ENABLED=0`). Set `BS_ASSISTANT_ENABLED=1` and `BS_GROQ_API_KEY` only when the real Groq adapter is available. See [hosted setup and verification](docs/HOSTED_PACKAGE_SETUP.md) and [hosted acceptance](docs/BS-028_ACCEPTANCE.md) for the verified live workflow.

## Human decisions

- Equipment kinds are `WHEELCHAIR`, `WALKER` and `CRUTCHES`.
- A draft never creates a request automatically. A volunteer reviews fields and submits the request.
- Allocation, pickup, return and inspection require explicit human confirmation.
- Inspection outcomes are `AVAILABLE`, `REPAIR` or `QUARANTINED`. A later human inspection can change the outcome.
- Transactions and a database uniqueness constraint prevent competing active allocations. Retries use idempotency keys; uncertain responses must not silently become new operations.

## Local architecture

![Current local runtime](architecture/diagrams/current_local_architecture.svg)

See [architecture boundaries and Mermaid source](architecture/diagrams/README.md), [API contract](docs/API_CONTRACT.md), and [accepted local integration evidence](docs/M2B_INTEGRATION.md).

## Run locally (PowerShell)

Verified development versions: Python 3.11.15 and Node.js 22.22.3. Start each terminal at the repository root. Installation requires package downloads; the structured workflow requires no model service.

Backend terminal:

```powershell
cd services/agent
rtk proxy python -m venv .venv
rtk proxy ./.venv/Scripts/python.exe -m pip install -r requirements.lock
rtk proxy ./.venv/Scripts/python.exe -m pip install -e . --no-deps
$env:BS_ASSISTANT_ENABLED = "false"
$env:BS_TASKS_ENABLED = "true"
rtk proxy ./.venv/Scripts/python.exe -m uvicorn borrowed_steps.main:app --host 127.0.0.1 --port 8000
```

Frontend terminal:

```powershell
cd apps/web
rtk proxy npm ci
rtk proxy npm run dev -- --host 127.0.0.1
```

Open [the local app](http://127.0.0.1:5173). Click **Start Synthetic Workspace** when prompted. This deliberately creates an isolated workspace; an initial missing-session response is expected. Use synthetic borrower labels only. Follow the [local verification guide](docs/LOCAL_VERIFICATION_GUIDE.md).

Configuration defaults:

| Variable | Default / purpose |
| --- | --- |
| `BS_DATABASE_URL` | Hosted PostgreSQL URL; requires TLS and an explicit public host |
| `BS_DB_PATH` | Local-only SQLite path, `data/borrowed_steps.db` |
| `BS_ALLOWED_ORIGINS` | `http://127.0.0.1:5173,http://localhost:5173`; mutation Origin allowlist |
| `BS_COOKIE_SECURE` | `false` for local HTTP |
| `BS_TASKS_ENABLED` | `true`; owned background coordination thread |
| `BS_ASSISTANT_ENABLED` | `false`; structured workflow remains available |
| `BS_ASSISTANT_HOST` | `http://127.0.0.1:11434`; pinned local assistant host |
| `BS_ASSISTANT_MODEL` | `openai/gpt-oss-20b` in hosted Groq mode; `llama3.2:3b` locally |

The assistant is optional and its successful output must include verified provider and tool provenance. Hosted enablement never silently falls back to fixtures. The local acceptance record documents the exact test evidence; public provider evidence is recorded separately after deployment. No paid service is required.

## Checks and evidence

From `services/agent`, using the installed locked environment:

```powershell
rtk proxy ./.venv/Scripts/python.exe -m pytest -q
rtk proxy ./.venv/Scripts/python.exe -m ruff check .
rtk proxy ./.venv/Scripts/python.exe -m ruff format --check .
rtk proxy ./.venv/Scripts/python.exe -m mypy src tests scripts
```

From `apps/web`:

```powershell
rtk proxy npm test
rtk proxy npm run lint
rtk proxy npm run format:check
rtk proxy npm run typecheck
rtk proxy npm run build
```

The accepted M2B run recorded 382 backend tests and 108 frontend tests (12 suites), plus real browser/API/SQLite persistence checks. These counts describe that recorded run; this documentation update did not rerun application tests. Fixture browser tests do not establish live inference or public deployment.

Further setup: [backend](docs/BACKEND_SETUP.md), [frontend](docs/FRONTEND_SETUP.md). Acceptance: [backend review](docs/BS-007_ACCEPTANCE.md), [frontend review](docs/BS-008_ACCEPTANCE.md), [combined local proof](docs/M2B_INTEGRATION.md).
