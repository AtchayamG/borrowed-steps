# M2B frozen contract - persisted coordination (2026-09-09)

Approved by Codex at user-confirmed ASTRA_HIGH. This is the implementation target for BS-007 (backend) and BS-008 (web), not a claim that M2B is already implemented. M2A remains accepted locally at f1e9428. All 29 historical live inference calls are spent and all allowances closed; these tasks authorize zero new inference.

## Scope and boundaries
Persist in-app pickup coordination and return reminders. Existing human-approved lifecycle routes complete tasks; there is no task action endpoint, acknowledgement, cancellation, email, SMS, calendar, notification provider, model call or second service. Domain uses stdlib types; application owns policy/ports; SQLite, threading and FastAPI stay in outer adapters. Preserve all M1/M2A rules, exact-byte idempotency, isolation, quarantine and explicit human approval.

## Public shape
Both GET /api/snapshot and POST /api/workspaces.snapshot add required tasks: CoordinationTask[]. Other responses retain existing shapes.
CoordinationTask has exactly:
{id, loan_id, kind, status, due_at, created_at}
All IDs are opaque strings. kind is PICKUP_DUE | RETURN_DUE. status is PENDING | DUE | RESOLVED. Timestamps are aware UTC ISO-8601 strings in the existing canonical format. Order tasks by created_at ascending then id ascending. Public objects never include workspace_id.
Fresh workspace has tasks: []. Snapshot agent_mode remains disabled | strands_ollama. After backend implementation, health.milestone is M2B; agent_mode still describes configuration, not successful inference.
New Event.action values PICKUP_DUE and RETURN_DUE have entity_type "loan", entity_id = loan ID, at = actual processing clock. Preserve existing event order. No task data or new events are invented by the client.

## Storage and atomic lifecycle
Add schema migration v2; never rewrite v1 or discard an existing database. tasks contains id primary key, workspace_id, loan_id, kind, status, due_at, created_at, with enum/check constraints, foreign keys and UNIQUE(loan_id, kind). Enforce workspace/loan consistency at the storage boundary. Index pending due discovery by status/due_at/id and workspace snapshot reads as needed for these actual queries.
Reservation creates one PENDING PICKUP_DUE task with due_at = created_at = current transaction clock. It means arrange pickup now, not an invented promised pickup time or overdue penalty.
Pickup resolves that pickup task and creates one PENDING RETURN_DUE task with due_at = loan.due_at, created_at = pickup transaction clock.
Return resolves the return task. Inspection that closes a loan defensively resolves any remaining non-resolved tasks for that loan.
These changes occur in the SAME transaction as the existing lifecycle state/version/event/idempotency record. Exact replay creates nothing again. Failed/conflicting commands roll back tasks too. Tasks never change inventory, loan or request states.
Backfill once during v2 upgrade: RESERVED loans get PENDING pickup tasks due at loan.created_at; ON_LOAN loans get PENDING return tasks due at loan.due_at. Record migration time as task.created_at; no fabricated historical due event. RETURNED/CLOSED loans create no tasks. A repeated migration/startup creates no duplicate. Upgrade existing workspaces as well as new ones.

## Due processing
An application operation uses an injected aware clock. Equality counts as due: PENDING and due_at <= tick time. Cross-workspace discovery is an internal scheduler capability, limited to at most 100 candidate task references per tick, ordered by due_at/id. It is not exposed through HTTP or passed to the model.
Reopen each candidate's server-bound workspace transaction and conditionally transition PENDING -> DUE only if still due and its loan is still the applicable RESERVED/ON_LOAN state. Write the corresponding due event in that transaction only when the transition succeeds. Recheck state inside the transaction: never rely on a stale discovery result. Stale inapplicable pending tasks resolve without a due event.
Use existing SQLite write serialization plus conditional update/row count. Concurrent runners cannot duplicate a due event. A rolled-back event/task write leaves both unchanged. A busy timeout may occur; roll back, record a bounded diagnostic without personal data, and retry in a later tick. Do not pretend all contenders always succeed without contention.
Once a task is DUE, later ticks do not re-notify. Pickup/return resolve PENDING as well as DUE tasks, including when action wins a race with processing. Restart catches up overdue persisted tasks. No browser tab is required.

## Owned runner
One small stdlib thread owned by FastAPI lifespan invokes an immediate startup tick, then waits interruptibly for 30 seconds between ticks. Default BS_TASKS_ENABLED=true; false is allowed for isolated tests. No scheduler framework, queue dependency or unbounded executor.
Use separate connections per transaction/thread. Check stop between candidate transactions; keep per-tick work bounded above. Stop via Event and bounded join (10 seconds), off the async event loop. Record/report failure to stop; never silently discard a live thread as successful cleanup. Preserve inference registry shutdown behavior. Unexpected tick errors are observable and do not silently disable future ticks. No credentials, model, or network access.
Do not use daemon status as proof of cleanup. Verify the actual owned runner starts, processes without HTTP reads, and terminates on lifespan exit. If the existing database timeout cannot support this shutdown contract, report the concrete conflict rather than inventing success.

## UI contract
Add a compact "Coordination" area using only server tasks and scoped snapshot request/loan data. Pickup label: "Arrange pickup"; explain the pickup location and that a person records pickup. Do not label pickup as late/overdue or invent a 24-hour promise. Return label: "Return reminder", showing the real loan due instant. DUE return tasks may be labelled "Return due"; PENDING ones are upcoming. RESOLVED tasks are separate from active tasks and remain inspectable.
PENDING pickup is already actionable; pending status only means the runner has not processed its notice. Explain status in plain language without exposing implementation detail unnecessarily.
Use the existing deliberate Sync snapshot control and refresh after successful lifecycle actions. Show that the list reflects the last successful sync. Do not add polling, compute persisted DUE state from browser time, auto-approve a lifecycle action, or fabricate an external delivery claim. Network errors retain existing safe retry/session handling. Empty tasks is a valid empty state; a missing/malformed required tasks field is a contract error, not invented empty data.
Keep existing intake controls, draft ownership, 409/uncertain retry behavior, accessibility and responsive layout intact. Runtime uses real relative /api calls; fixtures/interception are test-only.

## Acceptance gate
Backend: existing suite plus v1->v2 migration/backfill, same-workspace enforcement, snapshot shape/order, exact replay/rollback, pickup/return before and after due processing, equality boundary, repeat/restart, concurrent independent connections/runners, SQLite contention recovery, and actual lifespan startup/stop tests. Use controlled clocks/events and bounded waits. No model inference; disable the assistant for local smoke.
Frontend: exact typed parsing and test fixtures, empty/active/resolved states, scoped associations, correct timestamp display, stale-snapshot/session/error behavior, unchanged human actions, and desktop/mobile browser checks with fixture-only data clearly labelled as such.
Codex later integrates both and proves real persisted task processing while no browser is open, refresh/restart behavior and full lifecycle in a real browser. Fixture tests are not full-stack acceptance. Public hosting/security/abuse controls, fresh live proof allocations, video and final MAX gates remain separate.
