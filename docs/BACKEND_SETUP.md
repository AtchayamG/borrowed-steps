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
| `BS_ASSISTANT_ENABLED` | `false` | Turns on the local M2A intake assistant. Off by default. |
| `BS_ASSISTANT_HOST` | `http://127.0.0.1:11434` | **Pinned.** Present only so a mismatch can be refused; see below. |
| `BS_ASSISTANT_MODEL` | `llama3.2:3b` | **Pinned.** Same. |

The endpoint and the model are fixed by the contract and cannot be changed. If
either variable is set to anything other than its pinned value, `load_settings`
raises `ValueError` and the service refuses to start, rather than quietly using
the substitute. Setting them to exactly the pinned value, or leaving them unset,
is fine. Tests inject an interpreter through `create_app` instead of moving the
provider.
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

## The M2A intake assistant

`docs/M2A_CONTRACT.md` adds one read-only route, `POST /api/intake/interpret`.
It turns free text into a suggested draft that a human edits and then submits
through the existing `POST /api/requests`. It creates no request, loan,
equipment change, event, idempotency record or draft row, and it is explicitly
exempt from `Idempotency-Key`.

### Disabled startup, which is the default

With `BS_ASSISTANT_ENABLED` unset or false:

- `health.agent_mode` and `snapshot.agent_mode` are `"disabled"`;
- `POST /api/intake/interpret` returns `503 ASSISTANT_DISABLED`;
- the whole structured workflow works exactly as in M1;
- **no provider SDK is imported at all**, so the service runs with none of the
  assistant dependencies installed.

`health.milestone` is `"M2B"` in both modes. It names the milestone this service
implements, not one that has been accepted. `agent_mode` reports configuration
only: it never claims the provider is reachable or that a call would succeed.

### Enabled startup

Requires the assistant extra and a local Ollama with the model already pulled.
Nothing here downloads a model or contacts a remote provider.

```powershell
# one-off, in addition to the base install
.\.venv\Scripts\python.exe -m pip install -e ".[assistant]"

# confirm the local provider and model are present
ollama list                       # expect llama3.2:3b
curl http://127.0.0.1:11434/api/tags

$env:BS_ASSISTANT_ENABLED = "true"
.\.venv\Scripts\python.exe -m uvicorn borrowed_steps.main:app --host 127.0.0.1 --port 8000
```

`agent_mode` then reports `"strands_ollama"`.

### Provider dependency

| Piece | Pin |
|---|---|
| Strands SDK | `strands-agents[ollama]==1.54.0` (extras `assistant` and `dev`) |
| Ollama client | `ollama==0.6.2`, resolved by that extra |
| Ollama server | installed separately, verified at 0.33.3 |
| Model | `llama3.2:3b`, already installed |

The adapter builds an explicit `OllamaModel(host=..., model_id=...)`. There is no
implicit Bedrock or default provider, no credential lookup and no remote host.

### Limits, fixed by the contract

| Bound | Value | On breach |
|---|---|---|
| Concurrent interpretations per process | 1, no queue | `429 ASSISTANT_BUSY` |
| Wall clock, route guard | 120 s | `504 ASSISTANT_TIMEOUT` |
| Wall clock, adapter's own hard deadline | 110 s | `504 ASSISTANT_TIMEOUT` |
| HTTP transport timeout to Ollama | 60 s | provider error, `503 ASSISTANT_UNAVAILABLE` |
| Model requests per invocation | 6 (SDK `Limits(turns=6)`) | `502 ASSISTANT_INVALID_OUTPUT` |
| `read_inventory` **attempts** per invocation | 2 | `502 ASSISTANT_INVALID_OUTPUT` |
| Successful `read_inventory` executions | 1 to 2 | `502 ASSISTANT_INVALID_OUTPUT` |
| Intake text, after trimming | 1 to 2000 characters | `422 VALIDATION_ERROR` |

A caller that times out does **not** free the concurrency slot. The slot stays
held until the owned run really ends, so a slow interpretation cannot be
overtaken by a second one.

Three separate things stop a stalled provider from holding that slot forever:

1. the ollama client is given a finite `timeout` through `ollama_client_args`.
   Left alone it defaults to `None`, meaning no timeout at all;
2. the adapter wraps its own run in `asyncio.timeout(110 s)`, which genuinely
   cancels the run at its next await point rather than only asking it to stop.
   The cooperative `cancel_signal` is still passed so the SDK can wind down
   cleanly first;
3. the route keeps its 120 s guard as an outer backstop.

Because the adapter's deadline is inside the route's, the run ends itself and
the slot is released. There is no orphaned background inference.

