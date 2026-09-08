# M2A integrated checkout verification — 2026-09-08

Codex integrated accepted backend285df0f (implementation9db2d8c, R13 proof) and frontend e4ba9f4 into integration/m1 from659dd12. Production source is unchanged from those accepted versions. Worker reports are retained; these current integration checkpoints supersede their historical status. Frontend LF checkout attributes prevent Windows line endings from failing Prettier.

## Checks passed
- Backend331 tests, Ruff lint/format and strict mypy over the canonical source. Used the existing R11 Python environment with explicit canonical PYTHONPATH; no dependency change.
- Frontend87 tests, ESLint, Prettier, TypeScript/build, and six browser checks. The frontend assistant browser tests use labeled fixtures, not real inference.
- Real Edge152 -> installed Vite -> canonical FastAPI -> fresh SQLite, without interception: disabled interpretation returned503 ASSISTANT_DISABLED, snapshot unchanged; manual request/reservation/pickup/return/inspection completed with CLOSED request/loan and five events. Reload persisted state, a fresh browser context received401, 390px width had no overflow, and no page errors occurred. Own server and browser stopped.
- Backend R13's six successful real model cases remain evidence for the identical backend source; they are not a claim of browser-driven inference in this integration run.

Integration evidence in program-control reviews: M2A-disabled-integration.mjs, M2A-disabled-integration-output.txt, M2A-disabled-integration-server.log, M2A-disabled-real-desktop.png and M2A-disabled-real-mobile.png. Disposable synthetic DB retained outside the repo. Current run made zero inference calls; ledger remains cumulative28, incomplete0, all allocations closed.

## Acceptance limits and next gate
Accepted local source integration, disabled-provider behavior and preserved business workflow. Real enabled browser interpretation -> deliberate Use Draft -> separate request submission remains open. A new bounded allocation is needed; do not reuse any old probe allowance. M2B durable tasks, public persistent hosting/provider availability, release security, videos and final audits remain open. No public deployment, AgentCore claim, spend or submission change.
