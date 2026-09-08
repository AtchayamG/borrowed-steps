# BS-003-R11-CLAUDE - the generated schema in the stage-two prompt

TASK_ID: BS-003-R11-CLAUDE
WORKER: Claude Code, senior backend developer
MODEL: claude-opus-5 (configured identifier reported by the session runtime)
EFFORT: HIGH (per-task operator attestation in the dispatch)
STATUS: READY_FOR_REVIEW (implementation and offline verification only; M2A live proof remains BLOCKED)
WORKSPACE: 00_PROGRAM_CONTROL/worktrees/BS-003-R11-claude
BRANCH: worker/claude/BS-003-R11
START_COMMIT: 23426c30cb1ecbecb73c530234aafdadf8a4d4c6
IMPLEMENTATION_COMMIT: 6bca2c31eac7004c7dcde5f1c124bc04821a8e77

## Preflight: model and effort, recorded at their real strength

The session runtime states this session is configured for `claude-opus-5`, and
the dispatch attests HIGH effort. The runtime also states plainly that the model
serving any given turn can differ from the configured one and can change
mid-session, and this environment withholds serving-model identity. What I have
is a configuration value plus a per-task operator attestation, which is what the
task allows. Nothing here is described as verified serving-model identity.
Manual worker throughout: no subagents were spawned.

## DONE

**The change.** Stage two's system prompt is now the approved extraction rules,
byte-for-byte unchanged, followed by a labelled block containing the generated
JSON schema:

```
_EXTRACTION_SCHEMA_HEADING   "Return one JSON object that validates against this
                              exact output schema. Every key listed is required;
                              use null for anything the message does not state.
                              Output schema:"
_extraction_system_prompt()  _EXTRACTION_SYSTEM_PROMPT + heading +
                             json.dumps(_Extraction.model_json_schema(), indent=2)
_EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA = _extraction_system_prompt()
```

built once at import and passed to `model.structured_output` in `_extract`.

`model_json_schema()` is the same call `OwnedOllamaModel.structured_output`
already makes to build the request's `format` parameter, so there is one schema,
generated in one place, and no second hand-maintained copy that could drift from
it. `json` was added to the module's imports; that is the only new dependency of
any kind, and it is stdlib.

**What did not change.** `_EXTRACTION_SYSTEM_PROMPT` itself, the field
descriptions, `_EXTRACTION_USER_PROMPT`, the source-only user message,
`format`, `stream=False`, temperature 0, the six-request send budget, the
two-attempt tool budget, both deadlines, cancellation ownership, grounding,
runtime validation, the HTTP body and every frozen contract. No model swap, no
retry, no fallback, no extra stage, no dependency, no server run and no
inference.

**Official guidance.** Ollama's structured-output documentation asks that the
schema be passed in the prompt as well as in `format`
(https://docs.ollama.com/capabilities/structured-outputs, checked 2026-09-08 per
the R11 decision). That is the reason for the change and the whole of the claim
being made. This report does not assert that extraction accuracy will improve;
whether it does is what a separately authorised live proof is for.

## FILES_CHANGED

| File | Change |
|---|---|
| `services/agent/src/borrowed_steps/infrastructure/strands_interpreter.py` | `import json`; `_EXTRACTION_SCHEMA_HEADING`; `_extraction_system_prompt()`; `_EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA`; `_extract` sends it |
| `services/agent/tests/test_extraction_request.py` | NEW - 6 tests reading the outgoing stage-two request |
| `services/agent/tests/test_strands_adapter.py` | One assertion retargeted to the composed prompt (see below) |
| `docs/BACKEND_SETUP.md` | New "The stage-two extraction prompt" subsection with the guidance link |

Nothing outside FILES_ALLOWED was touched: no lifecycle, transport, grounding,
domain, application, endpoint, frontend, dependency, ledger, old evidence or
program-control file. `git status` reports nothing under
`services/agent/test-evidence/`, and no new live artifact was created.

## The tests

`tests/test_extraction_request.py`, 6 tests. They read the **outgoing request**,
not the module constant, because a constant agreeing with itself would not show
what the model is sent. The transport is `ScriptedClient` from
`tests/test_cancellation.py` - the two-stage in-process fake already in the
suite - subclassed to keep a copy of every request. No model runs and no socket
is opened.

| Test | What it pins down |
|---|---|
| `test_the_outgoing_system_prompt_carries_the_generated_schema` | `format` is the generated schema, and `json.dumps(format, indent=2)` appears in the outgoing system message - serialised from the very dict that is sent, so a separately written copy could not satisfy it |
| `test_the_schema_block_in_the_prompt_parses_and_names_every_required_key` | The section after the heading parses as JSON on its own and equals `format`; all eight keys are required and described |
| `test_the_semantic_rules_are_still_the_start_of_the_prompt` | The schema is added to the approved rules, not in place of them |
| `test_the_user_message_is_still_the_source_wrapper_alone` | The user message is exactly the intake wrapper; `read_inventory`, `AVAILABLE`, `checked`, `inventory` and `schema` are all absent from it |
| `test_the_request_still_pins_the_native_schema_parameters` | `stream=False`, `format` unchanged, temperature 0, pinned model, no tools on stage two |
| `test_there_is_one_schema_and_it_is_generated` | The constant equals the generator's output and contains the schema exactly once |

The second test parses the block rather than looking for key names anywhere in
the prompt, because the worked example in the approved rules already mentions all
eight names - a substring check would have passed without the change and proved
nothing.

## Which tests actually discriminate the change

