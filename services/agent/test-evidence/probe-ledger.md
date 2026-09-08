# Live probe ledger

> This file grows by appending. The sections below are in the order they were
> written: **BS-003-R2** first, then **BS-003-R3 pass 1**, then **corrections**
> to R2 claims. Numbers stated in one section describe that task only; the
> running cumulative total is in the R3 section. Nothing earlier is edited.

## BS-003-R2 live probe ledger

Append-only machine record: `probe-ledger.jsonl` (8 entries at the time this
section was written; 14 lines now, because BS-003-R3 writes a start and an end
line per attempt). Every entry is one
real `/api/intake/interpret` call against localhost `llama3.2:3b`. Health and
snapshot reads are not inference and are not counted. No probe ran outside this
ledger. Allowance: 8 additional calls, all 8 used. Historical BS-003 total: 5.

Environment: Ollama 0.33.3 on 127.0.0.1:11434, `llama3.2:3b` (`a80c4f17acd5`),
strands-agents 1.54.0, ollama client 0.6.2, uvicorn on 127.0.0.1:8300 with a
disposable database under `%TEMP%`, `BS_ASSISTANT_ENABLED=true`,
`BS_LOG_LEVEL=INFO`. Model requests and grounding reason codes below are read
from the service's own log line, which carries field names and reason codes only.

| # | Case | Status | Time | Model requests | Tool calls | Recovery | Outcome |
|---|---|---|---|---|---|---|---|
| 1 | explicit fields (diagnostic) | 200 | 8.77 s | 3 | 1 | no | `due_at=evidence_not_in_source` — cause of the long-standing null found |
| 2 | explicit fields (diagnostic confirm) | 502 | 4.39 s | — | — | no | `StructuredOutputException`; long schema descriptions degraded the model |
| 3 | explicit fields | 200 | 4.09 s | 3 | 1 | no | all four fields grounded, `due_at` populated for the first time |
| 4 | relative date + no location | 502 | 3.84 s | — | — | no | `StructuredOutputException` |
| 5 | ambiguous kinds | 502 | 3.16 s | — | — | no | `StructuredOutputException` |
| 6 | explicit fields | 200 | 5.59 s | 4 | 1 | no | all four fields grounded, `due_at=2026-09-15T01:49:37Z` |
| 7 | relative date + no location | 200 | 3.81 s | 4 | 1 | no | `due_at` and `pickup_location` null, both reported missing |
| 8 | ambiguous kinds | 502 | 3.25 s | — | — | no | `StructuredOutputException` |

Business snapshot compared before and after every run: unchanged each time.
The recovery pass was never triggered: every completed run executed the
inventory tool on its first pass.

## What the two diagnostic probes established

Probe 1 answered the BS-003 question directly. The service logged
`due_at=evidence_not_in_source`, so the model **was** proposing a date; its
evidence substring simply was not a verbatim run from the message, and grounding
correctly refused it. The null was never an absent model field.

Probe 2 tested a fix that made things worse: long, prose-heavy field
descriptions in the extraction schema produced `StructuredOutputException`. They
were trimmed back immediately.

## What changed between probes 3-5 and 6-8

Only the extraction schema, within the grounding contract the decision allows:
short field descriptions, and all eight fields made **required and nullable**
rather than defaulted, so Ollama's constrained decoder emits the whole shape.
No grounding rule was loosened, no date was inferred, no fallback was added and
the model was not changed.

Result: `due_at` extraction went from never working to working on both attempts,
and the relative-date case went from 502 to a correct clarification.

## Honest reading of this evidence

Two of the three required final cases pass; the ambiguous-kind case failed with
`StructuredOutputException` on both attempts. The probe allowance is spent, so
this is reported as BLOCKED rather than claimed as acceptance.

The failures are safe: 502, no draft, no writes, no fabricated provenance. But
they are frequent enough that interpretation is not dependable on this model.

Probe 7 is worth reading closely, because it shows grounding doing real work
rather than the model simply returning nulls: the service logged
`pickup_location=evidence_not_in_source, due_at=parse_failure`, meaning the model
proposed both values and the rules rejected both, leaving them for a human.

Sample size is eight calls. Nothing here is a reliability measurement.

---

## BS-003-R3 pass 1, 2026-09-08 — three probes, STOPPED

Ledger entries 9-11, written as start/end pairs. Cumulative attempts after this
pass: **16 of the 19 ceiling** (5 pre-ledger BS-003 + 8 R2 + 3 here). Three of
R3's six remain and were deliberately **not** spent: the task stops at the first
failed required case.

Environment identical to R2 except the port: Ollama 0.33.3 on 127.0.0.1:11434,
`llama3.2:3b` digest `a80c4f17acd5` (re-checked against `/api/tags` before the
run), strands-agents 1.54.0, ollama 0.6.2, uvicorn on 127.0.0.1:8401, disposable
database under `%TEMP%`, `BS_ASSISTANT_ENABLED=true`, `BS_LOG_LEVEL=INFO`.
Implementation frozen before the first probe: no source file changed between the
offline PASS and these three calls.

| # | Case | Status | Time | Sends | Tool calls | Recovery | Cleanup | Grounding reason codes |
|---|---|---|---|---|---|---|---|---|
| 9 | explicit fields | 200 | 6.41 s | 3 | 1 | no | closed | `borrower_label=accepted, equipment_kind=absent_candidate, pickup_location=accepted, due_at=accepted` |
| 10 | relative date + no location | 200 | 1.27 s | 3 | 1 | no | closed | `borrower_label=accepted, equipment_kind=absent_candidate, pickup_location=absent_candidate, due_at=absent_candidate` |
| 11 | ambiguous kinds | 200 | 1.47 s | 3 | 1 | no | closed | `borrower_label=accepted, equipment_kind=ambiguous_kinds, pickup_location=accepted, due_at=absent_candidate` |

