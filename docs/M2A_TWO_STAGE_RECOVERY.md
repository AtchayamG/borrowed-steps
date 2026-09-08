# M2A two-stage recovery decision

Approved 2026-09-08 at user-confirmed ASTRA_HIGH for BS-003-R3. Supersedes only the extraction-path and task-specific probe provisions in M2A_CONTRACT.md and M2A_PROVIDER_RECOVERY.md. Public HTTP shapes, grounding, actual inventory-tool provenance, human review, read-only behavior, pinned provider/model and aggregate limits remain unchanged. This is a local integration decision, not release acceptance.

## Evidence and choice

BS-003-R2 at 7b992d1 passes 194 independently run tests/static checks. The worker recorded four successes and four 502 responses in eight probes; the final ambiguous-kind case failed. Installed Strands 1.54.0 default ModelRetryStrategy retries ModelThrottledException, not StructuredOutputException. Structured-output tool forcing happens separately in the event loop. Thus the worker's proposed retry-toggle explanation is unsupported.

Agent.invoke_async(structured_output_model=...) uses a structured-output tool. It does not establish that Ollama's native JSON-schema format parameter was used. OwnedOllamaModel already implements the separate public provider structured_output method using format=output_model.model_json_schema(), stream=False. Use that existing path after the real tool run. Do not add a framework, dependency, provider, model or a general retry engine.

The architectural hypothesis is that separating tool selection from schema generation removes competition between inventory and structured-output tools. This is not a promise of extraction accuracy or tool reliability; live probes must test it. Required nullable fields, exact-source evidence checks and human review remain essential.

## Sequential invocation

1. Build one fresh Strands Agent using the existing owned Ollama model and only the server-workspace-bound read_inventory tool. Invoke WITHOUT structured_output_model. Ask for the inventory read and a short completion; ignore model prose. Do not parse that prose as a draft or expose it. Actual model-selected SDK tool execution is required. Calling the Python tool directly, injecting tool messages/results or manufacturing metrics does not qualify.
2. Check the completed agent result, cancellation, sticky request/tool budgets and observed tool executions. Permit at most one same-agent corrective continuation ONLY for a normally completed first run with zero successful inventory reads and no error, cancellation or exhausted budget. Recovery remains a tool-recovery mechanism only. If it still executed zero tools, fail 502. Failed/limited/cancelled runs cannot proceed to extraction even if a tool previously succeeded.
3. After valid actual tool execution, call the SAME owned model's public structured_output method exactly once for the existing required-nullable extraction schema. Supply a fresh source-only message list with the original intake text and a dedicated extraction system prompt. Do not pass agent/tool conversation or inventory counts into extraction; they must not become evidence or defaults. The model, client and request counter remain the same. This is an explicit provider extraction stage within the Strands-backed adapter, not Agent.structured_output_async (deprecated), and not the Agent structured-output-tool path.
4. Consume/close the extraction generator, validate the typed result, apply existing grounding and assemble truthful provenance from real stage-one tool executions. Reject malformed output with 502 and no draft. Do not retry extraction or fall back to prose/regex/deterministic field filling.

Both stages and the optional tool continuation share ONE six-outbound-chat request ceiling, ONE two-tool-attempt ceiling (1..2 successful calls for success), ONE 110-second active deadline and ONE owned client. Keep 60-second transport timeout, 120-second HTTP guard and retry_strategy=None. No stage resets counters/timers. Check remaining capacity before starting extraction; lack of capacity fails without a new send. Request seven must never reach transport; budget refusal remains sticky. Keep the process-wide single active inference slot until actual cleanup finishes. No automatic frontend retries, queues or background jobs.

Replace obsolete structured-output-tool tests with tests for this approved path while retaining their behavioral invariants. Do not preserve obsolete private test seams solely to retain a test count. Report every removed/replaced test and its replacement coverage.

## Required lifecycle and credential corrections

Installed ollama 0.6.2 explicitly inherits OLLAMA_API_KEY even with trust_env=False. Prevent its value from entering outgoing headers using supported per-client arguments. A fixed, explicitly nonsecret local Authorization marker is acceptable to override SDK inheritance; document it as a marker, not authentication. Alternatively remove authorization through a supported per-client request hook. Do not mutate process environment, inspect real credentials, reach into private clients, patch installed libraries or change localhost/model pinning. Test the real client request construction with a synthetic sentinel key and an in-process fake HTTP transport, asserting the sentinel is absent from outgoing requests/logs; do not test only constructor arguments. Keep redirects/proxies disabled.

