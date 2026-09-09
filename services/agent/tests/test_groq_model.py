"""Tests for GroqModel transport adapter under Strands OpenAIModel.

Exercises:
1. Strict constructor argument validation and pinned constants.
2. Target URL verification (refusal of non-HTTPS, wrong host, or wrong endpoint).
3. Monotonic operation deadline expiration (pre-send, during dispatch, and mid-stream).
4. Sticky send budget enforcement across both stream() and structured_output().
5. Real Strands Agent tool loop execution with synthetic read_inventory.
6. Retry/continuation bounds and budget exhaustion.
7. Stage 2 structured extraction with _Extraction schema (wire inspection and parse handling).
8. Preservation of HTTP 429 status code and Retry-After header.
9. Cancellation and clean resource closing.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from strands import Agent, tool
from strands.types.exceptions import ModelThrottledException

from borrowed_steps.infrastructure.groq_model import (
    ALLOWED_HOST,
    ALLOWED_PATH,
    DEFAULT_MAX_SENDS,
    DEFAULT_OPERATION_DEADLINE_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    GROQ_BASE_URL,
    GROQ_MODEL_ID,
    GroqDeadlineExpiredError,
    GroqModel,
    GroqModelError,
    GroqRequestTimeoutError,
    GroqSendBudgetExceededError,
    GroqTargetRefusedError,
    SendBudget,
)
from borrowed_steps.infrastructure.strands_interpreter import _Extraction

DUMMY_KEY = "gsk_test_mock_secret_key_12345"


def _make_sse_tool_call(tool_name: str, arguments: dict[str, Any], call_id: str = "call_1") -> str:
    """Build OpenAI-compatible SSE text stream proposing a tool call."""
    chunk1 = {
        "id": "chatcmpl-tool-1",
        "object": "chat.completion.chunk",
        "created": 1725880000,
        "model": GROQ_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": None,
            }
        ],
    }
    chunk2 = {
        "id": "chatcmpl-tool-1",
        "object": "chat.completion.chunk",
        "created": 1725880000,
        "model": GROQ_MODEL_ID,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    return f"data: {json.dumps(chunk1)}\n\ndata: {json.dumps(chunk2)}\n\ndata: [DONE]\n\n"


def _make_sse_text_response(text: str) -> str:
    """Build OpenAI-compatible SSE text stream delivering an assistant message."""
    chunk = {
        "id": "chatcmpl-text-1",
        "object": "chat.completion.chunk",
        "created": 1725880000,
        "model": GROQ_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


def _make_parsed_response(
    content: dict[str, Any] | None, refusal: str | None = None
) -> dict[str, Any]:
    """Build OpenAI-compatible chat completion JSON for structured output."""
    message: dict[str, Any] = {
        "role": "assistant",
        "content": json.dumps(content) if content is not None else None,
        "refusal": refusal,
    }
    return {
        "id": "chatcmpl-parsed-1",
        "object": "chat.completion",
        "created": 1725880000,
        "model": GROQ_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
    }


# ===========================================================================
# 1. Construction & Validation Tests
# ===========================================================================


def test_groq_model_construction_validation() -> None:
    """Constructor validates explicit arguments and limits without network calls."""
    with pytest.raises(ValueError, match="api_key must be a non-empty string"):
        GroqModel(api_key="")

    with pytest.raises(ValueError, match="api_key must be a non-empty string"):
        GroqModel(api_key="   ")

    with pytest.raises(ValueError, match="api_key must be a non-empty string"):
        GroqModel(api_key=None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="max_sends must be between 1 and 6"):
        GroqModel(api_key=DUMMY_KEY, max_sends=7)

    with pytest.raises(ValueError, match="max_sends must be between 1 and 6"):
        GroqModel(api_key=DUMMY_KEY, max_sends=0)

    with pytest.raises(ValueError, match="operation_deadline_seconds must be between"):
        GroqModel(api_key=DUMMY_KEY, operation_deadline_seconds=111.0)

    with pytest.raises(ValueError, match="operation_deadline_seconds must be between"):
        GroqModel(api_key=DUMMY_KEY, operation_deadline_seconds=0.0)

    with pytest.raises(ValueError, match="request_timeout_seconds must be between"):
        GroqModel(api_key=DUMMY_KEY, request_timeout_seconds=61.0)

    with pytest.raises(ValueError, match="request_timeout_seconds must be between"):
        GroqModel(api_key=DUMMY_KEY, request_timeout_seconds=0.0)

    # Valid instance with injectable reductions
    model = GroqModel(
        api_key=DUMMY_KEY,
        max_sends=3,
        operation_deadline_seconds=45.0,
        request_timeout_seconds=15.0,
    )
    assert model.sent == 0
    assert not model.exhausted
    assert model.budget.limit == 3
    assert model.time_remaining <= 45.0
    assert model.client_open
    assert GROQ_BASE_URL == "https://api.groq.com/openai/v1"
    assert GROQ_MODEL_ID == "openai/gpt-oss-20b"
    assert DEFAULT_MAX_SENDS == 6
    assert DEFAULT_OPERATION_DEADLINE_SECONDS == 110.0
    assert DEFAULT_REQUEST_TIMEOUT_SECONDS == 60.0


def test_send_budget_unit_behavior() -> None:
    """SendBudget behaves stickily and fails closed on exhaustion."""
    with pytest.raises(ValueError, match="Budget limit must be at least 1"):
        SendBudget(limit=0)

    b = SendBudget(limit=2)
    assert b.sent == 0
    assert b.remaining == 2

    b.charge()
    assert b.sent == 1
    assert b.remaining == 1

    b.charge()
    assert b.sent == 2
    assert b.remaining == 0
    assert b.exhausted

    with pytest.raises(GroqSendBudgetExceededError, match="Send budget exhausted"):
        b.charge()

    assert b.exhausted
    assert b.sent == 2


# ===========================================================================
# 2. Target URL Refusal Tests
# ===========================================================================


def test_groq_model_target_refusal_non_https() -> None:
    """Requests using http:// instead of https:// fail closed immediately."""

    async def _test() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        # Dispatch request with http scheme
        req = httpx.Request("POST", f"http://{ALLOWED_HOST}{ALLOWED_PATH}")
        with pytest.raises(
            GroqTargetRefusedError, match=re.escape("Request target refused: http://")
        ):
            await model._groq_transport.handle_async_request(req)

    asyncio.run(_test())