I reverted only the `_extract` wiring (back to sending
`_EXTRACTION_SYSTEM_PROMPT`), left the tests in place, ran them, then restored
the file and verified it was byte-identical:

```
2 failed, 4 passed
FAILED test_the_outgoing_system_prompt_carries_the_generated_schema
FAILED test_the_schema_block_in_the_prompt_parses_and_names_every_required_key
```

The other four pass either way **by design**: they are guard rails for the
invariants this change must not break - source-only user message, native schema
parameters, rules preserved, one generated schema - not regressions for the
change itself. Recording that distinction rather than presenting six tests as
six proofs.

## The one adapted assertion

`tests/test_strands_adapter.py::test_stage_two_makes_exactly_one_schema_request_from_the_source_alone`
asserted `call["system_prompt"] == adapter._EXTRACTION_SYSTEM_PROMPT`. It now
asserts `== adapter._EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA`. That test's subject -
that the *user* message carries the source and nothing else - is unchanged and
still asserted in full, including its leak checks. Nothing was weakened or
dropped, and the outgoing prompt itself is now examined directly in the new file.

## TESTS_RUN / TEST_RESULTS

All from `services/agent` in this worktree's own venv, created for this task:
Python 3.11.15 from the uv-managed CPython, installed from `requirements.lock`
plus a `--no-deps` editable install of this package. No other worktree's
installation was reused. Commands prefixed with `rtk`.

| Command | Result |
|---|---|
| `rtk ...ruff format --check .` | 56 files already formatted, exit 0 |
| `rtk ...ruff check .` | All checks passed!, exit 0 |
| `rtk ...mypy src tests scripts` | Success: no issues found in 52 source files, exit 0 |
| `rtk ...pytest -q` | 314 passed, 2 warnings, exit 0 |
| `rtk ...pytest -q --ignore=tests/test_extraction_request.py` | 308 passed, exit 0 - the retained baseline |
| `rtk ...pytest tests/test_extraction_request.py -q` (wiring reverted) | 2 failed, 4 passed, exit 1 - the discrimination check above |
| `git diff --check` | clean, exit 0 |

Exact counts: **308 -> 314**. Six added, all 308 retained, one of them with a
single assertion retargeted as documented. Nothing skipped, xfailed or relaxed.

**Zero inference and zero server calls.** No model was invoked, no server was
started, nothing was downloaded, no cloud, credits or spend, no credential
discovery, no external message, no provider swap. The ledger, `test-evidence/`
and every historical artifact are untouched. R10's 18/18 allocation stays closed
and this task neither reuses nor requests any allowance.

## RISK

- **The prompt more than tripled, and this is the largest risk here.** Measured:
  the rules are 1,587 characters, the serialised schema adds 3,761, and the
  stage-two system prompt goes from 1,587 to 5,518 characters. The `_Extraction`
  docstring travels inside the schema as its `description`, so that prose now
  reaches the model twice - once in `format`, once in the prompt. The adapter's
  own comment records that long prose in the schema degrades this 3B model. That
  concern applies to this addition at least as much as to the field
  descriptions, so the change could plausibly make extraction worse rather than
  better on llama3.2:3b. Only the live proof can tell, and this is the specific
  thing it should look at.
- **No accuracy claim is made or tested.** Nothing offline can tell whether
  `equipment_kind` and `due_at` stop coming back as `absent_candidate`. The
  R10 evidence is one attempt; the R8 historical cause remains UNKNOWN.
- **The change is only visible at the wire.** If a future edit passes
  `_EXTRACTION_SYSTEM_PROMPT` to `structured_output` again, two of the six new
  tests catch it; the guard rails would not.
- Rebuilding the prompt at import means a schema change is picked up on the next
  process start, not hot-reloaded. That matches how the constants around it
  already behave.

## KNOWN_ISSUES

- The extraction accuracy question from BS-003-R3 and BS-003-R10 is open. This
  task addresses one hypothesis about provider grounding and resolves nothing on
  its own.
- Installed alternatives (mistral, codellama, deepseek-coder) remain inventory
  facts only. No call, download or decision was made about them.
- The shutdown drain has still not been exercised against a real stalled model
  (carried forward, untouched here).

## ARCHITECTURE_DEVIATIONS

None. Two-stage path, exact-source grounding, real tool provenance, shared
budgets, default-disabled mode, public contracts and every frozen decision are
unchanged. One stdlib import, one module-level constant and one small function
in the file the task named.

## REVIEW_REQUEST

1. Confirm the heading wording (`_EXTRACTION_SCHEMA_HEADING`) and its placement
   after the rules rather than before them.
2. Confirm `indent=2` pretty-printed JSON rather than a compact single line.
   Given the measured 3,761-character schema, a compact dump is the cheapest
   available reduction and is a one-line change if you want it.
3. Confirm you want the `_Extraction` docstring to keep travelling inside the
   embedded schema's `description`. It is the bulk of the added length and now
   reaches the model twice. Trimming it is out of this task's scope - it would
   change descriptions the alignment decision froze - but it is the obvious
   follow-up, and I would raise it before the live proof rather than after.
4. Confirm the retargeted assertion in `test_strands_adapter.py`.

## NEXT_SAFE_ACTION

Codex reviews commit `6bca2c31eac7004c7dcde5f1c124bc04821a8e77` and the
checkpoints, then decides whether to authorise a separate bounded live proof
against this frozen change. Source and test expectations should stay frozen from
that authorisation onward. No merge, push, deployment, M2B or live allowance from
this worktree.

**M2A remains BLOCKED.**