The tool budget is enforced on **attempts**, not only on successes. The SDK turns
an ordinary tool exception into a tool-error result and lets the model continue,
so a refused third `read_inventory` could otherwise be followed by valid
structured output and be reported as a success. A run that attempted more than
two reads fails as a whole.

### Owned provider lifecycle

`infrastructure/owned_ollama.py` subclasses the native `OllamaModel` and reuses
its public `format_request` / `format_chunk` helpers unchanged, so request and
response translation stays the SDK's. It adds exactly two things:

- **One owned client per interpretation.** The installed provider builds a fresh
  `ollama.AsyncClient` inside every `stream` and `structured_output` call and
  never closes it. Here one client is opened for the logical interpretation and
  closed through the client's public API on every exit: success, provider error,
  timeout, cancellation and budget refusal. The streaming response is closed the
  same way, including when a consumer abandons it part-way.
- **A counted request budget.** Every outbound chat request is charged *before*
  it is sent, across streaming, structured output and the recovery pass. The
  seventh request is refused without reaching the transport, and the refusal is
  sticky, so a layer that swallows it cannot turn the run into a success.
  `retry_strategy=None` disables implicit SDK retries; the explicit counter is
  the authority.

The installed SDK is not modified, monkeypatched or vendored wholesale, and no
private attribute of the client is touched. Adapted control-flow carries its
Apache-2.0 attribution in the module docstring.

Client arguments pin the destination: a finite timeout, `follow_redirects=False`
and `trust_env=False`, so no redirect, environment proxy or inherited credential
can move traffic off the loopback endpoint.

### The stage-two extraction prompt