def test_groq_model_target_refusal_wrong_host_or_path() -> None:
    """Requests targeting unauthorized hosts or paths fail closed."""

    async def _test() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        # Wrong host
        req1 = httpx.Request("POST", f"https://api.openai.com{ALLOWED_PATH}")
        expected_host_err = re.escape("Request target refused: https://api.openai.com")
        with pytest.raises(GroqTargetRefusedError, match=expected_host_err):
            await model._groq_transport.handle_async_request(req1)

        # Wrong path
        req2 = httpx.Request("POST", f"https://{ALLOWED_HOST}/openai/v1/models")
        expected_path_err = re.escape(
            "Request target refused: https://api.groq.com/openai/v1/models"
        )
        with pytest.raises(GroqTargetRefusedError, match=expected_path_err):
            await model._groq_transport.handle_async_request(req2)

    asyncio.run(_test())


# ===========================================================================
# 3. Monotonic Deadline Expiration Tests
# ===========================================================================


def test_groq_model_deadline_expired_before_send() -> None:
    """Expired monotonic deadline fails closed before wire dispatch."""

    async def _test() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler)
        model = GroqModel(
            api_key=DUMMY_KEY,
            operation_deadline_seconds=0.01,
            transport=transport,
        )
        # Wait for the tight deadline to expire
        await asyncio.sleep(0.02)
        assert model.time_remaining == 0.0

        prompt: Any = [{"role": "user", "content": [{"text": "hello"}]}]
        with pytest.raises(GroqDeadlineExpiredError, match="Operation deadline expired before"):
            async for _ in model.stream(prompt):
                pass

        with pytest.raises(GroqDeadlineExpiredError, match="Operation deadline expired before"):
            async for _ in model.structured_output(_Extraction, prompt):
                pass

        assert model.sent == 0

    asyncio.run(_test())


def test_groq_model_deadline_expired_during_dispatch() -> None:
    """Stalled dispatch exceeding deadline raises GroqDeadlineExpiredError."""

    async def _test() -> None:
        async def slow_handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.05)
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(slow_handler)
        model = GroqModel(
            api_key=DUMMY_KEY,
            operation_deadline_seconds=0.02,
            transport=transport,
        )

        prompt: Any = [{"role": "user", "content": [{"text": "hello"}]}]
        with pytest.raises(GroqDeadlineExpiredError):
            async for _ in model.stream(prompt):
                pass

    asyncio.run(_test())


