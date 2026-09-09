# M2B local integration acceptance - 2026-09-09
STATUS: LOCAL_ACCEPTED. Not public-release or submission acceptance.
Backend worker6ad75bc plus reviewer test-only b371b7c; frontend accepted5ce9611. Imported implementation paths match those commits exactly. Shared checkpoint docs reconciled explicitly; historical canonical frontend evidence retained, only new M2B fixture images imported.

## Independent checks
Canonical382 backend tests PASS, Ruff lint and strict mypy PASS; worker baseline Ruff format PASS. Canonical108 frontend tests, production build and format PASS; identical frontend source previously passed lint/typecheck and seven fixture browser checks at5ce9611.
Two backend reviewer tests add injected event-write rollback and a real pickup interleaved between discovery and claim. No production backend changes or new dependencies.

## Real combined proof
Evidence: services/agent/test-evidence/m2b-integration/result.json, server.log, desktop.png, mobile.png. Captured from real Edge -> installed Vite -> FastAPI -> SQLite, with no interception. Runner default30 seconds, actual wall-clock loan deadline, assistant explicitly disabled. This proof made zero interpretation calls.
1. Human UI created request and confirmed reservation.
2. Closed the entire browser; database-only reads observed PICKUP_DUE, exactly one due event.
3. Reopened browser with its session cookie; UI confirmed pickup, resolving pickup and creating return notice.
4. Closed browser again; database-only reads observed RETURN_DUE at the real deadline.
5. Gracefully stopped and restarted backend against the same database. Tasks/events/loans were unchanged across restart, no duplicate notices.
6. Reopened UI; human confirmed return and inspection outcome QUARANTINED. Loan/request closed, two tasks RESOLVED, seven total lifecycle/due events.
7. Reload returned identical snapshot; fresh browser context received401;390px layout had no horizontal overflow and no page errors.
Own browser/Vite/backend stopped. Server logs show completed shutdown. Full script and retained synthetic DB are in program-control reviews/M2B-real-integration.mjs and M2B-real-*.db. Published evidence omits the machine-specific DB directory.

## Scope and next gate
M2A's earlier real Strands proof remains historical evidence for its unchanged adapter; this test does not claim enabled inference on M2B or production. All29 historical calls/allocations remain closed.
No public hosting, cloud spend, push or submission. M3 must establish actual no-cost hosting/domain/provider entitlement, persistent deployment, session/abuse protections, availability through judging and fresh public E2E. AWS account status remains distinct from Builder ID. LumaLoad video and MAX audits remain later. Benchbook/Schoolbag follow flagship completion.
