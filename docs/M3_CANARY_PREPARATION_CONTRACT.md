# BS-020: prepare one bounded Groq canary offline

Codex HIGH decision, 2026-09-11. BS-019 accepted at f6d2be6.

## Hosting decision

Retain Vercel + Neon PostgreSQL + Groq Free as the candidate. Do not migrate to
AWS simply because a code was redeemed. The $50 code from the Agents for Humans
email is active until 2026-10-31, but the console names it Football for Good.
Service coverage/project applicability and taxes remain unresolved; no AWS
runtime use is authorized. Other account credits are not this project's budget.
Chrome disconnected before the coverage check; this is not a redemption failure.

Groq and Neon currently require sign-in on the available Edge browser. This does
not prove accounts do not exist. Account-specific quotas, plan, other consumers,
Neon entitlement and Vercel shared usage are still prerequisites to activation.
Official rules rechecked September 11: submission September 15 05:30 IST,
judging ends October 9 05:30 IST; Strands required, AgentCore optional. Separate
submissions must be substantially different. Keep Borrowed Steps first.

## Independent implementation now

Prepare a small, callable verification harness, an immutable candidate plan and
offline tests. This task authorizes zero provider calls, zero credentials and no
network-capable CLI mode. It does not implement admission or enable the app.

One candidate input, all synthetic:
"Priya S wants crutches from the Adyar centre, back by 2026-10-01T08:00:00Z."
Use existing seed inventory counts, accepted extraction schema and prompts.
Stage one: real Strands Agent selects and executes read_inventory, at most two
tool attempts, bounded agent turns, same GroqModel for both stages. At most one
existing recovery prompt if the model omits the tool and sufficient send budget
remains. No direct Python tool call masquerading as agent execution.
Stage two: exactly one structured_output call with source-only messages. Require
an actual successful tool event before extraction. Assert quoted source fields
match the fixed fixture; do not publish a draft or write any business data.
No automatic retry of provider failures, rate limits, length errors or invalid
output. Existing model ceiling: six physical sends, 110s operation, 60s request,
16384 bytes per send, 1024 max_completion_tokens, reasoning_effort low.

Use an explicit injected GroqModel for the callable harness. The only CLI mode
available now is offline and constructs the fail-closed mock transport with the
existing explicit dummy key. It must never read real credential environment
variables, fall back to default networking or offer a --live option. Reuse
admission_probe helpers and existing constants/schema; leave historical BS-015
and the production interpreter untouched. Small extraction of helper functions
within the new script is fine; no new framework, dependencies or runtime refactor.

Plan artifact records fixture, source/lock hashes (so changed code invalidates the
candidate), provider/model, request/deadline/byte/output ceilings, retry policy,
expected success/failure criteria, live_authorized=false and token-accounting
unknowns. Exclude its own generated evidence from hashes. Same inputs produce
the same plan hash. No arbitrary intake or endpoint overrides. Do not invent
account token allowances or treat bytes/synthetic usage as provider measurements.

Evidence: per-stage sends and actual successful tool count, request byte sizes,
observed fixed fields, output-field assertion booleans, cleanup outcome, error
category and provenance=offline_fixture. Do not log API keys, headers, model
prose/reasoning, raw responses, machine environment or private paths. If usage is
present, label it fixture data; missing usage is unknown, not zero. Suppress
default Strands console output. Always close the owned model in a finally path.

Offline tests use real installed Strands/OpenAI serialization and cover normal
tool/extraction, omitted tool refusal, attempted third tool call, invalid source
field, length-limited extraction, provider429 without retry, shared sixth/seventh
send bound, cleanup on failure and deterministic plan invalidation. No brittle
timing sleeps or mirrored implementation tests. Verify CLI cannot access network
or credential environment even when real-looking variables are present.

## Subsequent gate

Codex reviews the harness and plan, verifies accounts and independently grants
a new dated allowance before any real call. That later execution must reserve a
durable receipt before dispatch, allow only the approved plan hash, prevent replay
and preserve uncertain debits. BS-020 is preparation, not authorization, production
integration, universal token-bound proof or release approval. All29 historical
probe allowances remain closed. Global admission and hosted integration follow
real evidence; MAX final audits, live video and submission remain mandatory.

Sources checked: https://agentsforhumans.devpost.com/rules,
https://console.groq.com/docs/rate-limits. AWS receipt and console verification
are in the private program-control review note; never copy the redemption code.
