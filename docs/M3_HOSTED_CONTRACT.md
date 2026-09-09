# M3 hosted contract — foundation gate, 2026-09-09

Codex architecture decision at ASTRA_HIGH. This amends the local-only M2A/M2B restrictions **for a future explicitly selected hosted mode**. Existing local behavior and evidence remain unchanged. Implementation is authorized in bounded worker tasks; account creation, live model calls and deployment are not part of this gate.

## Selected deployment direction

One Vercel Hobby project serves the built React SPA through its CDN and a Python 3.12 FastAPI function through the same origin. PostgreSQL on Neon Free supplies durable storage. GitHub Actions on standard public-repository runners invokes an authenticated due-processing endpoint. Groq's free tier with `openai/gpt-oss-20b`, through the real Strands OpenAIModel, is the selected hosted inference candidate. Compatibility is provisional until a separately allocated live proof passes. No local-machine origin, tunnel or local Ollama public dependency.

Do not assume that native FastAPI deployment creates separate functions for individual routes. Start with the standard supported FastAPI packaging and lazy provider imports; measure build size, cold-start CPU and tick cost before splitting functions. Keep API routes ahead of any SPA fallback. Invalid API routes must never return index.html. Production startup cannot create a SQLite file or start TaskRunner.

## Cadence and honest UI

Hosted scheduling target: every 15 minutes, offset from the hour (`7,22,37,52 * * * *`, UTC). It is best effort: GitHub can delay or drop runs. There is **no promise of delivery within 15 minutes**. Persisted due timestamps remain exact; PENDING means not yet processed. Browser refresh only reads; the independent tick remains responsible for due processing. Local TaskRunner retains its 30-second interval.

Each tick processes at most 100 candidates with bounded execution. No request body can choose another workspace or increase this limit. Later integration must retain aggregate last-success evidence and expose honest stale/unavailable state rather than claim a healthy scheduler from configuration alone. No external messages or autonomous lending decisions.

Neon's documented 0.25-CU minimum and five-minute idle suspension imply roughly 60 CU-hours/30 days, or 62/31 days, for isolated 15-minute ticks before query duration and human traffic. This estimate requires a compute capped at 0.25 CU; autoscaling, repeated readiness probes, pool keepalives and failed-job retries can invalidate it. Neon Free suspends compute when its 100 monthly CU-hours or network quota is exhausted. Vercel quotas are shared with the user's other projects; the screenshot is not a remaining-budget guarantee. No paid upgrade or trial fallback.

## PostgreSQL adapter boundary (BS-011)

Keep domain, application ports and use cases unchanged. Use a synchronous psycopg 3 adapter matching the existing synchronous Store port. Public entry point: `PostgresStore(database_url: str)` in `infrastructure/postgres_store.py`. Constructor must not run migrations. Provide an explicit read-only `check_schema()` and a separate `apply_migrations(database_url: str)` admin function in `infrastructure/postgres_migrations.py`. These take explicit parameters, never discover unrelated credentials. Normal pooled connections use bounded connect/statement/lock timeouts and close after each unit of work; no in-process keepalive pool.

Serialize **all writes within each workspace**, using a transaction-scoped lock on its `workspaces` row before reading idempotency or lifecycle state. PostgreSQL READ COMMITTED plus the workspace row lock is adequate for these short writes; different workspaces may proceed independently. Retain partial uniqueness for active loans and workspace-scoped foreign keys as durable backstops. A read-only snapshot must be internally consistent across lists, e.g. a read-only REPEATABLE READ transaction. Never hold a DB transaction across inference or an HTTP call.

Idempotency lookup, domain operation, event writes and exact response bytes belong to the same serialized write transaction. An `ON CONFLICT DO NOTHING` insert after side effects is **not** a replacement for serialization. Same key/different payload must return the existing conflict; identical retries replay exactly. Preserve response strings without JSONB reserialization.

Due candidate discovery remains a bounded, ordered list of identifiers, not a claim. Do not add `SKIP LOCKED` to a short discovery transaction and pretend its locks survive into a later transaction. Re-read task and loan inside the workspace write transaction; conditional transition plus event must commit or roll back together. Human pickup/return uses the same serialization discipline.

Fresh PostgreSQL schema contains the current M1/M2B tables and constraints with its own migration version. Run explicit additive migrations under a transaction-scoped advisory lock through a direct connection. Normal connections use the pooled endpoint. Disable auto-prepare conservatively (`prepare_threshold=None`); Neon's protocol-level prepared statement support means this is a simplification, not a claim that all prepared statements are forbidden. No ORM or generic repository framework.

## Hosted model transport boundary (BS-012)

Implement `GroqModel`, a small Strands `OpenAIModel` adapter, in `infrastructure/groq_model.py`. It takes an explicit API key and per-interpretation six-send budget, pinned endpoint `https://api.groq.com/openai/v1` and model `openai/gpt-oss-20b`. No environment-key discovery, endpoint override, fallback model, tools with side effects, or hosted Ollama relabeling.

Reuse Strands streaming and structured-output formatting wherever possible. Enforce the send budget at the HTTP transport boundary, shared by both stages and any Strands continuation/retry. Charging once per `stream()` is insufficient. Disable hidden OpenAI SDK and transport retries; redirects and environment proxies must be disabled. A seventh transport send is impossible, and budget exhaustion is sticky. Limit request timeout to 60 seconds and share an absolute operation deadline no greater than the existing 110 seconds. Cancellation/deadline must abort pending transport and close owned resources before reuse. Do not add an unbounded background cleanup task.

The offline proof must use real Strands/OpenAI SDK serialization with an intercepted HTTP transport: stage one is a real Strands tool loop, and stage two calls the public structured_output method once with the existing extraction schema and no tools/streaming fields. Inspect the resulting request JSON. A parsed schema is not business grounding: later integration retains the existing source-only evidence validators and real inventory tool-count checks. Missing/invalid output is a failure, never a fabricated draft.