def test_groq_model_deadline_expired_mid_stream_chunk_read() -> None:
    """Monotonic deadline aborts and cleans up stalled SSE byte stream mid-read."""

    async def _test() -> None:
        async def stalled_byte_stream() -> AsyncIterator[bytes]:
            yield b"data: {}\n\n"
            # Stall until deadline definitely passes
            await asyncio.sleep(0.06)
            yield b"data: [DONE]\n\n"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=stalled_byte_stream(),
            )

        transport = httpx.MockTransport(handler)
        model = GroqModel(
            api_key=DUMMY_KEY,
            operation_deadline_seconds=0.03,
            transport=transport,
        )

        prompt: Any = [{"role": "user", "content": [{"text": "hello"}]}]
        match_msg = "Operation deadline expired while streaming"
        with pytest.raises(GroqDeadlineExpiredError, match=match_msg):
            async for _ in model.stream(prompt):
                pass

    asyncio.run(_test())


def test_groq_model_request_timeout_within_deadline() -> None:
    """Per-request timeout triggers GroqRequestTimeoutError when deadline has remaining time."""

    async def _test() -> None:
        async def slow_handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.05)
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(slow_handler)
        model = GroqModel(
            api_key=DUMMY_KEY,
            operation_deadline_seconds=10.0,  # deadline remains plenty
            request_timeout_seconds=0.02,  # request timeout is small
            transport=transport,
        )

        req = httpx.Request("POST", f"{GROQ_BASE_URL}/chat/completions")
        with pytest.raises(GroqRequestTimeoutError, match="Request dispatch timed out after"):
            await model._groq_transport.handle_async_request(req)

    asyncio.run(_test())


# ===========================================================================
# 4. Sticky Send Budget Across Operations
# ===========================================================================


def test_groq_model_sticky_send_budget_shared_across_stages() -> None:
    """Send budget is strictly bounded and shared across stream() and structured_output()."""

    async def _test() -> None:
        sse_text = _make_sse_text_response("Hello world")
        parsed_json = _make_parsed_response(
            {
                "borrower_label": "Alice",
                "equipment_kind": "wheelchair",
                "pickup_location": "Shed",
                "due_at": "2026-09-14T09:00:00Z",
            }
        )

        send_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal send_count
            send_count += 1
            if "response_format" in json.loads(request.content.decode("utf-8")):
                return httpx.Response(200, json=parsed_json)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse_text)

        transport = httpx.MockTransport(handler)
        # Budget limited to 3 sends
        model = GroqModel(api_key=DUMMY_KEY, max_sends=3, transport=transport)
        prompt: Any = [{"role": "user", "content": [{"text": "Request"}]}]

        # Send 1 (stream)
        async for _ in model.stream(prompt):
            pass
        assert model.sent == 1
        assert model.budget.remaining == 2

        # Send 2 (structured_output)
        async for _ in model.structured_output(_Extraction, prompt):
            pass
        assert model.sent == 2
        assert model.budget.remaining == 1

        # Send 3 (stream) - consumes final budget slot
        async for _ in model.stream(prompt):
            pass
        assert model.sent == 3
        assert model.exhausted
        assert model.budget.remaining == 0

        # Send 4 (stream attempt) - rejected
        with pytest.raises(GroqSendBudgetExceededError, match="Send budget exhausted: 3/3"):
            async for _ in model.stream(prompt):
                pass

        # Send 5 (structured_output attempt) - sticky rejection
        with pytest.raises(GroqSendBudgetExceededError, match="Send budget exhausted: 3/3"):
            async for _ in model.structured_output(_Extraction, prompt):
                pass

        assert send_count == 3
        assert model.sent == 3
        assert model.exhausted

    asyncio.run(_test())


# ===========================================================================
# 5. Real Strands Agent Tool Loop & Wire Inspection
# ===========================================================================


