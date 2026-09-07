# Architecture — BS-000 accepted by Codex 2026-09-07
M2A amendment: docs/M2A_CONTRACT.md freezes a read-only RequestInterpreter with a real local Strands/Ollama adapter. Default disabled; explicit human draft review; no queue/persistence for suggestions. M2B persisted pickup/due processing and public provider/hosting remain separate gates.
One Python REST service plus React SPA, SQLite for M1 local persistence. Backend later can serve built SPA, avoiding unnecessary service boundaries.
Python 3.11+, FastAPI boundary, stdlib domain dataclasses/enums, application use cases/ports, sqlite3 adapter. React strict TypeScript/Vite/CSS/native controls, no component/state framework without need.
Python Strands SDK in M2: supports local Ollama as well as hosted providers. Provider/hosting decision is gated on verified ₹0 access; no AWS account required for local domain/UI work.
No hosting selected or .openai/hosting.json. Public release requires persistent disk/database, TLS, session isolation, bounded abuse/inference costs and availability through judging.
SQLite public deployment only with verified persistent storage/supported concurrency; otherwise approve a persistence ADR first.

## Ownership
Claude BS-001: services/agent (src/borrowed_steps/domain, application, interfaces, infrastructure; tests/config).
AGY BS-002: apps/web (React, HTTP adapter, UI/tests/npm config).
Codex: frozen docs and API contract. Independent manifests/locks; no root npm workspace.
Each worker writes its own docs/workers/<TASK_ID>.md and setup guide. Four checkpoint files are isolated by worktree; Codex reconciles them during integration.

## Boundaries
domain <- application <- interfaces/infrastructure.
Domain no FastAPI/Pydantic/SQLite/Strands/DB/provider imports. Application transaction/workspace context; outer adapters map framework types.
Small WorkspaceStore/unit-of-work port; clocks/ID seams only as needed for tests. RequestInterpreter port in M2; no speculative empty layers.
One item has at most one open RESERVED/ON_LOAN loan; enforce partial unique index plus transactional rule.
Atomic reservation checks request, matching kind, AVAILABLE state/version. Concurrent winner commits; loser 409. Failed mutations roll back everything.
Every write scoped to server session, not client workspace ID; no cross-workspace data access. Transaction updates state/version/event/idempotency response together.
SQLite foreign keys, WAL, busy timeout; explicit file config and restart persistence.

## Trust
Synthetic-only evaluation. Validate enums/lengths/dates/relations. Stable error codes, no traces/secrets.
M1 bind localhost. HttpOnly SameSite=Lax opaque cookie, 24h expiry; Secure on HTTPS at release. Origin protection, Vite same-origin proxy.
Final public release additionally needs abuse/session/TLS/cost controls and dedicated security review.
Strands sees bounded task/context; tools call deterministic application policies with server-bound workspace. Model cannot approve allocation, pick conflict winner, release quarantine or run arbitrary code/network.
No silent fixture fallback in live mode. Production trace plus actual inference required for final Strands claim.
