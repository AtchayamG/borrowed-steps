# M2A provider recovery decision - 2026-09-08
Approved by Codex at user-confirmed ASTRA_HIGH. Applies to BS-003-R2. This amends only implementation/recovery/probe constraints in M2A_CONTRACT.md. All HTTP shapes, model identifier, nullable evidence grounding, mandatory successful inventory tool, human review, isolation and zero-spend rules remain fixed.

## Evidence and decision
BS-003-R1 at8118a53 fixes environment pinning/tool-attempt failure and adds bounded asyncio execution;174 tests pass. Installed strands-agents1.54.0 native OllamaModel creates an AsyncClient per stream/structured call without close. Installed ollama0.6.2 has a public async context-manager/close API. Limits(turns=6) bounds cycles, not transport attempts: synthetic throttling reproduction produced12 actual stream attempts. First local proof used all5 calls, did not populate explicit due_at and one case skipped read_inventory.
Do not waive cleanup or weaken provenance. Use a narrow owned provider adapter under infrastructure, extending OllamaModel and reusing its public format_request/format_chunk helpers where useful. Override only network-owning stream/structured_output paths needed to control client ownership and limits. Real Strands Agent/tool/structured_output_model remain responsible for agent orchestration. This is a native Ollama integration with a local lifecycle extension; describe it accurately in setup/evidence.

## Transport ownership and request budget
One owned ollama.AsyncClient per logical interpretation, closed explicitly in an async context/finally on success, provider error, timeout, cancellation and budget failure. Its streaming iterator/response must also close deterministically on early termination. No global monkeypatch, private client reach-in, installed SDK edits, background service or copied whole provider module. If a small amount of SDK method logic is adapted, preserve required source/license attribution in task-owned files.
Count immediately before each outbound chat request, before side effects. Shared invocation counter across stream, structured output, recovery and any SDK retry: at most6 requests SENT. Attempt7 is rejected before network; report502 with no draft. Even if SDK catches a budget exception, the sticky budget failure prevents later success. Disable implicit SDK throttling retries using the installed supported retry_strategy=None option; explicit counter is still the authority. No HTTP transport retries/redirects/environment proxies to bypass the pinned loopback destination; use documented client arguments.
Retain110s active-run deadline/60s transport timeout inside120s route guard, with cleanup completing before slot release. Track/cancel/drain owned tasks during application shutdown. A disconnected caller must not make an old response reusable or allow overlapping inference. Do not claim remote Ollama computation cancellation solely from Python task cancellation; distinguish closed local request from observed provider behavior.

## Optional single recovery for skipped tool
Within ONE logical interpretation, if first completed agent result has zero successful read_inventory calls, authorize at most ONE corrective continuation asking that same per-request agent to call read_inventory and return the structured result using original source text. Do not synthesize a tool call, tool message, result or metrics; only actual SDK tool execution counts.
No retry after provider/transport error, timeout, malformed output, budget breach or already successful tool execution. No general retry loop. Both passes share the same client,110s timer,six-request budget and two-tool-attempt budget. Once exhausted, fail honestly. If request budget leaves no room, do not attempt recovery. No browser retries added.
Successful provenance counts real successful inventory calls across that logical interpretation and remains1..2. Record recovery_used and total_model_requests in redacted test evidence only, not HTTP schema. No zero-tool success.

## Diagnose due_at before changing extraction
First inspect the provider's raw schema/result transformation with local fakes. Add reason-coded diagnostics for fields: absent candidate, absent evidence, mismatch, evidence not in source, parse failure, window failure, accepted. Ordinary logs contain field names/reason codes only, no intake/candidates/model reasoning.
Live diagnostic artifacts may show only synthetic submitted text and candidate/evidence fields needed to explain due_at. No hidden reasoning or full prompt/provider dumps. Determine observed cause before claiming it.
Field descriptions, required nullable extraction fields and a consistent worked example may be improved within the same exact-substring grounding contract. Do not fill date deterministically when the model omitted it, loosen evidence checks, infer a zone/date, prefill from inventory or swap model. Multiple/ambiguous dates stay for human clarification.

## Additional live probe allowance for this recovery task
Authorize at most8 ADDITIONAL synthetic /api/intake/interpret invocations on localhost with the existing llama3.2:3b only, after offline lifecycle/budget tests pass. Includes failed/aborted/repeated probes. Original5 remain historical (total ceiling13 across these tasks). Every endpoint invocation still has <=6 raw model requests; keep an append-only ledger of attempt number, synthetic case, actual request/tool counts, recovery used, status and timing.
Use first up to2 for cause diagnosis if needed; reserve remaining calls for final verification. Required final cases: all explicit fields with future timezone-aware due_at/nonzero seconds; relative date and missing location -> respective nulls with successful tool provenance; ambiguous equipment kinds -> null. Run final three-case set twice if budget permits; do not claim a statistical reliability estimate. One successful final set proves only bounded local acceptance evidence.
Create timestamp cases relative to probe clock (e.g.now+7days). Do not count health/snapshot reads as inference. No standalone model calls outside ledger. Stop early if evidence shows unresolved systematic failure; preserve partial work and BLOCKED.
No downloads/cloud/AWS/payment/credits/remote inference. No further expansion without Codex decision.

## Acceptance and limits
Real cleanup tests must exercise owned provider transport with instrumented async clients/streams, not only FakeModel cancellation. Prove close after successful stream, structured output, error, no-chunk stall, cancellation and budget refusal; slot reusable only after cleanup. Prove six outgoing requests across mixed stream/structured/recovery calls and synthetic retry pressure; a seventh never reaches transport.
Retain174 existing backend tests, correcting unsupported test/report names; test optional recovery on zero tools, unchanged zero-tool failure after recovery, shared deadlines/budgets and no writes. Full typed/static checks.
Frontend e4ba9f4 already accepted and HTTP contract unchanged; no AGY task needed.
No M2A integrated/live acceptance yet. Public hosting/provider, M2B and finalMAX audits remain later gates.

## Sources inspected
- Installed strands-agents1.54.0: models/ollama.py, event_loop/_retry.py, agent/agent.py.
- Installed ollama0.6.2: _client.py public context manager and close.
- https://strandsagents.com/docs/user-guide/concepts/model-providers/custom_model_provider/
- https://strandsagents.com/docs/api/python/strands.agent.agent/
- https://strandsagents.com/docs/user-guide/concepts/agents/structured-output/
