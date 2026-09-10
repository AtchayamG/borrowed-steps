# Scheduler HTTP packaging decision: BS-016

Codex ASTRA_HIGH decision2026-09-10 after accepted BS-014. This is bounded local
implementation authorization, not deployment or scheduler activation. Read with
M3_OPERATIONS_CONTRACT.md; no inference-admission scope is added.

## Resolve cold-start authentication ordering

BS-013 performs a read-only schema check during hosted factory construction.
That cannot meet authentication-before-database for an unauthorized cold start.
For BS-016, hosted create_app must construct adapters without database I/O and
must not check schema in lifespan. Local SQLite startup behavior stays unchanged.

Use a synchronous FastAPI request dependency for the hosted gates, so blocking
SQL stays off the async event loop. Normal business API endpoints check the
expected schema before their first database operation. Do not migrate, cache
readiness indefinitely, or start a background connection/pool. Health remains
pure liveness and is explicitly not database or scheduler health. OpenAPI/docs
and unmatched routes need no database work. This deliberately changes hosted
wrong-schema failure from factory-time to a generic request503.

For the two exact operational routes below, authenticate first, then run their
bounded scheduler service readiness/read/tick methods. Do not also apply normal
Store readiness to them: its longer timeout would invalidate the tick envelope.
Instrument psycopg.connect from before app construction through cold request
handling to prove missing/bad operational auth causes ZERO database connections.
No merely warm-app test or fake dependency-order assertion substitutes for this.

## Private operations

Add private Settings.task_tick_token, repr=False, loaded only from the explicitly
named BS_TASK_TICK_TOKEN. DefaultNone disables operational routes (generic503,
no database work); this does not disable structured human routes. If supplied,
require32..256 ASCII URL-safe characters [A-Za-z0-9_-], with no trimming or echo
in errors. Local mode does not expose operational routes.

- POST /api/internal/tasks/tick: exactly one Authorization header containing
  `Bearer <token>`. Compare token bytes with hmac.compare_digest. Missing/wrong,
  malformed, duplicate or oversized auth yields generic401 before database work;
  cookies, Origin, query/body token or workspace parameters are not substitutes.
  There are no request-controlled limits or scopes. Call the accepted tick with
  its default100 candidates,20s budget and60s scheduler lease. Ignore body/query
  parameters entirely; never log them or turn them into scheduler arguments.
- GET /api/internal/tasks/status: same authentication, bounded read-only schema
  check then status read. It never runs tasks. Return only SchedulerStatus.to_dict
  fields; no run IDs, database URLs, borrower/workspace/session data.

Omit both from OpenAPI and public UI. No redirects or automatic retries in the
workflow. Return200 for success/partial (partial stays explicit),409 for busy or
stale_lease,503 for failed/unavailable or evidence-write failure. The tick JSON
contains outcome, capacity_limited and report counters or null; omit run_id and
driver/error details. Set Cache-Control:no-store on all operational responses.
Status read errors are generic503, never a fabricated healthy status. Existing
human workflow Origin/session/idempotency gates and response bytes are preserved.

## Inactive workflow artifact only

Place a YAML EXAMPLE under docs/workflows/hosted-tick.yml.example, not .github.
Schedule7,22,37,52 minutes each UTC hour plus workflow_dispatch only; standard
Linux runner; permissions:{}; serialize a fixed concurrency group with
cancel-in-progress:false. Gate job with repository variable
BS_SCHEDULER_ENABLED == 'true'; default/unset means no run. No checkout/actions,
package installs, inference, pull-request trigger or response-body logging.
Use HTTPS URL and bearer secret from step environment, never direct expression
interpolation in shell code; validate URL scheme and fail closed for missing
values. curl connect timeout10s, max-time55s, no retries/redirect following, no
verbose mode, discard response body, fail for non2xx. Job timeout2min. Example
values are placeholders, not live account settings or an active schedule.

## Acceptance

Real disposable PostgreSQL tests: authenticated tick and fresh-app status;
duplicate/busy and partial outcomes; no duplicate notices; schema0/future refusal;
evidence-write failure never returns success. Auth cold-start zero-connect tests
also cover duplicate headers, cookie-only auth and missing configuration.
Prove operational requests cannot increase candidate limits or choose a workspace.
Confirm no protected paths in OpenAPI, no hosted SQLite/TaskRunner/provider import,
and all522 accepted backend tests plus new tests pass. Preserve their assertions
except intentional factory-to-request schema-gate expectations documented above.
Static validation of inactive workflow: schedule/gate/no PR/no retries/secret
handling. Do not activate it to test. Live hosting, cost, scheduling availability,
admission, final release and video gates remain unaccepted.
