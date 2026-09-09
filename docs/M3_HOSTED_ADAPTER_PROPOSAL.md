> Historical worker proposal. Codex accepted the direction with corrections in [M3 hosted contract](M3_HOSTED_CONTRACT.md); its cadence guarantee, locking, budgeting, rollback and decomposition claims below are superseded.

# M3 hosted migration recommendation - BS-009

Bounded compatibility investigation against `25e6bb3`. A recommendation for
Codex to decide on, not an approved architecture and not a deployment. Nothing
was provisioned, no account was touched, no model was called.

## Recommendation

**One topology: Vercel Hobby (FastAPI + built SPA) -> Neon Postgres free ->
GitHub Actions scheduled workflow as the independent scheduler -> Groq free tier
as the hosted model, reached through the Strands `OpenAIModel`.**

I did not need a second topology. Every component verified as *capable*; the one
thing that fails is not a component but a parameter - the 30-second coordination
cadence - and swapping providers does not rescue it (see "The cadence finding").

Three properties decided it. GitHub Actions is free for public repositories on
standard runners, and the repository must be public under MIT anyway, so the
scheduler adds no new account, card or trial. Neon's free plan is a plan, not a
trial, with no documented expiry - unlike Render's free Postgres, which the
earlier feasibility note recorded as expiring after 30 days. And Groq's
structured-output constraint fits the M2A two-stage design exactly, which is what
makes this migration small rather than a rewrite.

**Unresolved entitlement blockers - none of these are resolved by this report:**

1. Vercel Hobby is restricted to "non-commercial, personal use only". Whether a
   hackathon entry qualifies is the user's call, not mine.
2. The Vercel screenshot evidences a displayed plan. It does not evidence
   remaining quota at deploy time, deployment authorization, or a token.
3. No Neon account is known to exist. Free-plan *terms* are verified; an
   *entitlement* is not.
4. No Groq account is known to exist, and no API key exists. Groq's free tier is
   documented as a plan tier; the page does not describe it as permanent, so
   continuity through 9 October is **unverified**.
5. Exceeding a Vercel Hobby limit is an **enforced pause, not a bill**: "in most
   cases, if you exceed your usage limits on the Hobby plan, you will have to
   wait until 30 days have passed before you can use the feature again." A
   blowout during judging takes the site down for up to 30 days, which crosses
   the 9 October deadline. This is the single largest availability risk and it
   is a capacity-planning problem, not a spending one.

## The cadence finding

**A 30-second cadence is infeasible on any free topology I verified, and Vercel's
own scheduler cannot drive it at all.**

Vercel Hobby cron jobs are limited to "once per day" with per-hour (±59 min)
precision, and a more frequent expression **fails at deployment**: "Cron
expressions that would run more frequently will fail during deployment." So the
scheduler must be external regardless. GitHub Actions' minimum is "once every 5
minutes", and scheduled workflows "may be delayed during periods of high load".
That alone rules out sub-5-minute cadence.

The binding constraint is the database, not the scheduler. Neon free gives 100
CU-hours per project per month; the smallest compute is 0.25 CU; scale-to-zero
triggers "after 5 minutes of inactivity" and "cannot disable". So:

- Budget: 100 CU-h ÷ 0.25 CU = **400 wall-clock hours of awake compute a month**,
  against 720 hours in a 30-day month - a **55% duty cycle ceiling**.