def test_groq_model_strands_agent_tool_loop_and_wire_inspection(tmp_path: Path) -> None:
    """Stage 1: real Strands Agent executes read_inventory tool via intercepted stream.

    Verifies:
    1. The tool is actually executed by the SDK/Agent, not called directly.
    2. SDK issues turn 1 proposing tool call, runs tool, and turn 2 continuation sends tool result.
    3. Exactly 2 wire sends are charged.
    4. Outgoing wire format in Stage 1 includes tools, stream: True, and no response_format.
    """

    async def _test() -> None:
        tool_executed = False

        @tool(name="read_inventory", description="Counts of equipment in room.")
        def read_inventory() -> str:
            nonlocal tool_executed
            tool_executed = True
            payload = {"counts": [{"kind": "WHEELCHAIR", "state": "AVAILABLE", "count": 2}]}
            return json.dumps(payload)

        stream_turn1 = _make_sse_tool_call("read_inventory", {})
        stream_turn2 = _make_sse_text_response("We have 2 available wheelchairs.")

        intercepted_requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            intercepted_requests.append(request)
            if len(intercepted_requests) == 1:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text=stream_turn1,
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=stream_turn2,
            )

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        agent = Agent(
            model=model,
            tools=[read_inventory],
            system_prompt="You are a helpful inventory assistant.",
            load_tools_from_directory=False,
        )

        result = await agent.invoke_async("Do we have any wheelchairs available?")

        # 1. Tool execution provenance
        assert tool_executed is True
        assert "2 available wheelchairs" in str(result.message)

        # 2. Exactly 2 wire sends
        assert len(intercepted_requests) == 2
        assert model.sent == 2

        # 3. Inspect Stage 1 Turn 1 Wire JSON
        wire_turn1 = json.loads(intercepted_requests[0].content.decode("utf-8"))
        assert wire_turn1["model"] == GROQ_MODEL_ID
        assert wire_turn1["stream"] is True
        assert "tools" in wire_turn1
        assert len(wire_turn1["tools"]) == 1
        assert wire_turn1["tools"][0]["function"]["name"] == "read_inventory"
        assert "response_format" not in wire_turn1

        # 4. Inspect Stage 1 Turn 2 Wire JSON
        wire_turn2 = json.loads(intercepted_requests[1].content.decode("utf-8"))
        assert wire_turn2["model"] == GROQ_MODEL_ID
        assert wire_turn2["stream"] is True
        roles = [m["role"] for m in wire_turn2["messages"]]
        assert "tool" in roles

    asyncio.run(_test())


# ===========================================================================
# 6. Retry / Continuation Bounds & Exhaustion
# ===========================================================================


def test_groq_model_provider_error_retry_exhaustion() -> None:
    """Repeated provider failures trigger wire sends that exhaust budget and fail closed."""

    async def _test() -> None:
        attempts = 0

        async def error_handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(500, json={"error": {"message": "Internal Server Error"}})

        transport = httpx.MockTransport(error_handler)
        # Budget limited to 2 sends
        model = GroqModel(api_key=DUMMY_KEY, max_sends=2, transport=transport)
        prompt: Any = [{"role": "user", "content": [{"text": "Hello"}]}]

        # Send 1 fails with 500
        with pytest.raises(GroqModelError, match="Groq provider request failed"):
            async for _ in model.stream(prompt):
                pass

        assert model.sent == 1
        assert model.budget.remaining == 1

        # Send 2 (retry attempt) fails with 500 and exhausts budget
        with pytest.raises(GroqModelError, match="Groq provider request failed"):
            async for _ in model.stream(prompt):
                pass

        assert model.sent == 2
        assert model.exhausted

        # Send 3 is blocked by sticky budget exhaustion
        with pytest.raises(GroqSendBudgetExceededError, match="Send budget exhausted: 2/2"):
            async for _ in model.stream(prompt):
                pass

        assert attempts == 2
        assert model.sent == 2

    asyncio.run(_test())


# ===========================================================================
# 7. Stage 2 Structured Output with _Extraction Schema
# ===========================================================================


def test_groq_model_stage2_structured_output_wire_shape_and_parsing() -> None:
    """Stage 2: structured_output with _Extraction enforces strict json_schema wire shape."""

    async def _test() -> None:
        valid_extraction_data = {
            "borrower_label": "Deepa K",
            "equipment_kind": "crutches",
            "pickup_location": "Main Hall",
            "due_at": "2026-09-15T10:00:00Z",
        }
        parsed_json = _make_parsed_response(valid_extraction_data)

        captured_requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(200, json=parsed_json)

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        intake_text = "Deepa K needs crutches at Main Hall due 2026-09-15T10:00:00Z"
        prompt: Any = [{"role": "user", "content": [{"text": intake_text}]}]

        results: list[dict[str, Any]] = []
        async for item in model.structured_output(
            _Extraction, prompt, system_prompt="Extract loan info"
        ):
            results.append(item)

        assert len(results) == 1
        output = results[0]["output"]
        assert isinstance(output, _Extraction)
        assert output.borrower_label == "Deepa K"
        assert output.equipment_kind == "crutches"
        assert output.pickup_location == "Main Hall"
        assert output.due_at == "2026-09-15T10:00:00Z"

        # Wire shape inspection
        assert len(captured_requests) == 1
        wire_json = json.loads(captured_requests[0].content.decode("utf-8"))

        # Must NOT have tools or tool_choice
        assert "tools" not in wire_json
        assert "tool_choice" not in wire_json
        # Must NOT have streaming fields
        assert "stream_options" not in wire_json
        assert wire_json["stream"] is False

        # Must have strict response_format
        assert "response_format" in wire_json
        rf = wire_json["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["strict"] is True
        assert rf["json_schema"]["name"] == "_Extraction"

        schema = rf["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        # All nullable fields required
        assert set(schema["required"]) == {
            "borrower_label",
            "equipment_kind",
            "pickup_location",
            "due_at",
        }

    asyncio.run(_test())


def test_groq_model_structured_output_null_fields_handling() -> None:
    """Unstated fields returned as null parse cleanly into None attributes."""

    async def _test() -> None:
        null_data = {
            "borrower_label": "Sanjay",
            "equipment_kind": None,
            "pickup_location": None,
            "due_at": None,
        }
        parsed_json = _make_parsed_response(null_data)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=parsed_json)

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        prompt: Any = [{"role": "user", "content": [{"text": "Sanjay is asking for something"}]}]
        results = [item async for item in model.structured_output(_Extraction, prompt)]
        assert len(results) == 1
        output = results[0]["output"]
        assert isinstance(output, _Extraction)
        assert output.borrower_label == "Sanjay"
        assert output.equipment_kind is None
        assert output.pickup_location is None
        assert output.due_at is None

    asyncio.run(_test())


def test_groq_model_structured_output_refusal_handling() -> None:
    """Model refusal in structured output raises ValueError."""

    async def _test() -> None:
        parsed_json = _make_parsed_response(None, refusal="Cannot parse message content.")

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=parsed_json)

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        prompt: Any = [{"role": "user", "content": [{"text": "gibberish"}]}]
        match_refusal = "Model refused structured output"
        with pytest.raises(ValueError, match=match_refusal):
            async for _ in model.structured_output(_Extraction, prompt):
                pass

    asyncio.run(_test())


