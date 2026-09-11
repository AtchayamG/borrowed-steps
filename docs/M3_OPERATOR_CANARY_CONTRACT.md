# BS-022 operator canary runner

Codex HIGH decision after BS-021 acceptance, 2026-09-11.
Implement the last offline integration before one separately authorized provider
check. Do not add another general framework, public route or quota service.
Worker authorization is ZERO provider calls and ZERO credential access.

## Minimal runner

Add scripts/operator_canary.py with a callable operator entrypoint. It receives
an explicit Groq API key, explicit PostgreSQL receipt URL, and an explicit dated
grant. No credential/environment discovery, default grant or automatic grant
creation. The CLI supports only manifest preparation and offline fixture checks.
Real invocation remains a Codex-owned callable action after independent review.

A grant must bind receipt/owner/authorization UUID4s, expiry, the exact BS-020
candidate hash and an approved execution-manifest hash; live_authorized defaults
false and must be exact True for an execution. It is a trusted operator input,
not an authentication mechanism. Do not issue an actual grant in this task.

Prepare a deterministic manifest of the BS-020 plan plus exact file hashes:
groq_model.py, strands_interpreter.py, canary_receipt.py, groq_canary.py,
admission_probe.py, operator_canary.py, requirements.lock, requirements-groq.lock
and requirements-postgres.lock. Include fixture, pinned ceilings and retry policy.
Do not hash generated artifacts or include absolute machine paths. Keep the
original BS-020 offline plan unchanged, including live_authorized=false.
An execution manifest is a separate identity; changing it cannot silently reuse
operator approval. The operator must retain the approved grant as its audit
record; emit its nonsecret identity and execution hash in the final receipt
summary. Verify hashes from this checkout before reserving/dispatching. Missing
or changed files, bad grant, expired grant or wrong plan means zero provider sends.

Call CanaryReceiptStore.reserve and then mark_dispatched. Only a successful
first transition permits construction/invocation of the network model. Existing
DISPATCHED/terminal/recovered receipts cannot send again. Storage failure or lost
transition confirmation means zero new sends; leave the receipt for explicit
operator recovery. Do not automatically recover or retry any receipt.

Reuse run_canary_stages with one GroqModel and the accepted fixed fixture;
do not duplicate its tool/extraction logic. Minimal changes in groq_canary.py
are allowed to disable Strands automatic retry (retry_strategy=None), provide
an outer bounded operation with owned cleanup, and produce truthful sanitized
failure/count fields. Keep the offline CLI strictly offline and existing fixture
tests passing. Do not modify production GroqModel or production interpreter.
The model and owned transport must close on every path, including cancellation
and a failed receipt finalization. Never label unfinished cleanup as success.

Use a small transport observer around the injected fixture transport or the
explicit live HTTP transport (retries=0, trust_env=False); actual requests still
pass the existing GroqModel pinned-target/envelope/deadline checks. Observe
send counts, serialized byte sizes and status codes only. Never persist API keys,
headers, prompt text, model output/prose/reasoning, raw exceptions or raw responses.
No home-grown SSE parser or new dependencies to obtain token counters:
actual_total_tokens stays NULL unless a complete trustworthy total is available.
Do not label reservations as observed usage or claim total-token quota safety.

Success requires actual tool execution, source-field assertions and completed
cleanup. Finalize the receipt with measured sends and nullable measured tokens.
Use fixed FailureCode values and conservative UNCERTAIN for cancellation,
ambiguous transport/cleanup or storage-finalization outcomes. Confirmed provider
or validation failure may be FAILED_CONFIRMED only after the execution is settled.
A failed finalization leaves the existing active receipt blocked; do not repeat
inference to produce a cleaner report. Separate aggregate summary provenance:
offline_fixture for injected fixtures; live_provider only for a real attempted
provider execution, with success/failure recorded independently. Never relabel
the old offline evidence as a live result.

## Verification and ownership

Add focused tests for changed manifest/grant/expiry refusing before network,
identical replay never redispatching, actual Strands tool/extraction through
injected HTTP fixtures, 429/invalid output without automatic retry, cancellation,
cleanup failure, finalization failure retaining the active receipt, truthful
unknown token/provenance fields, and no raw data leakage. Exercise the receipt
against disposable local PostgreSQL, including two concurrent runner attempts
and exactly one dispatch owner. Reuse existing database fixtures.

Use local Python3.12, unchanged locks, ruff/format/mypy and focused receipt/Groq/
canary tests. Start your own disposable PostgreSQL cluster if needed using the
existing binaries at ../BS-011-claude/.pgtest/pgsql/bin; assign a free numeric
loopback port, record it, close/drop only your own test databases and stop only
your own server. Server control output should go to a log file rather than a
captured pipe inherited by the server. No production/cloud database access.

Task-owned files: new scripts/operator_canary.py, tests/test_operator_canary.py,
scripts/groq_canary.py and tests/test_groq_canary.py if needed for the limited
changes above; test-evidence/bs022/**; docs/OPERATOR_CANARY.md;
docs/workers/BS-022.md and the four worktree checkpoint files.
Existing production source, migrations, locks, contracts, app/frontend, deployment
and program-control files are frozen. Stop on an actual architecture conflict.
A prepared runner is not a live result, public assistant or release approval.
