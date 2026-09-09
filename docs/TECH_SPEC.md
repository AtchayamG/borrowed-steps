# Technical specification
Current amendments: M2A_CONTRACT.md and dated amendments describe the locally accepted intake slice; M2B_CONTRACT.md is the next frozen implementation target. The M1 statements below are historical baseline specifications, not current health/agent-mode claims. Hosting remains an independent gate.
M1 implements frozen API contract exactly.
Backend: FastAPI/Pydantic boundary, stdlib domain/sqlite3 persistence, reproducible project-local Python venv/dependencies. Minimal FastAPI/uvicorn/httpx/pytest/Ruff/mypy tooling as needed; no Strands model calls in M1.
Frontend: React/ReactDOM, strict TypeScript/Vite/CSS, Vitest/Testing Library and focused browser checks. Pin compatible versions for Node 22.22.3, commit package-lock. No root workspace.
Relative /api requests; Vite proxy http://127.0.0.1:8000. No runtime fixture fallback.
Health explicitly declares M1 and agent_mode not_implemented.
Verify valid/invalid transitions, reopen file DB, parallel allocation race, rollback, stale versions, idempotency replay/conflict, cookie isolation/origin. Frontend verifies forms, confirmation, failures/retry, loading/empty/keyboard states using test-only contract mocks.
Later gates add real Strands, background due processing, public hosting, deployment E2E and video/submission evidence.
