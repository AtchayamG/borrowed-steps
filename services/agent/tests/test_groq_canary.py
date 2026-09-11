"""Tests for the offline Groq canary preparation harness and plan verification.

Conforms strictly to docs/M3_CANARY_PREPARATION_CONTRACT.md and docs/M3_GROQ_BOUNDS_CONTRACT.md.
Tests real Strands and OpenAI SDK serialization intercepted by fail-closed transport,
verifying normal flow, refusals, ceilings, cleanup, deterministic plan hashing,
and credential/network isolation.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from strands import Agent, tool
from strands.types.agent import Limits

from borrowed_steps.infrastructure.groq_model import (
    DEFAULT_MAX_SENDS,
    GROQ_MODEL_ID,
    MAX_REQUEST_BYTES,
    GroqModel,
    GroqSendBudgetExceededError,
)

# Add scripts directory to sys.path
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from admission_probe import (  # noqa: E402
    OFFLINE_DUMMY_KEY,
    OfflineProbeTransport,
    RealSeedInventoryReader,
    make_simulated_parsed_completion,
    make_simulated_sse_text,
    make_simulated_sse_tool_call,
)
from groq_canary import (  # noqa: E402
    CANARY_FIXTURE_INPUT,
    CanaryGroundingAssertionError,
    CanaryLengthLimitError,
    CanaryThrottledError,
    CanaryToolLimitExceededError,
    CanaryToolNotExecutedError,
    compute_plan_hash,
    generate_canary_plan,
    run_canary_stages,
    run_offline_canary,
)

# ===========================================================================
# 1. Normal Tool + Extraction Flow (Happy Path)
# ===========================================================================


@pytest.mark.anyio
async def test_canary_successful_tool_and_extraction_flow() -> None:
    """Normal 3-send happy path: Stage 1 proposes & executes tool; Stage 2 extracts."""
    transport = OfflineProbeTransport()
    transport.set_scenario("test_happy_path")

    # Send 1: Stage 1 Turn 1 proposes read_inventory
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}))
    )
    # Send 2: Stage 1 Turn 2 tool result returned; conversational completion
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Checked inventory counts."))
    )
    # Send 3: Stage 2 Turn 1 extraction parse
    extraction_output = {
        "borrower_label": "Priya S",
        "equipment_kind": "crutches",
        "pickup_location": "the Adyar centre",
        "due_at": "2026-10-01T08:00:00Z",
    }
    transport.queue_response(
        httpx.Response(200, json=make_simulated_parsed_completion(extraction_output))
    )

    model = GroqModel(
        api_key=OFFLINE_DUMMY_KEY,
        max_sends=DEFAULT_MAX_SENDS,
        transport=transport,
    )

    result = await run_canary_stages(
        model=model,
        fixture_text=CANARY_FIXTURE_INPUT,
        transport=transport,
        plan_hash="mock_plan_hash",
        propagate_errors=True,
    )

    assert result.outcome == "success"
    assert result.stage_reached == "complete"
    assert result.error_category is None
    assert result.error_reason is None
    assert result.sends_total == 3
    assert result.stage_one_sends == 2
    assert result.stage_two_sends == 1
    assert result.tool_attempts == 1
    assert result.tool_successes == 1
    assert result.recovery_prompt_used is False
    assert result.model_closed is True
    assert model.client_open is False

    # Wire assertions
    assert len(result.request_wire_bytes) == 3
    for b in result.request_wire_bytes:
        assert b <= MAX_REQUEST_BYTES

    # Field assertions
    for field_name, passed in result.field_assertions.items():
        assert passed is True, f"Assertion failed: {field_name}"

    assert result.extracted_fields == extraction_output


# ===========================================================================
# 2. Omitted-Tool Handling & Recovery
# ===========================================================================


@pytest.mark.anyio
async def test_canary_omitted_tool_recovery_success() -> None:
    """Model omits tool in Turn 1; recovery prompt triggers tool execution in Turn 2."""
    transport = OfflineProbeTransport()
    transport.set_scenario("test_omitted_tool_recovery")

    # Send 1: Stage 1 Turn 1 conversational reply (no tool)
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Hello, how can I help you today?"))
    )
    # Send 2: Stage 1 Turn 2 (Recovery) proposes read_inventory
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}))
    )
    # Send 3: Stage 1 Turn 3 tool result returned; conversational completion
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Checked inventory counts."))
    )
    # Send 4: Stage 2 Turn 1 extraction parse
    extraction_output = {
        "borrower_label": "Priya S",
        "equipment_kind": "crutches",
        "pickup_location": "the Adyar centre",
        "due_at": "2026-10-01T08:00:00Z",
    }
    transport.queue_response(
        httpx.Response(200, json=make_simulated_parsed_completion(extraction_output))
    )

    model = GroqModel(
        api_key=OFFLINE_DUMMY_KEY,
        max_sends=DEFAULT_MAX_SENDS,
        transport=transport,
    )

    result = await run_canary_stages(
        model=model,
        fixture_text=CANARY_FIXTURE_INPUT,
        transport=transport,
        plan_hash="mock_plan_hash",
        propagate_errors=True,
    )

    assert result.outcome == "success"
    assert result.recovery_prompt_used is True
    assert result.tool_successes == 1
    assert result.sends_total == 4
    assert result.stage_one_sends == 3
    assert result.stage_two_sends == 1
    assert result.model_closed is True


@pytest.mark.anyio
async def test_canary_omitted_tool_refusal_fails_closed() -> None:
    """Model omits tool even after recovery prompt: Stage 2 is refused and model closes."""
    transport = OfflineProbeTransport()
    transport.set_scenario("test_omitted_tool_refusal")

    # Send 1: Stage 1 Turn 1 conversational reply (no tool)
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Hello, how can I help you today?"))
    )
    # Send 2: Stage 1 Turn 2 (Recovery) conversational reply again (still no tool)
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("I still cannot find any tool to use."))
    )

    model = GroqModel(
        api_key=OFFLINE_DUMMY_KEY,
        max_sends=DEFAULT_MAX_SENDS,
        transport=transport,
    )

    with pytest.raises(CanaryToolNotExecutedError, match="without executing read_inventory tool"):
        await run_canary_stages(
            model=model,
            fixture_text=CANARY_FIXTURE_INPUT,
            transport=transport,
            plan_hash="mock_plan_hash",
            propagate_errors=True,
        )

    # Owned model cleanup guaranteed
    assert model.client_open is False


# ===========================================================================
# 3. Third Tool Call Refusal
# ===========================================================================


@pytest.mark.anyio
async def test_canary_third_tool_call_refused() -> None:
    """Stage 1 attempts more than 2 tool calls; harness raises CanaryToolLimitExceededError."""
    transport = OfflineProbeTransport()
    transport.set_scenario("test_third_tool_call")

    # Turn 1: tool call 1
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}, call_id="c1"))
    )
    # Turn 2: tool call 2
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}, call_id="c2"))
    )
    # Turn 3: tool call 3 (forbidden!)
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}, call_id="c3"))
    )
    # Turn 4: text after tool failure
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Completed checking."))
    )

    model = GroqModel(
        api_key=OFFLINE_DUMMY_KEY,
        max_sends=DEFAULT_MAX_SENDS,
        transport=transport,
    )

    with pytest.raises(CanaryToolLimitExceededError, match="exceeding ceiling of 2"):
        await run_canary_stages(
            model=model,
            fixture_text=CANARY_FIXTURE_INPUT,
            transport=transport,
            plan_hash="mock_plan_hash",
            propagate_errors=True,
        )

    assert model.client_open is False


# ===========================================================================
# 4. Invalid Source Field & Grounding Assertions
# ===========================================================================


@pytest.mark.anyio
async def test_canary_invalid_source_field_refused() -> None:
    """Stage 2 produces an unquoted or hallucinated field; grounding assertion fails closed."""
    transport = OfflineProbeTransport()
    transport.set_scenario("test_invalid_source_field")

    # Stage 1: tool call 1 and completion
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}))
    )
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Checked inventory counts."))
    )
    # Stage 2: Hallucinated borrower_label not in source text
    hallucinated_output = {
        "borrower_label": "John Doe",  # NOT in "Priya S wants crutches..."
        "equipment_kind": "crutches",
        "pickup_location": "the Adyar centre",
        "due_at": "2026-10-01T08:00:00Z",
    }
    transport.queue_response(
        httpx.Response(200, json=make_simulated_parsed_completion(hallucinated_output))
    )

    model = GroqModel(
        api_key=OFFLINE_DUMMY_KEY,
        max_sends=DEFAULT_MAX_SENDS,
        transport=transport,
    )

    with pytest.raises(CanaryGroundingAssertionError, match="not present in source text"):
        await run_canary_stages(
            model=model,
            fixture_text=CANARY_FIXTURE_INPUT,
            transport=transport,
            plan_hash="mock_plan_hash",
            propagate_errors=True,
        )

    assert model.client_open is False


# ===========================================================================
# 5. Length Truncation Failure Without Retry
# ===========================================================================


@pytest.mark.anyio
async def test_canary_length_failure_refused_without_retry() -> None:
    """Stage 2 receives length finish reason; harness fails explicitly without retry."""
    transport = OfflineProbeTransport()
    transport.set_scenario("test_length_failure")

    # Stage 1: tool call 1 and completion
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}))
    )
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Checked inventory counts."))
    )
    # Stage 2: Length finish reason payload
    truncated_completion = {
        "id": "chatcmpl-sim-length",
        "object": "chat.completion",
        "created": 123456789,
        "model": GROQ_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": '{"borrower_label": "Priya',
                    "refusal": None,
                },
                "finish_reason": "length",
            }
        ],
    }
    transport.queue_response(httpx.Response(200, json=truncated_completion))

    model = GroqModel(
        api_key=OFFLINE_DUMMY_KEY,
        max_sends=DEFAULT_MAX_SENDS,
        transport=transport,
    )

    with pytest.raises(CanaryLengthLimitError, match="truncated due to length limit"):
        await run_canary_stages(
            model=model,
            fixture_text=CANARY_FIXTURE_INPUT,
            transport=transport,
            plan_hash="mock_plan_hash",
            propagate_errors=True,
        )

    assert model.client_open is False


# ===========================================================================
# 6. Simulated 429 Without Retry
# ===========================================================================


@pytest.mark.anyio
async def test_canary_simulated_429_fails_without_retry() -> None:
    """Stage 2 receives HTTP 429; fails immediately as throttled without retry."""
    transport = OfflineProbeTransport()
    transport.set_scenario("test_429_throttling")

    # Stage 1: tool call 1 and completion
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}))
    )
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Checked inventory counts."))
    )
    # Stage 2: 429 Rate Limit
    transport.queue_response(
        httpx.Response(
            429,
            headers={"Retry-After": "5"},
            json={"error": {"message": "Rate limit reached"}},
        )
    )

    model = GroqModel(
        api_key=OFFLINE_DUMMY_KEY,
        max_sends=DEFAULT_MAX_SENDS,
        transport=transport,
    )

    with pytest.raises(
        CanaryThrottledError, match="Simulated 429 rate limit received without retry"
    ):
        await run_canary_stages(
            model=model,
            fixture_text=CANARY_FIXTURE_INPUT,
            transport=transport,
            plan_hash="mock_plan_hash",
            propagate_errors=True,
        )

    assert model.client_open is False


# ===========================================================================
# 7. Shared Sixth & Seventh Send Bound
# ===========================================================================


@pytest.mark.anyio
async def test_canary_shared_sixth_send_bound_and_seventh_refusal() -> None:
    """Multi-turn conversation exhausting exactly 6 sends; 7th send is refused by sticky budget."""
    transport = OfflineProbeTransport()
    transport.set_scenario("test_six_send_bound")

    # Send 1: Stage 1 Turn 1 no tool
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Hello, how can I help?"))
    )
    # Send 2: Stage 1 Turn 2 recovery tool call 1
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}, call_id="c1"))
    )
    # Send 3: Stage 1 Turn 3 tool call 2
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}, call_id="c2"))
    )
    # Send 4: Stage 1 Turn 4 conversational finish
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Inventory check finished."))
    )
    # Send 5: Stage 2 structured output receives 429
    transport.queue_response(
        httpx.Response(
            429,
            headers={"Retry-After": "2"},
            json={"error": {"message": "Rate limit"}},
        )
    )
    # Send 6: Stage 2 extraction succeeds
    canary_extraction_output = {
        "borrower_label": "Priya S",
        "equipment_kind": "crutches",
        "pickup_location": "the Adyar centre",
        "due_at": "2026-10-01T08:00:00Z",
    }
    transport.queue_response(
        httpx.Response(200, json=make_simulated_parsed_completion(canary_extraction_output))
    )

    model = GroqModel(
        api_key=OFFLINE_DUMMY_KEY,
        max_sends=DEFAULT_MAX_SENDS,
        transport=transport,
    )

    # We execute the 6 sends using model stream and structured_output
    inv_reader = RealSeedInventoryReader()

    @tool(name="read_inventory", description="Counts equipment")
    def read_inventory() -> dict[str, Any]:
        return {
            "counts": [
                {"kind": r.kind, "state": r.state, "count": r.count}
                for r in inv_reader.kind_state_counts()
            ]
        }

    agent = Agent(
        model=model,
        tools=[read_inventory],
        callback_handler=None,
        load_tools_from_directory=False,
    )
    # Stage 1 Turn 1: Send 1 (no tool reply)
    await agent.invoke_async("test prompt", limits=Limits(turns=4))
    assert model.sent == 1

    # Stage 1 Recovery: Send 2 (tool call 1), Send 3 (tool call 2), Send 4 (finish)
    await agent.invoke_async("Please call read_inventory now.", limits=Limits(turns=4))
    assert model.sent == 4

    # Send 5: Throttled send
    stage2_prompt: Any = [{"role": "user", "content": [{"text": "Priya S wants crutches..."}]}]
    from strands.types.exceptions import ModelThrottledException

    with pytest.raises(ModelThrottledException):
        async for _ in model.structured_output(from_import_extraction(), stage2_prompt):
            pass
    assert model.sent == 5

    # Send 6: Successful send (budget reaches 6/6)
    async for _ in model.structured_output(from_import_extraction(), stage2_prompt):
        pass
    assert model.sent == 6
    assert model.budget.exhausted is True

    # Attempt 7th send: MUST be refused upfront by sticky budget without dispatching
    with pytest.raises(GroqSendBudgetExceededError, match="Send budget exhausted: 6/6"):
        async for _ in model.structured_output(from_import_extraction(), stage2_prompt):
            pass

    assert model.sent == 6  # Did not charge 7th debit
    await model.aclose()


def from_import_extraction() -> type[Any]:
    from borrowed_steps.infrastructure.strands_interpreter import _Extraction

    return _Extraction


# ===========================================================================
# 8. Owned Model Cleanup on All Failure Modes
# ===========================================================================


@pytest.mark.anyio
async def test_canary_model_cleanup_on_all_failures() -> None:
    """Verify that model.aclose() is invoked and client_open is False across failure modes."""
    failures = [
        # (Scenario label, list of responses, expected exception)
        (
            "omitted_tool",
            [
                httpx.Response(200, text=make_simulated_sse_text("Hello")),
                httpx.Response(200, text=make_simulated_sse_text("Still no tool")),
            ],
            CanaryToolNotExecutedError,
        ),
        (
            "throttling",
            [
                httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {})),
                httpx.Response(200, text=make_simulated_sse_text("Done")),
                httpx.Response(429, json={"error": {"message": "Rate limit"}}),
            ],
            CanaryThrottledError,
        ),
    ]

    for label, responses, exc_type in failures:
        transport = OfflineProbeTransport()
        transport.set_scenario(label)
        for r in responses:
            transport.queue_response(r)

        model = GroqModel(
            api_key=OFFLINE_DUMMY_KEY,
            max_sends=DEFAULT_MAX_SENDS,
            transport=transport,
        )

        with pytest.raises(exc_type):
            await run_canary_stages(
                model=model,
                fixture_text=CANARY_FIXTURE_INPUT,
                transport=transport,
                plan_hash="mock_hash",
                propagate_errors=True,
            )

        assert model.client_open is False, f"Model was not closed after failure in {label}"


# ===========================================================================
# 9. Deterministic Plan Hashing & Invalidation
# ===========================================================================


def test_deterministic_plan_hashing_and_invalidation(tmp_path: Path) -> None:
    """Plan hash is deterministic and changes when any source, lock, or fixture changes."""
    # Generate canonical plan from real repo
    plan1 = generate_canary_plan()
    plan2 = generate_canary_plan()

    assert plan1["plan_hash"] == plan2["plan_hash"]
    assert len(plan1["plan_hash"]) == 64

    # Adding generated evidence to plan dictionary does NOT change plan_hash
    plan_with_evidence = copy.deepcopy(plan1)
    plan_with_evidence["evidence"] = {"some": "evidence", "outcome": "success"}
    assert compute_plan_hash(plan_with_evidence) == plan1["plan_hash"]

    # 1. Changing candidate fixture invalidates plan_hash
    alt_fixture = "Anand Kumar wants a wheelchair from St Johns."
    plan_alt_fixture = generate_canary_plan(fixture_text=alt_fixture)
    assert plan_alt_fixture["plan_hash"] != plan1["plan_hash"]

    # 2. Modifying a source file invalidates plan_hash
    fake_base = tmp_path / "fake_agent"
    infra_dir = fake_base / "src" / "borrowed_steps" / "infrastructure"
    infra_dir.mkdir(parents=True)

    # Copy real files into fake base
    canonical_paths = {
        "groq_model.py": infra_dir / "groq_model.py",
        "strands_interpreter.py": infra_dir / "strands_interpreter.py",
        "requirements-groq.lock": fake_base / "requirements-groq.lock",
        "requirements.lock": fake_base / "requirements.lock",
    }
    real_repo_base = Path(__file__).resolve().parent.parent
    real_infra = real_repo_base / "src" / "borrowed_steps" / "infrastructure"
    canonical_paths["groq_model.py"].write_bytes((real_infra / "groq_model.py").read_bytes())
    canonical_paths["strands_interpreter.py"].write_bytes(
        (real_infra / "strands_interpreter.py").read_bytes()
    )
    canonical_paths["requirements-groq.lock"].write_bytes(
        (real_repo_base / "requirements-groq.lock").read_bytes()
    )
    canonical_paths["requirements.lock"].write_bytes(
        (real_repo_base / "requirements.lock").read_bytes()
    )

    plan_copied = generate_canary_plan(base_dir=fake_base)
    assert plan_copied["plan_hash"] == plan1["plan_hash"]

    # Now mutate groq_model.py in fake base
    canonical_paths["groq_model.py"].write_text("# mutated\n", encoding="utf-8")
    plan_mutated = generate_canary_plan(base_dir=fake_base)
    assert plan_mutated["plan_hash"] != plan1["plan_hash"]
    assert plan_mutated["input_hashes"]["groq_model.py"] != plan1["input_hashes"]["groq_model.py"]


# ===========================================================================
# 10. Credential & Network Isolation
# ===========================================================================


@pytest.mark.anyio
async def test_credential_and_network_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Harness and CLI ignore environment credentials and fail closed on live egress."""
    # Inject real-looking credentials and live base URLs into the environment
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_looking_secret_key_abcdef123456")
    monkeypatch.setenv("OPENAI_API_KEY", "sk_other_real_looking_secret_key_987654")
    monkeypatch.setenv("GROQ_BASE_URL", "https://api.openai.com/v1")

    # 1. Verify offline canary run completes cleanly without reading environment keys
    _plan, result = await run_offline_canary()
    assert result.outcome == "success"
    assert result.model_closed is True

    # 2. Verify OfflineProbeTransport rejects live network sockets and non-Groq hosts
    transport = OfflineProbeTransport()
    live_req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions", content=b"{}")
    with pytest.raises(RuntimeError, match="LIVE EGRESS ATTEMPT REFUSED"):
        await transport.handle_async_request(live_req)

    # 3. Verify CLI arguments reject --live flag
    from groq_canary import main

    monkeypatch.setattr(sys, "argv", ["groq_canary.py", "--live"])
    with pytest.raises(SystemExit):
        main()