- A tick every *T* minutes keeps the compute awake roughly 5 minutes per tick,
  so duty ≈ 5/*T* (plus the tick's own work).

| Cadence | Approx. duty | CU-hours/month at 0.25 CU | Verdict |
|---|---|---|---|
| 30 s | ~100% | ~180 | 80% over the free quota |
| 5 min | ~100% (never idles) | ~180 | 80% over |
| 10 min | ~50% | ~90 | Inside, ~10 CU-h left for all human traffic |
| **15 min** | **~33%** | **~60** | Inside, ~40 CU-h headroom |
| 30 min | ~17% | ~30 | Comfortable |

**The smallest honest cadence I can propose is 15 minutes.** Ten minutes is
arithmetically inside 100 CU-hours but leaves roughly a tenth of the budget for
every human page view across judging, which is not a margin I would defend. **I
do not authorize any cadence change; this is Codex's decision**, and it changes
what the product means: a return reminder becomes accurate to within about a
quarter of an hour rather than half a minute. The M2B UI contract needs no
change - PENDING still means "the runner has not processed this notice" - but
that wording's honesty depends on Codex accepting the new interval.

## What ports unchanged, and why the change is small

`sqlite3` is imported in exactly one file, `infrastructure/sqlite_store.py`. The
`Store` and `WorkspaceUnitOfWork` ports already express everything the
application needs, so a Postgres adapter is a sibling file, not a rewrite.
`requires-python = ">=3.11"` is compatible with Vercel's default Python 3.12
(3.11 is not offered; 3.12, 3.13 and 3.14 are), so no source change is needed for
the interpreter - only a version pin.

**The provider finding.** Groq documents that "Streaming and tool use are not
currently supported with Structured Outputs", and strict mode requires all fields
required with `additionalProperties: false`. The M2A design already splits the
run into stage one (real `read_inventory` tool calls, deliberately **no**
`structured_output_model`) and stage two (one native schema request, **no**
tools). Those halves fall on opposite sides of Groq's restriction, so both are
supported - and `_Extraction`'s eight fields are already all required and
nullable, which is exactly what strict mode wants. This compatibility is
accidental, not designed, and it is the reason I am not proposing a new
extraction path.

Strands 1.54.0 ships `models/openai.py`; `OpenAIModel` accepts `client_args`
(so `base_url` can point at Groq), and its `structured_output` calls
`client.beta.chat.completions.parse(**request, response_format=output_model)` -
the strict json_schema path. It creates a client per request inside
`_get_client`, which means the owned-client lifecycle problem that
`OwnedOllamaModel` exists to solve largely disappears; the **request budget does
not**, and must be re-implemented by overriding `stream` and `structured_output`
on an `OwnedOpenAIModel` subclass. The existing `OwnedOllamaModel` is a native
Ollama client and is **not** a portable API client; it stays for local
development. Adding `strands-agents[openai]` pulls `openai>=1.68.0,<3.0.0`.

## Exact changes

1. **`infrastructure/postgres_store.py`** (new). Implements `Store` and
   `WorkspaceUnitOfWork` against Neon. Same schema shape; SQLite's partial unique
   index on active loans becomes a Postgres partial unique index.
2. **Migrations move out of `__init__`.** Running `_MIGRATIONS` on every cold
   start races concurrent instances and burns Active CPU. Run them as a deploy
   step or one-shot admin command under `pg_advisory_xact_lock` (transaction
   scoped - a session-level advisory lock is unsafe through a transaction-mode
   pooler), against the **direct**, non-pooled endpoint. At startup the app only
   *asserts* the expected `schema_migrations` version and refuses to serve if it
   does not match.
3. **No lifespan thread in production.** `TaskRunner` is retained for local
   development and disabled by settings on Vercel. There is no durable process to
   own a thread between invocations.
4. **`interfaces/http/tick.py`** (new): `POST /api/internal/tasks/tick`, bearer
   authenticated from a secret, bounded to the existing 100-candidate limit,
   calling the unchanged `process_due_tasks`. Deploy it as its own function that
   imports the store and coordination module **but not Strands or the HTTP app**,
   so each cold start pays a small CPU cost rather than importing the provider
   SDK 96 times a day.
5. **`infrastructure/owned_openai.py`** (new): `OwnedOpenAIModel(OpenAIModel)`
   carrying the `RequestBudget` charge into `stream`/`structured_output`.
6. **Settings and readiness.** `BS_STORE=sqlite|postgres`,
   `BS_ASSISTANT_PROVIDER=ollama|groq`, `BS_DATABASE_URL`, `BS_GROQ_API_KEY`,
   `BS_TASK_TICK_TOKEN`. Missing configuration must fail closed at startup with a
   coded error, exactly as `_pinned` refuses a mismatched Ollama host today -
   never a silent fallback to the local provider.

Local SQLite + Ollama modes stay the default, so every existing test keeps
running unchanged.

## Transactions and concurrency

SQLite gave serialization for free through `BEGIN IMMEDIATE` and one writer.
Postgres does not, so four things become explicit:

- **Reservation uniqueness.** `SELECT ... FOR UPDATE` on the equipment row inside
  the transaction, plus the partial unique index as the durable backstop. The
  loser still gets `409 STATE_CONFLICT` from the existing version check.
- **Workspace scoping.** Unchanged: every statement is already parameterised by
  `workspace_id`, and the composite foreign key from BS-007 carries over.
- **Idempotency.** `PRIMARY KEY (workspace_id, key)` with
  `INSERT ... ON CONFLICT DO NOTHING`, then re-read and replay the stored bytes.
  Exact-byte replay is preserved.
- **Due claim + event atomicity.** Unchanged in shape: the conditional
  `UPDATE ... WHERE id = $1 AND status = 'PENDING'` and the event insert in one
  transaction. Add `FOR UPDATE SKIP LOCKED` to candidate discovery so two
  overlapping invocations do not queue on the same rows.
- **Inference admission.** `InferenceSlot` is process-local. On Vercel, N
  concurrent instances mean N concurrent interpretations, and **a process-local
  lock cannot enforce an account-wide free quota**. Admission must move into the
  database - a small `inference_admissions` table claimed and released inside a
  transaction, bounded well below Groq's 30 requests/minute - or the account will
  be rate-limited by the provider instead of by us.

Pooling caveat: Neon's pooler is PgBouncer in transaction mode, which forbids
SQL-level `PREPARE`, session variables and `LISTEN`/`NOTIFY`. Protocol-level
prepared statements are supported, so the driver must be configured not to
auto-prepare. Use the pooled endpoint for request traffic and the direct endpoint
for migrations.

## Required contract and provenance amendments

State these to Codex now rather than discovering them at integration:

1. **`agent_mode` must stop saying `strands_ollama`.** Relabelling a hosted
   provider as Ollama would be false provenance. Proposed value:
   `strands_groq`. This amends the frozen M2A public shape.
2. **`provenance.provider` and `provenance.model`** must carry the real hosted
   provider and model id (e.g. `openai/gpt-oss-20b`), not `ollama` /
   `llama3.2:3b`. The M2A live-proof evidence remains historical evidence for the
   Ollama adapter only; it transfers to nothing hosted.
3. **`health.milestone`** value for a hosted release is Codex's to set.
4. **`POST /api/internal/tasks/tick`** is a new internal route, not a human
   action and not part of the public contract. It must never appear in the UI.
5. **The cadence constant** is a documented product property once it is 15
   minutes; the BACKEND_SETUP wording and any UI copy that implies promptness
   need Codex's review.

## Consumption for a modest judging workload

Assume 40 judge sessions across the judging window, each with ~6 page views and
2 interpretations; ticks every 15 minutes; a 30-day month.

- **Scheduler:** 96 ticks/day = ~2,880/month. Free on a public repository.
- **Neon:** ~60 CU-hours of the 100 free, leaving ~40 for human traffic. Storage
  is trivially inside 0.5 GB; egress inside 5 GB.
- **Vercel functions:** ~2,880 tick invocations plus a few thousand request
  invocations, against 1,000,000 included. **Active CPU is the tight one**: 4
  CPU-hours included, and cold-start imports dominate. At an *estimated* 1-2 s of
  CPU per cold tick, ticks alone are ~0.8-1.6 CPU-hours. This estimate is not
  measured and is the number most likely to be wrong; the minimal tick function
  in change 4 exists to protect it.
- **Groq:** 30 RPM / 1,000 RPD / 8,000 TPM / 200,000 TPD. At ~3 model sends per
  interpretation, RPD supports ~330 interpretations/day. **TPD is tighter**: the
  stage-two system prompt measured 5,518 characters in BS-007 (~1,400 tokens,
  *estimated*), and with the schema echoed in `response_format` a run plausibly
  costs 4,000-6,000 tokens, giving roughly **35-50 interpretations/day** before
  the daily token cap. Abuse cap: enforce a per-workspace daily interpretation
  limit in the database well below that, and keep the assistant default-disabled.

Free tier vs trial: Neon Free and Vercel Hobby are documented plans; Vercel's Pro
*trial* is explicitly separate and is not proposed. Groq's page does not say
whether its free tier is permanent - **marked unverified**. None of these is an
SLA; no uptime is promised by any of them, and I claim none.

## Implementation tasks, tests, rollback, prerequisites

**Task A (backend store):** `infrastructure/postgres_store.py`, migration
runner, settings, `tests/test_postgres_store.py`. **Task B (serverless
execution):** `interfaces/http/tick.py`, lifespan gating, `vercel.json`,
`.python-version`, `tests/test_tick_endpoint.py`. **Task C (provider):**
`infrastructure/owned_openai.py`, provider selection, database-backed admission,
`tests/test_owned_openai.py`. Disjoint files; A and C are independent; B depends
on A.

**Proof matrix.** Offline: the full existing suite unchanged on SQLite; the same
lifecycle/contention/idempotency suites re-run against a real local Postgres;
a fake-transport test asserting the outgoing Groq request carries strict
json_schema and no tools in stage two, and tools and no schema in stage one.
Live (needs a new Codex allocation): one authenticated tick invocation observed
in the database with no browser open; one real hosted interpretation with real
tool provenance; a full browser lifecycle against the deployed URL.

**Rollback.** Migrations are additive and versioned; the local SQLite path is
never removed, so rollback is redeploying the previous commit and pointing
`BS_STORE` back. There is no destructive migration in this plan.

**Prerequisites (user action, none taken):** confirm Hobby non-commercial
eligibility; create Neon and Groq accounts without card or trial; generate a Groq
key and a tick secret; make the repository public.

## Security decisions for Codex's later gate

Not a security audit - a list of decisions someone must make: how the tick
endpoint authenticates (shared bearer secret, rotation, constant-time compare,
and whether a leaked secret can do more than advance a notice); whether
`X-Forwarded-For` from Vercel is trusted for any rate limiting; per-IP and
per-workspace request limits now that the origin is public; where the Groq key
and tick secret live and who can read them; and whether 1 hour of Hobby runtime
log retention is enough to investigate anything during judging.

## Evidence

All URLs accessed **2026-09-09**.

| Claim | Source |
|---|---|
| Hobby cron: once per day, ±59 min, more frequent fails deployment | https://vercel.com/docs/cron-jobs/usage-and-pricing |
| Hobby: 300 s max duration, 2 GB/1 vCPU, 500 MB Python bundle, 4.5 MB body | https://vercel.com/docs/functions/limitations |
| Python 3.12 default (3.13, 3.14 available); entrypoint and dependency rules | https://vercel.com/docs/functions/runtimes/python |
| Hobby: 4 CPU-hrs Active CPU, 360 GB-hrs memory, 1M invocations, non-commercial only, 30-day pause on overage | https://vercel.com/docs/plans/hobby |
| FastAPI deploys as a Vercel Function | https://vercel.com/docs/frameworks/backend/fastapi |
| Neon Free: 0.5 GB storage, 100 CU-hours/project, scale-to-zero after 5 min, cannot disable, 5 GB egress | https://neon.com/docs/introduction/plans |
| 1 CU = 1 CPU with 4 GB RAM; CU-hours = CU-seconds / 3600 | https://neon.com/docs/introduction/usage-calculations |
| Smallest compute 0.25 CU (1 GB RAM); scale-to-zero after 5 minutes | https://neon.com/docs/manage/computes |
| PgBouncer transaction mode; `-pooler` endpoint; no SQL-level PREPARE, session vars, LISTEN/NOTIFY | https://neon.com/docs/connect/connection-pooling |
| Actions schedule: minimum 5 minutes; delayed under high load; disabled after 60 days of repository inactivity | https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows |
| Actions free for public repositories on standard GitHub-hosted runners | https://docs.github.com/en/billing/managing-billing-for-your-products/about-billing-for-github-actions |
| Groq strict structured outputs on `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`; "Streaming and tool use are not currently supported with Structured Outputs"; strict requires all fields required and `additionalProperties: false` | https://console.groq.com/docs/structured-outputs |
| Groq free tier: 30 RPM, 1,000 RPD, 8,000 TPM, 200,000 TPD; 429 with `retry-after` | https://console.groq.com/docs/rate-limits |

Local source inspected read-only at `25e6bb3` and in the BS-007 virtual
environment: `services/agent/pyproject.toml` (`requires-python = ">=3.11"`,
`strands-agents[ollama]==1.54.0`), `infrastructure/sqlite_store.py` (sole
`sqlite3` importer), `strands/models/openai.py` (`client_args`, `_get_client`,
`structured_output` via `client.beta.chat.completions.parse`),
`strands/models/_strict_schema.py`, and `strands_agents-1.54.0.dist-info/METADATA`
(`extra == 'openai'` requires `openai<3.0.0,>=1.68.0`).

**Marked unverified:** Groq free-tier permanence and availability through 9
October; Groq's acceptance of the OpenAI SDK `.parse()` helper specifically (the
docs show Pydantic examples but do not name that method); per-run token cost and
per-tick Active CPU, both estimates rather than measurements; whether the driver
choice (`psycopg` vs `asyncpg`) needs further pooler-specific configuration
beyond disabling auto-prepare. No component was exercised: this report contains
no live provider call, deployment or account action.