Add application lifespan ownership of active inference tasks and cancellation signals. Stop accepting new interpretations during shutdown, signal and cancel owned tasks and drain their cleanup. Caller cancellation and route timeout must initiate cancellation without releasing the slot prematurely or starting another inference. Keep cleanup deterministic and cover cancellation during both stages, including client/generator closure and task/slot reclamation. Do not add a persistent runner or polling service. Use a bounded shutdown drain (up to 10 seconds); if cleanup fails, report the failure and retain task/slot ownership rather than declaring clean shutdown or swallowing the exception. Never claim Python cancellation proves remote Ollama computation stopped.

## Diagnostic evidence

Emit one safe terminal diagnostic per logical interpretation on every outcome, in a finally path after cleanup. Include a server-generated correlation identifier, stage/outcome reason, total attempted model sends (charged before chat), tool attempts/successes, recovery_used, elapsed time and cleanup result. Count a transport failure after the charge conservatively as attempted; distinguish that from proof the model processed it. No raw user text, candidate values, headers, hidden reasoning, tracebacks or full prompts in ordinary logs.

For a single-flight synthetic smoke server, correlate each HTTP attempt with its terminal diagnostic in the test-evidence ledger without changing the public response schema. Write an append-only attempt-start record BEFORE sending; append completion afterward. Interrupted or uncorrelated attempts still consume allowance and remain explicitly incomplete/unknown. Do not backfill invented counts for old failures. Preserve earlier ledgers/reports; append a dated correction of the retry/constrained-decoder claims and historical unknown counters.

## BS-003-R3 local allowance and acceptance

Authorize at most SIX ADDITIONAL synthetic localhost /api/intake/interpret calls after all offline checks pass. Original 5 + R2 8 remain spent; cumulative ceiling is now 19 across these tasks. Maximum six charged chat sends per endpoint invocation; failures, aborted calls and restarts consume the endpoint allowance. No standalone model calls or retries outside the ledger. Same already-installed llama3.2:3b at http://127.0.0.1:11434 only; no download, model swap, cloud, paid calls or account changes.

Freeze the implementation before these acceptance probes; no live prompt-tuning loop. Run the existing three-case set: explicit four fields with aware now+7days/nonzero-seconds due_at; relative date and omitted pickup -> nulls; ambiguous equipment kinds -> null. Require real tool execution, exact grounding, complete diagnostic accounting, and unchanged before/after business snapshots for each. If the first set passes, repeat it once. Stop at the FIRST failed required case, report BLOCKED and preserve evidence; do not use remaining allowance to tune or selectively rerun. READY_FOR_REVIEW requires both full sets passing on the same implementation. Six successes demonstrate bounded local evidence only, not statistical reliability or public availability.

Offline requirements: real Agent over fake transport for stage ordering and actual tool provenance; no extraction after zero-tool/error/cancel/limit; source-only schema request without tools; shared request/tool/deadline enforcement; malformed extraction and invalid evidence rejection; credential sentinel; lifecycle shutdown/caller cancellation/timeout/cleanup; terminal accounting for successful, failed and interrupted runs. Retain M1 and existing security/grounding/error/no-write coverage. Run Ruff format/lint, strict mypy, full pytest and diff whitespace check. Offline tests make no actual model calls.

Frontend e4ba9f4 stays accepted and untouched. No M2B, integration merge, deployment, final claim verification or full-product audit in this task. If this changed approach fails, return evidence for a new feasibility decision rather than another equivalent repair loop.

## Sources checked

- Installed strands-agents 1.54.0: event_loop/_retry.py; event_loop/event_loop.py; agent/agent.py; models/ollama.py.
- Installed ollama 0.6.2: _client.py.
- https://strandsagents.com/docs/user-guide/concepts/agents/structured-output/ (tool-based Agent output, deprecated older Agent helpers).
- https://strandsagents.com/docs/api/python/strands.models.ollama/ (provider interface; pinned installed source governs this implementation).
- https://docs.ollama.com/capabilities/structured-outputs (schema-format requests).