# ===========================================================================
# 11. Full Artifact Generation & Schema Validation
# ===========================================================================


@pytest.mark.anyio
async def test_run_offline_canary_generates_valid_artifacts(tmp_path: Path) -> None:
    """run_offline_canary writes valid canary_plan.json and canary_evidence.json."""
    output_dir = tmp_path / "evidence_bs020"
    plan, _result = await run_offline_canary(output_dir=output_dir)

    plan_file = output_dir / "canary_plan.json"
    evidence_file = output_dir / "canary_evidence.json"

    assert plan_file.exists()
    assert evidence_file.exists()

    with open(plan_file, encoding="utf-8") as f:
        plan_data = json.load(f)

    with open(evidence_file, encoding="utf-8") as f:
        evidence_data = json.load(f)

    # Plan checks
    assert plan_data["plan_hash"] == plan["plan_hash"]
    assert plan_data["live_authorized"] is False
    assert plan_data["ceilings"]["max_sends"] == 6
    assert plan_data["ceilings"]["max_request_bytes"] == 16384
    assert plan_data["ceilings"]["max_completion_tokens"] == 1024
    assert plan_data["ceilings"]["reasoning_effort"] == "low"
    assert plan_data["retry_policy"]["max_retries"] == 0
    assert plan_data["retry_policy"]["auto_retry"] is False

    # Evidence checks
    assert evidence_data["provenance"] == "offline_fixture"
    assert evidence_data["outcome"] == "success"
    assert evidence_data["model_closed"] is True
    assert evidence_data["usage"] == "fixture data"
    assert evidence_data["token_accounting"]["input_tokens"] == "unknown"
    assert evidence_data["sends_total"] == 3
    assert evidence_data["tool_successes"] == 1
    assert evidence_data["max_request_wire_bytes"] <= 16384

    # Security check: no secret keys or prompt prose in evidence
    evidence_str = json.dumps(evidence_data)
    assert "gsk_" not in evidence_str
    assert "sk-" not in evidence_str
    assert "Bearer " not in evidence_str