Stage two sends the extraction rules **and the generated JSON schema itself** as
the system prompt, then the intake text alone as the user message. The schema is
serialised from `_Extraction.model_json_schema()` — the same call that builds the
request's native `format` parameter — so one schema is generated in one place and
there is no second copy to drift. Ollama's structured-output guidance asks for
the schema in the prompt as well as in `format`
(https://docs.ollama.com/capabilities/structured-outputs, checked 2026-09-08).

This is prompt construction only. `format`, `stream=False`, temperature 0, the
budgets, the grounding and every validation are unchanged, and nothing here
claims the model extracts more accurately as a result — that is a question for a
separately authorised live proof.

## Coordination tasks and the owned runner (M2B)

Two persisted in-app notices per loan, and one small thread that processes them.
Nothing is sent anywhere: there is no email, SMS, calendar or notification
provider in this service, and a notice is a record, not a message.

**Settings.** `BS_TASKS_ENABLED` defaults to **true**; set it to `false`, `0`,
`off` or `no` to start without the runner. It needs no provider, opens no socket
and costs nothing, which is why it is on by default. The tick interval (30 s),
the stop timeout (10 s) and the per-tick candidate limit (100) are fixed
constants in `config.py`, not environment variables.

**Migration.** Schema version 2 adds the `tasks` table with its enum checks,
`UNIQUE(loan_id, kind)`, a composite foreign key on `(workspace_id, loan_id)`
that makes cross-workspace rows impossible at rest, and indexes for due
discovery and snapshot reads. Version 1 is untouched and an existing database
keeps every row. The upgrade backfills once, and only for loans that are still
open: a `RESERVED` loan gets a pending pickup notice due at that loan's own
creation instant, an `ON_LOAN` loan gets a pending return notice due at the
loan's due date. `RETURNED` and `CLOSED` loans get nothing, and no due event is
written for a deadline that passed before this code existed. `created_at` on a
backfilled row is the migration instant, because that is when the row appeared.
Restarting creates no duplicates - the migration is gated by
`schema_migrations`, and the unique index is the durable guarantee behind that.

**Lifecycle.** Reservation opens a `PICKUP_DUE` notice due immediately - arrange
the pickup now, which is not a promised pickup time and not an overdue penalty.
Pickup resolves it and opens a `RETURN_DUE` notice carrying the loan's real due
instant. Return resolves that one. Inspection that closes a loan defensively
resolves anything still open. All of this happens **inside the same transaction**
as the state change, version bump, event and idempotency record, so an exact
replay creates nothing again and a refused or conflicting command rolls the
notice back with everything else. Tasks never change equipment, request or loan
state.

**Processing.** A tick asks for at most 100 pending tasks due at or before now -
equality counts as due - as identifiers only, then reopens each one's own
workspace transaction and re-reads everything there. It transitions
`PENDING -> DUE` with a conditional update and writes the due event in the same
transaction, only when the transition really succeeded. A task whose loan has
moved on resolves quietly, with no event: announcing a pickup that already
happened would tell the volunteer something untrue. A busy database rolls back,
is counted and logged as contention, and is retried on a later tick. Log lines
are counts only - no workspace, loan or borrower data, and no prompt text.

**Runner.** One `threading.Thread` owned by the FastAPI lifespan: an immediate
tick at startup, then an interruptible 30-second wait between ticks. Shutdown
sets its stop event and joins with a 10-second bound, off the event loop, and
reports honestly if the thread is still running - the daemon flag is never
offered as proof of cleanup. The provider drain runs in a `finally`, so a runner
that refuses to stop cannot cause inference cleanup to be skipped. An unexpected
tick error is logged and the next tick still runs.

**Smoke.** `scripts/m2b_smoke.py` starts a real uvicorn process on an unused
loopback port with `BS_ASSISTANT_ENABLED=false`, against a disposable database,
and drives the real HTTP routes. Between the reservation and the assertion it
makes **no HTTP call at all**, reading the SQLite file directly, so what moves
the tasks can only be the runner. It then asks the server to stop and requires
the lifespan to report a clean shutdown. It makes zero model calls. Run it from
`services/agent` and read the result in
`test-evidence/m2b/m2b-smoke.json`:

```
rtk .venv/Scripts/python.exe scripts/m2b_smoke.py
```

### Optional single recovery

If the first completed pass executed **zero** inventory tools, the same agent is
asked once more to call the tool and return the structured result. Both passes
share one client, one 110 s deadline, one six-request budget and one
two-attempt tool budget. There is no recovery after a timeout, a provider error,
malformed output, a budget breach or any successful tool execution, and no
broader retry loop. Nothing is ever synthesised: only real tool executions count,
and a run that still has none fails with 502.

### Diagnostics

Set `BS_LOG_LEVEL=INFO` to see one line per interpretation naming each field and
the reason code for its outcome — `accepted`, `absent_candidate`,
`evidence_not_in_source`, `value_evidence_mismatch`, `parse_failure`,
`window_failure`, `ambiguous_kinds` and so on — plus the real model-request and
tool-call counts. These lines never contain intake text, candidate values or
model reasoning. `uvicorn --log-level` alone is not enough: it configures only
the `uvicorn.*` loggers.

Error codes: `401 SESSION_REQUIRED`, `403 ORIGIN_FORBIDDEN`,
`422 VALIDATION_ERROR`, `429 ASSISTANT_BUSY`, `502 ASSISTANT_INVALID_OUTPUT`,
`503 ASSISTANT_DISABLED` / `ASSISTANT_UNAVAILABLE`, `504 ASSISTANT_TIMEOUT`,
`500 INTERNAL_ERROR`. Error bodies carry no draft, no provenance, no prompt and
no trace.

#### Live smoke, separately labelled

Ordinary `pytest` never runs inference. The one script that does:

```powershell
$env:BS_DB_PATH = "$env:TEMP/bs-assistant-bs.db"
$env:BS_ASSISTANT_ENABLED = "true"
.\.venv\Scripts\python.exe -m uvicorn borrowed_steps.main:app --host 127.0.0.1 --port 8218

# in another shell (requires exact task authorization)
$env:BS_LIVE_PROOF_AUTHORIZATION = "BS-003-R8-AGY"
.\.venv\Scripts\python.exe scripts/assistant_smoke.py http://127.0.0.1:8218 --label "r8-pass-1"
```

It drives real HTTP, charges every attempt to `services/agent/test-evidence/probe-ledger.jsonl`
before sending, enforces the cumulative ceiling of 22 attempts, checks provenance and real tool
execution, verifies read-only snapshot consistency, and writes `services/agent/test-evidence/assistant-live-proof-r8.json`.
Top-level `PASS` (exit code 0) strictly requires all six controlled cases passing on the same run.
Proper subsets finish `PARTIAL` (exit code 2) and never emit full acceptance proof; invalid selections
are rejected before client creation (exit code 1).

To see which fields grounding rejected (field names and reason codes only, never values or model
reasoning), set `BS_LOG_LEVEL=INFO`.

### Remaining gates

This slice is local proof, not a finished M2 and not a public release.

- Live proof on Case 1 returned post-grounding `null` for `equipment_kind` and `due_at`.
  Because grounding logs were not captured during R8, the exact cause (model omission vs.
  grounding rejection) is `UNKNOWN`. Result is recorded as `BLOCKED` in `assistant-live-proof-r8.json`.
- The clarification path (`null` plus `missing_fields`) safely prompts a human operator rather than hallucinating.
- M2B persisted pickup and due processing, and idempotent background work.
- Public deployment, TLS, abuse and session protections, bounded inference cost
  and availability through judging.
- A public release would need a provider decision and a security review; this
  configuration is single-process localhost development only.
