# Groq admission investigation: evidence accepted, design blocked

Codex review2026-09-10 of AGY source16e2885. This replaces the worker proposal;
its original text remains in Git history. No admission implementation or live
provider call is authorized. Hosted assistant remains disabled.

## Reproduced offline evidence

The actual Strands/OpenAI SDK serialized requests into an injected offline HTTP
transport. All responses, usage counters and429 headers were synthetic fixtures.
No provider was contacted. Intake samples have2000 characters; inventory is the
three-row seed fixture. These are examples, not maximum allowed histories.

| Sample | Send bytes | Total bytes |
|---|---|---|
| ASCII, three sends |2928,3327,5624|11879|
| Unicode, three sends |3690,4089,6386|14165|
| Six sends, recovery/tool loop/simulated429 |2928,3167,3550,3933,5624,5624|24826|

The extraction schema serializes to1029 bytes.6386 is the largest request in
these samples only. The seventh send is refused by the existing sticky budget.
The429 fixture now must raise ModelThrottledException; arbitrary exceptions no
longer count as success. `probe_measurements.json` is the original worker artifact;
`codex-reproduced.json` is the corrected run and explicitly labels its limits.

## Claims rejected by review

No tokenizer was run and no server token count was measured. The response usage
values850/45/895 are hardcoded. Neither JSON byte counts nor characters/4 prove
token usage, upper bounds, a3-5x error, or a7000-token reservation. The previous
3430/4600/6800 estimates and29-58 interpretations/day are withdrawn. No input,
output or reasoning maximum is established; a server output cap was not shown
in these request bodies. Short simulated assistant replies do not bound future
tool calls, assistant accumulation, inventory states or escaping overhead.

The proposed65-second window measured from admission could expire while a run
can still send. A database fence check before HTTP dispatch cannot prevent a
suspended worker resuming between that check and dispatch. Lease expiry alone
must not restore inference capacity. Thirty-second spacing cannot guarantee
zero429 responses, and database transactions were not measured below5ms.

## Decisions retained from M3_OPERATIONS_CONTRACT

- Scope quota to the organization; a key alone is not quota isolation. Actual
  account limits and other consumers must be established before activation.
- Reserve both requests and defensible token bounds across instances, with a
  workspace limit and one active interpretation. No transaction spans HTTP.
- Retain debits through uncertain failure. Reservations that can still be spent
  must not age out. After confirmed completion, conservative accounting must
  cover the relevant provider windows following the final possible send.
- Uncertain or crashed inference stays blocked until explicit recovery confirms
  the prior invocation is dead. Preserve its conservative debit. Fencing alone
  is insufficient. Cleanup can release concurrency without refunding quota.
- Treat provider429/Retry-After/limit headers conservatively, with malformed or
  absent headers handled explicitly. No uncontrolled retries or guaranteed429
  avoidance. Structured human workflows remain usable while inference blocks.

## Unresolved next work

Bound every permitted request component and establish the exact provider/model
token accounting and output/reasoning-cap semantics using primary sources and
offline fixtures. A cap must appear in captured SDK JSON. If an upper bound
cannot fit the verified account window, record infeasibility; do not guess or
weaken limits. This research result is EVIDENCE_ONLY, not a production design.
Scheduler HTTP/workflow packaging can proceed independently. All29 historical
live probes remain closed; no new canary is authorized by this document.

Reproduce from services/agent:
`python scripts/admission_probe.py --output test-evidence/bs015/codex-reproduced.json`
`python -m pytest -q tests/test_admission_probe.py tests/test_groq_model.py`