Business snapshot byte-identical before and after all three. No writes.

### Result: probe 9 FAILED its required case, so the pass stopped here

Case 1 exists to show that fields stated outright survive grounding. The message
was `Meena R needs a wheelchair, pickup at the Velachery equipment room, return
by 2026-09-15T06:25:37Z`. Three of four fields were extracted and grounded,
including `due_at`, which had never once populated before this task. But
`equipment_kind` came back **null**, with the word "wheelchair" written plainly
in the message. That is the case failing at what it is for, so this is reported
as a failure and not as an acceptance.

The reason code says where the null came from: `absent_candidate` means the
**model returned null for the field**. Grounding did not reject a proposal —
there was no proposal to reject. The same code appears on probe 10, whose
message states "a walker". So on both messages naming exactly one kind, stage
two failed to extract it.

Probe 11 is the one case where a kind candidate did appear: `ambiguous_kinds`
means the model proposed a kind and grounding refused it because the message
names two. So the ambiguity rule is genuinely working and probe 11's null is a
real pass, not the same failure wearing the same shape — a distinction only the
reason codes make visible.

### What did improve, stated no more strongly than the evidence allows

- **Zero provider failures.** 0 of 3 here against 3 of 8 under R2. Three calls
  is far too small a sample to call this a rate; it is what these three did.
- **`due_at` grounded on probe 9**, the field that motivated the whole recovery.
- **`pickup_location` grounded** on the two probes that state one.
- **Latency** fell from 3-9 s to 1.3-6.4 s. Not measured or controlled for; the
  model was already warm.
- **Accounting is now complete.** Every probe logged one terminal line carrying
  the correlation id, stage, reason, charged sends, tool attempts and successes,
  recovery use, elapsed time and cleanup result. `sends=3` on all three
  (stage one, its tool continuation, then the single extraction request) and
  `cleanup=closed` on all three, so the owned client really was released each
  time. R2 could not report any of this for a failed run.

### Diagnosis handed over, deliberately not acted on

The likeliest cause is in the extraction schema, whose description for
`equipment_kind` reads "Null if the message names no kind, or more than one."
It states only when to answer null and never states the positive case, which a
3B model appears to over-apply. Rewording it would be a one-line change.

It was **not** made. The implementation was frozen for this proof, three probes
remain, and changing the prompt and re-probing until a case passes is the
pattern this process exists to prevent. The change and the probes are left for
Codex to authorise.

---

## Corrections, appended 2026-09-08 (BS-003-R3)

Nothing above is rewritten. These are corrections to claims made in BS-003-R2,
recorded here because the claims they correct are in this file and in
`docs/workers/BS-003-R2.md`.

### 1. The "—" model-request and tool-call counters are unknown, not zero

Rows 2, 4, 5 and 8 show `—` in the *Model requests* and *Tool calls* columns.
Those runs returned 502 before the service emitted its per-run accounting line,
so **the real counts were never observed**. They are unknown. In particular they
are not zero: a `StructuredOutputException` is raised after at least one request
has been sent, so each of those four runs made **at least one** outbound model
request and possibly more. Any total computed from this table by treating `—` as
0 understates real model traffic. The table is left as it was written; this note
is how it should be read.

BS-003-R3 closes that hole: the adapter now writes one terminal accounting line
in its `finally` path, so charged sends and tool attempts are recorded for
failed runs as well as successful ones.

### 2. Withdrawn: the claim that disabling the retry strategy reduced 502s

BS-003-R2 suggested `retry_strategy=None` was causally connected to the
`StructuredOutputException` rate. That is **contradicted by the installed
source** and is withdrawn.

In `strands-agents 1.54.0`, `event_loop/_retry.py` defines
`ModelRetryStrategy.is_retryable` as
`return isinstance(exception, ModelThrottledException)` — nothing else is ever
retried by the default strategy. `StructuredOutputException` is raised on a
separate path in `event_loop/event_loop.py`, where the loop forces the
structured-output tool and gives up if the model still will not call it. The
retry setting could not have affected those failures in either direction.

`retry_strategy=None` is still set, and is still worth setting, but for the
narrower reason the code supports: it keeps throttle retries from making
outbound requests the turn limit does not bound. That is what
`tests/test_owned_transport.py` and the two retry tests in
`tests/test_assistant_bounds.py` actually demonstrate.

### 3. Corrected: "Ollama's constrained decoder" did not apply to the R2 path

The section "What changed between probes 3-5 and 6-8" says the required-and-
nullable schema helped because "Ollama's constrained decoder emits the whole
shape". That was **not true of the code as it stood at BS-003-R2**, where the
schema was passed to `Agent.invoke_async(structured_output_model=...)`. That
argument makes the SDK expose a structured-output *tool* and ask the model to
call it; it is ordinary tool calling, and it constrains nothing at the decoder.
The observed improvement is real, but the mechanism given for it was wrong, and
with a sample of two attempts per variant it is not separable from run-to-run
variation on a 3B model.

The claim becomes accurate only under the BS-003-R3 two-stage design, where
extraction goes through `OwnedOllamaModel.structured_output`, which sets
`request["format"] = output_model.model_json_schema()`. That is Ollama's own
schema parameter, and decoding is genuinely constrained by it.
