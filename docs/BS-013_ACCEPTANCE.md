# BS-013 accepted locally after direct Codex correction

Worker return: 6e50b39, base ae2c517. Codex reviewed at user-confirmed
ASTRA_MEDIUM. This accepts the hosted-mode API foundation locally, not public
deployment, complete M3, scheduler operation or inference availability.

The factory selects PostgreSQL only for explicit hosted configuration, checks
schema read-only before serving, and preserves local SQLite behavior. Hosted
assistant and TaskRunner remain disabled. Existing human approvals, workspace
isolation, exact replay and transactional use cases are retained.

Codex corrected reflected configuration values and chained parser errors,
malformed/legacy-numeric origin hosts, and unrecognized hosted boolean flags.
Hosted direct Settings now requires actual boolean values. Test time is fixed
so the new HTTP cases will not expire with their example due dates. Verification
now asserts one idempotent event, unchanged state after disabled interpretation,
no provider/TaskRunner construction, health without database access, and local
factory operation with PostgreSQL/provider imports blocked in a fresh process.
Setup documentation uses platform-hostname hosting, not a required custom domain.

Independent checks on corrected source:

- `python -m pytest -q`: **493 passed, zero failed/skipped**, two inherited
  Starlette deprecation warnings, 62.00 seconds. PostgreSQL 16.10 was running on
  Codex's owned loopback cluster; all database-dependent cases executed.
- Focused hosted suite: 62 passed (56 configuration/import cases and six real
  PostgreSQL HTTP cases). Full suite includes the unchanged 431-test baseline.
- Ruff lint and format: PASS, 71 files. Strict mypy: PASS, 71 files.
- `uv pip check`: PASS, 72 compatible packages. Git whitespace/ownership checks pass.

Per-test databases were verified absent after execution; Codex's server stopped.
No cloud/model calls, spending, public push or deployment. All29 historical
model allocations remain closed. Original worker evidence remains historical;
this record supersedes its 477-test count and broad verification claims.

The next architecture gate is bounded scheduler integration and shared global
inference admission. No M3 UI/browser, account entitlement, actual scheduled
invocation or hosted provider acceptance has occurred. Keep assistant and
scheduler disabled until their respective gates pass.