BS-012 stops at this transport adapter and proof. It does not refactor the accepted Ollama interpreter, change public schemas, implement DB admission or wire a production route. This keeps its files independent from BS-011 and avoids revisiting the old correction loop before the new SDK path is understood.

## Public shape reserved for the integration gate

Local mode remains `strands_ollama` with provenance `strands` / `ollama` / `llama3.2:3b`. A successfully configured hosted assistant uses `agent_mode: strands_groq` and provenance `strands` / `groq` / `openai/gpt-oss-20b`; the application Interpretation type already permits these strings. Existing disabled behavior remains unchanged. The future hosted health milestone is `M3`. Do not widen validators to arbitrary strings: the frontend and HTTP adapter must accept only the documented tuples. No claim of AgentCore.

Future settings: explicit `BS_RUNTIME=local|hosted`, `BS_STORE=sqlite|postgres`, `BS_ASSISTANT_PROVIDER=ollama|groq`, private `BS_DATABASE_URL`, `BS_GROQ_API_KEY`, `BS_TASK_TICK_TOKEN`. Hosted mode requires PostgreSQL, secure session cookies, an exact public Origin allowlist and no local TaskRunner. Hosted assistant stays disabled until provider proof and shared admission pass. Missing configuration fails closed without secret values in logs.

Future `POST /api/internal/tasks/tick` is a separate authenticated operational route within the deployed API, omitted from public UI/OpenAPI. Use a server-managed bearer secret, constant-time comparison and counts-only responses. Authentication occurs before database work. Scheduled workflow uses a protected secret, standard Linux runner, minimal permissions, no dependency installation, bounded curl timeout, no verbose credential output, and no unbounded retry. It must not run on pull-request events.

**Release-blocking admission work is still pending.** Per-process locks or per-workspace caps cannot protect an account-wide free quota across serverless instances. Before enabling public inference, implement transactional global admission with conservative request AND token reservations, per-workspace limits, a single active lease with a deadline, and safe crash recovery. An expired lease must not permit a still-live call to overrun the reserved budget. Do not hold its DB transaction open during inference. Limits must be based on measured prompt/output size and actual account quota. An 8,000-token/minute ceiling can fail one multi-send interpretation even when daily estimates look safe; the worker's 35–50/day estimate is not an approved admission policy. Retain 429/timeout/failure handling and structured intake when inference is unavailable.

## Gates, rollback and test scope

BS-011 (Claude): PostgreSQL adapter, migrations and real local Postgres tests. BS-012 (AGY): Groq transport adapter and offline wire-format/cleanup/budget proof. They run in parallel in separate worktrees. Later tasks integrate provider orchestration, global admission, settings/HTTP/UI, Vercel packaging and scheduled workflow **after** these foundations are reviewed. No worker edits those shared integration files now.

Public release needs actual free-plan entitlements/remaining quotas, verified non-commercial Hobby eligibility, measured resource headroom, shared admission, a real hosted interpretation, real database persistence/concurrency proof, actual scheduled-run evidence and public browser acceptance. A personal, unpaid, non-monetized demonstration is the intended use; the proposal's suggestion that the user can simply decide provider eligibility is not sufficient evidence. Resolve any terms ambiguity before deployment. No SLA or uninterrupted availability through October 9 is claimed from plan names.

Hosted rollback must redeploy a previously verified **PostgreSQL-compatible hosted build**, preserving the same durable DB and compatible schema. Never point Vercel at SQLite. Before the first accepted hosted build there is no known-good hosted rollback: keep the rollout gated. Preserve database data, do not silently discard it or rebuild workspace state from local demo fixtures. Assistant may be disabled independently while human workflows remain available.

## Primary sources checked 2026-09-09

- [Vercel FastAPI](https://vercel.com/docs/frameworks/backend/fastapi): Python app and CDN static files supported. Separate route functions not assumed.
- [Python runtime](https://vercel.com/docs/functions/runtimes/python): runtime selection and packaging limits need build verification.
- [Hobby plan](https://vercel.com/docs/plans/hobby) and [fair use](https://vercel.com/docs/limits/fair-use-guidelines): quotas, suspension and non-commercial restriction; no account entitlement inferred.
- [Vercel cron](https://vercel.com/docs/cron-jobs/usage-and-pricing): Hobby daily-only, so not our 15-minute scheduler.
- [GitHub scheduling](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows): minimum five minutes, default branch, delay/drop risk, inactivity disabling. [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions): standard public runners free; storage/larger runners differ.
- [Neon plans](https://neon.com/docs/introduction/plans), [computes](https://neon.com/docs/manage/computes), [pooling](https://neon.com/docs/connect/connection-pooling): compute/storage limits, idle behavior and transaction pooling. Direct markdown copies were fetched when the web reader rejected that content type.
- [Groq structured output](https://console.groq.com/docs/structured-outputs), [compatibility](https://console.groq.com/docs/openai), [rate limits](https://console.groq.com/docs/rate-limits): candidate supports strict schema; tool phase separate; actual account capability still untested.
- [PostgreSQL locks](https://www.postgresql.org/docs/current/explicit-locking.html), [psycopg prepared statements](https://www.psycopg.org/psycopg3/docs/advanced/prepare.html): transaction locks and conservative pooler configuration.

Installed Strands 1.54.0 `models/openai.py` was inspected: `_get_client` closes newly created SDK clients; `structured_output` removes streaming fields and uses the OpenAI SDK `.parse()` method. The HTTP-boundary budget and end-to-end Groq behavior remain worker/live test obligations. This is an architecture decision, not a final security/release audit.
