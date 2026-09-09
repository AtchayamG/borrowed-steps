# M3 operations gate

Codex decision at user-confirmed ASTRA_HIGH, following accepted BS-013 (300a19f).
This supplements M3_HOSTED_CONTRACT.md. It authorizes bounded local work only.
No new inference allowance, cloud activation, workflow activation or deployment.

## Scheduler foundation: BS-014

Implement a callable hosted tick service and persisted aggregate execution
evidence first. HTTP authentication, workflow packaging and public status/UI
integration follow its acceptance. Keeping the service independently callable
lets actual database concurrency, timeout and crash behavior be tested before
an operational route is exposed. Hosted factory remains as accepted in BS-013.

Reuse application.process_due_tasks and its StopSignal. No new workflow rules,
loan transitions or messages. Discover at most100 candidates, ordered as today.
Stop beginning candidates after a20-second monotonic processing budget. Bound
individual database connects, statements and lock waits as well; checking only
between candidates or cancelling an awaiting thread does not stop blocked SQL.
Use operation-specific limits without weakening normal Store defaults. There
must be a demonstrated finite bound for one final transaction and evidence
recording after the processing deadline. Target a total of at most45 seconds
under the tested database timeout conditions. Do not claim protection from an
arbitrarily stalled OS or a platform hard kill based on that test.

The smallest durable state is one scheduler control row: active run ID, start,
lease deadline, last outcome and completion, last successful completion, and
counts (considered/marked_due/resolved_stale/unchanged/contended/stopped_early).
No intake, borrower text, session IDs, database URLs or unbounded run history.
Use database time for persisted lease/freshness comparisons; monotonic time for
the local deadline. A short atomic transaction admits one run at a time; never
hold that lock/transaction during the full tick.

An unexpired active run refuses duplicate work with a busy outcome. A crashed
scheduler run can be superseded after its60-second lease; an old run ID cannot
renew, finalize or overwrite a newer run's evidence. It must stop starting
candidates once its own processing deadline passes. A late in-flight candidate
can finish safely because both workers still use the existing workspace lock,
conditional task transition and atomic audit event. This scheduler recovery
rule does NOT authorize inference lease recovery.

A complete, uncontended report is successful. Contention or stopped_early is
partial, not success. Reaching100 candidates is conservatively capacity-limited
(no assertion that the backlog is empty). Failure preserves prior successful
evidence and reports a generic failed outcome. A killed process leaves an active
run that later reads as expired; never invent a completion time. If recording
the result fails, the caller must not report durable success. Retry processes
only remaining work and cannot duplicate already committed notices.

Expose a counts/timestamps-only read function. Status values: never_run,
running, success, partial, failed, expired. Freshness is separate: last_success
is recent only within30 minutes of DB time; that is a product warning threshold,
not a scheduling SLA or proof the latest run succeeded. Reading status does not
process tasks. Keep the last outcome visible even when an earlier success is recent.

Append main PostgreSQL migration version2 for this row; never edit schemaV1 or
seed/reset workspaces. Existing explicit migration CLI applies both versions.
Readiness checks the new expected version without migrating. Preserve a real
V1->V2 migration fixture and verify V1 business records/response strings survive.
Update tests' version expectations deliberately; future-version refusal must
test expected+1. A prior V1 build is not automatically compatible with V2:
rollback requires a verified V2-compatible hosted build.

## Operational HTTP/workflow boundary (later integration)

POST /api/internal/tasks/tick: no body-controlled scope/limit; exact bearer
secret with constant-time comparison, no cookie substitute, no public OpenAPI
or UI action. Authenticate before request-specific DB work. BS-013 currently
checks schema at app construction; that is NOT proof of zero DB activity for
an unauthorized cold start. Packaging must explicitly resolve this distinction
before claiming an auth-before-all-DB guarantee. No new route in BS-014.

Workflow target remains7,22,37,52 minutes past each UTC hour, best effort.
Use standard Linux runner, least permissions, secret environment variables,
no dependencies/checkout needed for a curl POST, bounded connection and overall
timeout, no redirect or retry loop, no verbose secrets/response dumping.
Only schedule/manual dispatch; never pull requests. Gate activation with an
explicit repository variable initially disabled, and serialize workflow runs
without cancelling a running invocation. Do not put an active workflow in the
repository during this foundation task.

## Shared inference admission: BS-015 investigation

Groq limits apply to the organization. A dedicated API key is not a separate
quota. Public activation needs actual organization limits and confirmed control
of other consumers, or a conservative allocation accounting for them. The
published30RPM/1000RPD/8000TPM/200000TPD for gpt-oss-20b is a reference, not an
account entitlement or authorization to use it.

Reserve requests AND tokens transactionally across all instances, enforce a
per-workspace limit and a single active interpretation, and retain reservations
through failures. No free refund because a timeout concealed the provider result.
Use DB time and conservative rolling windows or equivalent proven accounting;
do not allow boundary bursts or eviction of reservations that can still be spent.
No database transaction may remain open over inference.

A lease expiring is not evidence that a remote request or suspended worker has
stopped. Until a stronger mechanism is proved, uncertain/crashed inference must
fail closed for new inference and require explicit recovery after confirming
the prior invocation is dead and retaining its full conservative quota debit.
Normal confirmed cleanup can release concurrency without refunding quota.
Structured human workflows must continue when inference admission is blocked.

Before implementing admission, measure real SDK serialized request shapes
offline: worst permitted intake, inventory/tool descriptions, tool-result and
assistant-message accumulation, extraction schema, and maximum output/reasoning
allowance over all six possible sends. A guessed characters/4 ratio is not a
token upper bound. If a safe budget cannot fit the verified provider window,
record that constraint; do not silently relax limits or invent tokenizer proof.
Provider headers, 429/Retry-After and any separate input/output limits need an
explicit treatment. A server cap must be visible in the actual request body.

BS-015 returns a measured proposal and offline fixtures/simulation only. It does
not change production GroqModel, domain/application, migrations, route wiring or
assistant_enabled=False. Codex reviews the evidence before authorizing the
smallest implementation. Scheduler work can proceed independently.

## Official resources rechecked for this decision

- GitHub scheduling can be delayed/dropped and public schedules can disable
  after60 days of inactivity: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- Groq organization-wide limits, exact-account caveat and response headers:
  https://console.groq.com/docs/rate-limits
- Vercel runtime duration/plan limits require actual packaging/account checks:
  https://vercel.com/docs/functions/limitations

No hosting availability through October9 is inferred from plan names. Current
production source remains accepted locally; final live/security/MAX gates remain.