def test_groq_model_structured_output_invalid_json_handling() -> None:
    """Invalid JSON response raises ValidationError from Pydantic parser."""

    async def _test() -> None:
        invalid_resp = {
            "id": "chatcmpl-invalid-1",
            "object": "chat.completion",
            "created": 1725880000,
            "model": GROQ_MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "not valid json string",
                        "refusal": None,
                    },
                    "finish_reason": "stop",
                }
            ],
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=invalid_resp)

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        prompt: Any = [{"role": "user", "content": [{"text": "hi"}]}]
        with pytest.raises(ValueError, match="Groq structured output could not be parsed"):
            async for _ in model.structured_output(_Extraction, prompt):
                pass

    asyncio.run(_test())


# ===========================================================================
# 8. 429 & Retry-After Preservation
# ===========================================================================


def test_groq_model_preserves_429_and_retry_after() -> None:
    """HTTP 429 status code and Retry-After header are captured without sleep/retry."""

    async def _test() -> None:
        async def rate_limit_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                headers={"Retry-After": "15"},
                json={
                    "error": {
                        "message": "Rate limit reached for requests",
                        "type": "requests",
                        "code": "rate_limit_exceeded",
                    }
                },
            )

        transport = httpx.MockTransport(rate_limit_handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)
        prompt: Any = [{"role": "user", "content": [{"text": "hello"}]}]

        started = time.monotonic()
        with pytest.raises(ModelThrottledException):
            async for _ in model.stream(prompt):
                pass
        elapsed = time.monotonic() - started

        # Proves no sleep happened
        assert elapsed < 1.0
        assert model.last_status_code == 429
        assert model.last_retry_after == 15.0
        assert model.sent == 1

    asyncio.run(_test())


# ===========================================================================
# 9. Cancellation & Resource Cleanup
# ===========================================================================


def test_groq_model_cancellation_and_resource_closing() -> None:
    """Streams and client sessions close cleanly on cancellation or explicit aclose()."""

    async def _test() -> None:
        stream_closed = False

        async def cancellable_stream() -> AsyncIterator[bytes]:
            nonlocal stream_closed
            try:
                yield b"data: {}\n\n"
                await asyncio.sleep(10.0)
            finally:
                stream_closed = True

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=cancellable_stream(),
            )

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)
        prompt: Any = [{"role": "user", "content": [{"text": "hello"}]}]

        async def run_stream() -> None:
            async for _ in model.stream(prompt):
                pass

        task = asyncio.create_task(run_stream())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert stream_closed is True

        # Explicit aclose()
        await model.aclose()
        assert not model.client_open

        # Post-close calls fail closed
        with pytest.raises(GroqModelError, match="GroqModel adapter is closed"):
            async for _ in model.stream(prompt):
                pass

    asyncio.run(_test())


def test_groq_model_context_manager() -> None:
    """async with GroqModel(...) automatically closes resources on exit."""

    async def _test() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler)
        async with GroqModel(api_key=DUMMY_KEY, transport=transport) as model:
            assert model.client_open
        assert not model.client_open

    asyncio.run(_test())
