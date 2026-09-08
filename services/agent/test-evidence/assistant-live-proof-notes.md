# BS-003 live proof notes (real local inference)

Recorded 2026-09-07. Machine-readable results: `assistant-live-proof.json`.
No raw model reasoning, no prompt dump and no personal data: every intake text
below is synthetic and was authored for this probe.

## Environment

| Component | Version / value |
|---|---|
| Ollama server | 0.33.3, listening on 127.0.0.1:11434 (already installed; shared, left running) |
| Model | `llama3.2:3b`, digest `a80c4f17acd5`, 3.2B params, Q4_K_M, capabilities include `tools` |
| Strands SDK | `strands-agents==1.54.0` with the `ollama` extra (`ollama==0.6.2`) |
| Service | uvicorn on 127.0.0.1:8210, disposable SQLite under `%TEMP%`, `BS_ASSISTANT_ENABLED=true` |
| Python | 3.11.15 |

Nothing was downloaded, no remote provider was contacted and no model was
substituted. The shared Ollama installation was not stopped or modified.

## Invocation budget

The task allows at most five real invocations. Five were used, in three runs:

| # | Run | Case | Outcome |
|---|---|---|---|
| 1 | first probe | explicit fields | HTTP 200, tool calls 1, but `borrower_label`, `pickup_location` and `due_at` all null |
| 2 | after prompt fix | explicit fields | HTTP 200, tool calls 1, `borrower_label` + `equipment_kind` + `pickup_location` grounded |
| 3 | final run | explicit fields | HTTP 200, tool calls 1, same three fields grounded, `due_at` null |
| 4 | final run | relative date | HTTP 502 ASSISTANT_INVALID_OUTPUT |
| 5 | final run | ambiguous kind | HTTP 200, tool calls 1, `equipment_kind` correctly null |

Invocation 1 showed every free-text field being rejected by grounding. The cause
was the extraction prompt, not the rules: the contract requires a value to equal
its evidence substring, and the model was returning a wider span as evidence.
The system prompt was tightened to require the value and its evidence to be
identical, with a worked example. Invocation 2 confirmed the fix.

## What the probe proves

- **Real framework and provider.** Provenance on every success reads
  `framework=strands`, `provider=ollama`, `model=llama3.2:3b`, assembled by the
  adapter from the completed run, never supplied by the model.
- **Real tool execution.** `inventory_tool_calls` was 1 on each success, taken
  from the SDK's own `metrics.tool_metrics["read_inventory"].success_count`.
- **No fabricated success.** Invocation 4 returned 502 because the model never
  called the tool. The server log records the exact reason:
  `Assistant inventory tool executions out of bounds: 0`. The adapter refused
  rather than inventing provenance, which is the behaviour the contract requires.
- **Grounding works both ways.** Explicitly stated borrower, kind and location
  were extracted; an input naming two kinds returned `equipment_kind: null` with
  `equipment_kind` in `missing_fields`, so a human decides.
- **Read-only.** The workspace snapshot was byte-identical before and after all
  interpretations: no request, loan, equipment change or event.
- **Latency.** 46.7 s on the first cold call, then 4.5 s, 4.3 s, 1.2 s and 3.2 s
  warm. All well inside the 120 s deadline.

## What the probe does NOT prove, and why

1. **`due_at` was never populated, even when the message contained a complete
   ISO-8601 timestamp with an offset.** Both successful final-run cases returned
   `due_at: null` with `due_at` in `missing_fields`. Whether the model returned
   null itself or proposed a reformatted timestamp that grounding then rejected
   was **not determined**: the adapter's field-name-only diagnostic logs at DEBUG,
   and uvicorn's `--log-level debug` configures only the `uvicorn.*` loggers, so
   the `borrowed_steps.assistant` logger never emitted it. The five-invocation
   budget was exhausted before this could be re-run, so the cause is recorded as
   unknown rather than guessed.
2. **The relative-date case never produced a draft.** It was meant to show
   "next Tuesday" coming back as `due_at: null`; it returned 502 instead. The
   clarification path is still visible in the two successful cases, which both
   returned `due_at: null` with the field listed in `missing_fields`, but the
   relative-date phrasing itself was not demonstrated end to end.
3. **Tool use is not reliable on this model.** Two of three final-run inputs
   called the tool; one did not. The SDK emitted
   `UserWarning: A ToolChoice was provided to this provider but is not supported
   and will be ignored` on the Ollama path, so Strands' forced tool choice is not
   honoured by this provider and the model is free to skip the tool. The failure
   is safe — 502, no draft, no writes — but it means roughly one input in three
   produced no suggestion in this small sample.

Sample size is three inputs. These are observations from a bounded probe, not a
measured reliability rate.
