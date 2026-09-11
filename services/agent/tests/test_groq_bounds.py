"""Comprehensive tests for frozen Groq request envelope and serialization bounds.

Enforces BS-019 mechanical limits per docs/M3_GROQ_BOUNDS_CONTRACT.md:
1. Fixed max_completion_tokens: 1024 on both streaming tool-loop and structured-output calls.
2. Fixed reasoning_effort: "low"; keep one completion (n absent or 1).
3. Deprecated max_tokens forbidden; conflicting fields rejected.
4. Maximum serialized HTTP request body: 16,384 bytes per actual send.
5. All validations occur at the transport boundary BEFORE charging budget or dispatching.
6. Rejected sends do NOT call inner transport or consume a physical-send debit.
7. Mutation via kwargs or parent config fails closed without debit.
8. Sticky 6-send budget preserved (6 permitted, 7th refused).
9. Length-limited / incomplete responses in structured output fail explicitly.
10. Generic error messages contain no credentials, prompts, history, or raw payloads.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
import pytest
from strands import Agent, tool
from strands.types.content import Messages

from borrowed_steps.infrastructure.groq_model import (
    DEFAULT_MAX_SENDS,
    FIXED_MAX_COMPLETION_TOKENS,
    FIXED_REASONING_EFFORT,
    GROQ_BASE_URL,
    GROQ_MODEL_ID,
    MAX_REQUEST_BYTES,
    GroqEnvelopeRefusedError,
    GroqModel,
    GroqSendBudgetExceededError,
)
from borrowed_steps.infrastructure.strands_interpreter import _Extraction

DUMMY_KEY = "gsk_test_synthetic_bounds_key_98765"


@pytest.mark.parametrize("variant", ["duplicate", "nan", "nested", "target"])
def test_ambiguous_json_and_private_target_fail_closed(
    variant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if variant == "nested":

        def exhausted_parser(*args: object, **kwargs: object) -> None:
            raise RecursionError("private-sentinel")

        monkeypatch.setattr("borrowed_steps.infrastructure.groq_model.json.loads", exhausted_parser)

    async def check() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200)

        payload = (
            '{"model":"openai/gpt-oss-20b","max_completion_tokens":1024,"reasoning_effort":"low"'
        )
        if variant == "duplicate":
            payload += ',"max_completion_tokens":1024'
        elif variant == "nan":
            payload += ',"temperature":NaN'
        elif variant == "nested":
            payload += ',"extra":' + "[" * 1200 + "0" + "]" * 1200
        payload += "}"
        url = f"{GROQ_BASE_URL}/chat/completions"
        if variant == "target":
            url += "/private-sentinel"
        async with GroqModel(api_key=DUMMY_KEY, transport=httpx.MockTransport(handler)) as model:
            from borrowed_steps.infrastructure.groq_model import GroqTargetRefusedError

            with pytest.raises(GroqTargetRefusedError) as caught:
                await model._groq_transport.handle_async_request(
                    httpx.Request("POST", url, content=payload.encode())
                )
            assert "private-sentinel" not in str(caught.value)
            assert calls == 0
            assert model.sent == 0

    asyncio.run(check())


def _make_sse_chunk(content: str | None = None, tool_call: dict[str, Any] | None = None) -> str:
    """Build a minimal synthetic SSE chunk."""
    delta: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        delta["content"] = content
    if tool_call is not None:
        delta["tool_calls"] = [tool_call]
    chunk = {
        "id": "chatcmpl-synthetic",
        "object": "chat.completion.chunk",
        "created": 1725880000,
        "model": GROQ_MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    stop_chunk = {
        "id": "chatcmpl-synthetic",
        "object": "chat.completion.chunk",
        "created": 1725880000,
        "model": GROQ_MODEL_ID,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(stop_chunk)}\n\ndata: [DONE]\n\n"


def _make_parsed_response(
    parsed_data: dict[str, Any] | None, finish_reason: str = "stop"
) -> dict[str, Any]:
    """Build a synthetic OpenAI-compatible parsed chat completion."""
    return {
        "id": "chatcmpl-synthetic-parsed",
        "object": "chat.completion",
        "created": 1725880000,
        "model": GROQ_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(parsed_data) if parsed_data else None,
                    "refusal": None,
                },
                "finish_reason": finish_reason,
            }
        ],
    }


# ===========================================================================
# 1. Real SDK Serialization & Wire Payload Inspection (Both Stages)
# ===========================================================================


def test_real_sdk_serialization_streaming_tool_loop() -> None:
    """Real Strands Agent tool loop serializes frozen envelope fields across turns."""

    async def _test() -> None:
        tool_executed = False

        @tool(name="read_inventory", description="Counts of equipment.")
        def read_inventory() -> str:
            nonlocal tool_executed
            tool_executed = True
            return json.dumps({"counts": [{"kind": "crutches", "count": 3}]})

        intercepted_requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            intercepted_requests.append(request)
            if len(intercepted_requests) == 1:
                # Turn 1: tool call
                chunk = {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1725880000,
                    "model": GROQ_MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_inv_1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_inventory",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                c_stop = {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1725880000,
                    "model": GROQ_MODEL_ID,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                }
                text = (
                    f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(c_stop)}\n\ndata: [DONE]\n\n"
                )
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

            # Turn 2: final answer
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=_make_sse_chunk(content="We have 3 crutches available."),
            )

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        agent = Agent(
            model=model,
            tools=[read_inventory],
            system_prompt="Inventory assistant.",
            load_tools_from_directory=False,
        )

        res = await agent.invoke_async("Check crutches availability")
        assert tool_executed is True
        assert "3 crutches" in str(res.message)
        assert len(intercepted_requests) == 2
        assert model.sent == 2

        # Assert Turn 1 Wire JSON
        wire_1 = json.loads(intercepted_requests[0].content.decode("utf-8"))
        assert wire_1["model"] == "openai/gpt-oss-20b"
        assert wire_1["max_completion_tokens"] == 1024
        assert type(wire_1["max_completion_tokens"]) is int
        assert wire_1["reasoning_effort"] == "low"
        assert "max_tokens" not in wire_1
        assert "n" not in wire_1 or wire_1["n"] == 1
        assert len(intercepted_requests[0].content) <= 16384

        # Assert Turn 2 Wire JSON
        wire_2 = json.loads(intercepted_requests[1].content.decode("utf-8"))
        assert wire_2["model"] == "openai/gpt-oss-20b"
        assert wire_2["max_completion_tokens"] == 1024
        assert type(wire_2["max_completion_tokens"]) is int
        assert wire_2["reasoning_effort"] == "low"
        assert "max_tokens" not in wire_2
        assert len(intercepted_requests[1].content) <= 16384

    asyncio.run(_test())


def test_real_sdk_serialization_stage2_structured_output() -> None:
    """Stage 2 structured_output serializes max_completion_tokens and reasoning_effort."""

    async def _test() -> None:
        data = {
            "borrower_label": "Kavitha M",
            "equipment_kind": "wheelchair",
            "pickup_location": "South Wing",
            "due_at": "2026-09-18T14:00:00Z",
        }
        intercepted_requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            intercepted_requests.append(request)
            return httpx.Response(200, json=_make_parsed_response(data))

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        prompt: Messages = [
            {"role": "user", "content": [{"text": "Kavitha needs wheelchair at South Wing"}]}
        ]
        results = [item async for item in model.structured_output(_Extraction, prompt)]

        assert len(results) == 1
        assert results[0]["output"].borrower_label == "Kavitha M"
        assert len(intercepted_requests) == 1
        assert model.sent == 1

        wire = json.loads(intercepted_requests[0].content.decode("utf-8"))
        assert wire["model"] == "openai/gpt-oss-20b"
        assert wire["max_completion_tokens"] == 1024
        assert type(wire["max_completion_tokens"]) is int
        assert wire["reasoning_effort"] == "low"
        assert "max_tokens" not in wire
        assert "tools" not in wire
        assert "tool_choice" not in wire
        assert wire["stream"] is False
        assert "response_format" in wire
        assert len(intercepted_requests[0].content) <= 16384

    asyncio.run(_test())


# ===========================================================================
# 2. Exact Byte Boundary Tests (16,384 vs 16,385)
# ===========================================================================


def test_exact_byte_boundary_16384_accepted_16385_refused() -> None:
    """Exact 16,384 bytes wire payload is accepted; 16,385 bytes is refused without debit."""

    async def _test() -> None:
        inner_dispatched = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal inner_dispatched
            inner_dispatched += 1
            return httpx.Response(200, json=_make_parsed_response({"borrower_label": "Test"}))

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        # Baseline template
        messages_list: list[dict[str, Any]] = [{"role": "user", "content": ""}]
        base_obj: dict[str, Any] = {
            "model": GROQ_MODEL_ID,
            "messages": messages_list,
            "max_completion_tokens": FIXED_MAX_COMPLETION_TOKENS,
            "reasoning_effort": FIXED_REASONING_EFFORT,
        }
        empty_json = json.dumps(base_obj).encode("utf-8")
        pad_needed_for_16384 = MAX_REQUEST_BYTES - len(empty_json)

        # Case 1: Exactly 16,384 bytes
        messages_list[0]["content"] = "a" * pad_needed_for_16384
        exact_16384_bytes = json.dumps(base_obj).encode("utf-8")
        assert len(exact_16384_bytes) == 16384

        req_ok = httpx.Request(
            "POST", f"{GROQ_BASE_URL}/chat/completions", content=exact_16384_bytes
        )
        resp = await model._groq_transport.handle_async_request(req_ok)
        assert resp.status_code == 200
        assert inner_dispatched == 1
        assert model.sent == 1
        assert model.budget.remaining == 5

        # Case 2: Exactly 16,385 bytes (boundary + 1)
        messages_list[0]["content"] = "a" * (pad_needed_for_16384 + 1)
        exact_16385_bytes = json.dumps(base_obj).encode("utf-8")
        assert len(exact_16385_bytes) == 16385

        req_fail = httpx.Request(
            "POST", f"{GROQ_BASE_URL}/chat/completions", content=exact_16385_bytes
        )
        with pytest.raises(GroqEnvelopeRefusedError, match="Request body size exceeds 16384 bytes"):
            await model._groq_transport.handle_async_request(req_fail)

        # Invariant: inner transport was NOT called, no send debit charged
        assert inner_dispatched == 1
        assert model.sent == 1
        assert model.budget.remaining == 5
        assert not model.exhausted

    asyncio.run(_test())


# ===========================================================================
# 3. Unicode & JSON Escaping Wire Measurement
# ===========================================================================


def test_unicode_and_escaping_wire_measurement() -> None:
    """UTF-8 bytes and JSON escapes are measured on actual wire payload."""

    async def _test() -> None:
        inner_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal inner_calls
            inner_calls += 1
            return httpx.Response(200, json=_make_parsed_response({"borrower_label": "Test"}))

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        # Multilingual text with special characters, newlines, tabs, quotes, emojis
        special_content = 'Tamil: உதவி \n Devanagari: सहायता \t Quote: "test" \\ Emoji: 🦽'
        messages_spec: list[dict[str, Any]] = [{"role": "user", "content": special_content}]
        payload_dict: dict[str, Any] = {
            "model": GROQ_MODEL_ID,
            "messages": messages_spec,
            "max_completion_tokens": 1024,
            "reasoning_effort": "low",
        }
        # Raw multi-byte UTF-8 wire bytes strictly exceed string character length
        wire_utf8 = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
        assert len(wire_utf8) > len(json.dumps(payload_dict, ensure_ascii=False))
        assert len(wire_utf8) <= MAX_REQUEST_BYTES

        req = httpx.Request("POST", f"{GROQ_BASE_URL}/chat/completions", content=wire_utf8)
        resp = await model._groq_transport.handle_async_request(req)
        assert resp.status_code == 200
        assert inner_calls == 1
        assert model.sent == 1

        # Now test that unicode expanding past 16384 bytes fails closed without debit
        # Each 'உ' in UTF-8 is 3 bytes
        overflow_tamils = "உ" * 6000  # 18,000 bytes
        messages_spec[0]["content"] = overflow_tamils
        overflow_wire = json.dumps(payload_dict).encode("utf-8")
        assert len(overflow_wire) > MAX_REQUEST_BYTES

        req_overflow = httpx.Request(
            "POST", f"{GROQ_BASE_URL}/chat/completions", content=overflow_wire
        )
        with pytest.raises(GroqEnvelopeRefusedError, match="Request body size exceeds 16384 bytes"):
            await model._groq_transport.handle_async_request(req_overflow)

        # Invariant: inner transport not called, no debit
        assert inner_calls == 1
        assert model.sent == 1

    asyncio.run(_test())


# ===========================================================================
# 4. Invalid Envelope Rejections (Fail Closed, Zero Debit)
# ===========================================================================


@pytest.mark.parametrize(
    ("bad_payload_bytes", "expected_err_pattern"),
    [
        (b"", "Request body must not be empty"),
        (b"\xff\xfe\x00\x00", "Malformed request encoding"),
        (b'{"model": "openai/gpt-oss-20b", "max_completion_tokens":', "Malformed JSON payload"),
        (b"[1, 2, 3]", "Request payload must be a JSON object"),
        (b'"just a string"', "Request payload must be a JSON object"),
        (b"12345", "Request payload must be a JSON object"),
        (
            json.dumps(
                {"model": "gpt-4o", "max_completion_tokens": 1024, "reasoning_effort": "low"}
            ).encode("utf-8"),
            "Request model refused",
        ),
        (
            json.dumps({"model": "openai/gpt-oss-20b", "reasoning_effort": "low"}).encode("utf-8"),
            "Request max_completion_tokens must be exactly 1024",
        ),
        (
            json.dumps(
                {
                    "model": "openai/gpt-oss-20b",
                    "max_completion_tokens": True,
                    "reasoning_effort": "low",
                }
            ).encode("utf-8"),
            "Request max_completion_tokens must be exactly 1024",
        ),
        (
            json.dumps(
                {
                    "model": "openai/gpt-oss-20b",
                    "max_completion_tokens": 1024.0,
                    "reasoning_effort": "low",
                }
            ).encode("utf-8"),
            "Request max_completion_tokens must be exactly 1024",
        ),
        (
            json.dumps(
                {
                    "model": "openai/gpt-oss-20b",
                    "max_completion_tokens": "1024",
                    "reasoning_effort": "low",
                }
            ).encode("utf-8"),
            "Request max_completion_tokens must be exactly 1024",
        ),
        (
            json.dumps(
                {
                    "model": "openai/gpt-oss-20b",
                    "max_completion_tokens": 2048,
                    "reasoning_effort": "low",
                }
            ).encode("utf-8"),
            "Request max_completion_tokens must be exactly 1024",
        ),
        (
            json.dumps(
                {
                    "model": "openai/gpt-oss-20b",
                    "max_completion_tokens": 512,
                    "reasoning_effort": "low",
                }
            ).encode("utf-8"),
            "Request max_completion_tokens must be exactly 1024",
        ),
        (
            json.dumps(
                {
                    "model": "openai/gpt-oss-20b",
                    "max_completion_tokens": 1024,
                    "max_tokens": 1024,
                    "reasoning_effort": "low",
                }
            ).encode("utf-8"),
            "Deprecated max_tokens field is forbidden",
        ),
        (
            json.dumps(
                {
                    "model": "openai/gpt-oss-20b",
                    "max_completion_tokens": 1024,
                    "max_tokens": 100,
                    "reasoning_effort": "low",
                }
            ).encode("utf-8"),
            "Deprecated max_tokens field is forbidden",
        ),
        (
            json.dumps({"model": "openai/gpt-oss-20b", "max_completion_tokens": 1024}).encode(
                "utf-8"
            ),
            "Request reasoning_effort must be 'low'",
        ),
        (
            json.dumps(
                {
                    "model": "openai/gpt-oss-20b",
                    "max_completion_tokens": 1024,
                    "reasoning_effort": "high",
                }
            ).encode("utf-8"),
            "Request reasoning_effort must be 'low'",
        ),
        (
            json.dumps(
                {
                    "model": "openai/gpt-oss-20b",
                    "max_completion_tokens": 1024,
                    "reasoning_effort": "low",
                    "n": 2,
                }
            ).encode("utf-8"),
            "Field n must be absent or 1",
        ),
        (
            json.dumps(
                {
                    "model": "openai/gpt-oss-20b",
                    "max_completion_tokens": 1024,
                    "reasoning_effort": "low",
                    "n": 0,
                }
            ).encode("utf-8"),
            "Field n must be absent or 1",
        ),
        (
            json.dumps(
                {
                    "model": "openai/gpt-oss-20b",
                    "max_completion_tokens": 1024,
                    "reasoning_effort": "low",
                    "n": True,
                }
            ).encode("utf-8"),
            "Field n must be absent or 1",
        ),
    ],
)
def test_envelope_rejections_fail_closed_without_debit(
    bad_payload_bytes: bytes, expected_err_pattern: str
) -> None:
    """Each malformed or invalid envelope variant fails closed without charging send budget."""

    async def _test() -> None:
        inner_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal inner_calls
            inner_calls += 1
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        req = httpx.Request("POST", f"{GROQ_BASE_URL}/chat/completions", content=bad_payload_bytes)
        with pytest.raises(GroqEnvelopeRefusedError, match=re.escape(expected_err_pattern)):
            await model._groq_transport.handle_async_request(req)

        # Invariant: inner transport NEVER called, budget NEVER charged
        assert inner_calls == 0
        assert model.sent == 0
        assert model.budget.remaining == DEFAULT_MAX_SENDS
        assert not model.exhausted

    asyncio.run(_test())


def test_envelope_valid_n_field_accepted() -> None:
    """Envelope with explicit integer n=1 or absent n is accepted and charged."""

    async def _test() -> None:
        inner_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal inner_calls
            inner_calls += 1
            return httpx.Response(200, json=_make_parsed_response({"borrower_label": "Test"}))

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        # 1. Explicit integer n=1
        payload_n1 = json.dumps(
            {
                "model": GROQ_MODEL_ID,
                "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 1024,
                "reasoning_effort": "low",
                "n": 1,
            }
        ).encode("utf-8")
        req1 = httpx.Request("POST", f"{GROQ_BASE_URL}/chat/completions", content=payload_n1)
        resp1 = await model._groq_transport.handle_async_request(req1)
        assert resp1.status_code == 200
        assert inner_calls == 1
        assert model.sent == 1

        # 2. Absent n
        payload_no_n = json.dumps(
            {
                "model": GROQ_MODEL_ID,
                "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 1024,
                "reasoning_effort": "low",
            }
        ).encode("utf-8")
        req2 = httpx.Request("POST", f"{GROQ_BASE_URL}/chat/completions", content=payload_no_n)
        resp2 = await model._groq_transport.handle_async_request(req2)
        assert resp2.status_code == 200
        assert inner_calls == 2
        assert model.sent == 2

    asyncio.run(_test())


# ===========================================================================
# 5. Mutation Attempts Via Parent Config & Kwargs (Fail Closed, Zero Debit)
# ===========================================================================


def test_mutation_via_parent_config_params_rejected() -> None:
    """Mutations to model.config['params'] fail closed before dispatch without debit."""

    async def _test() -> None:
        inner_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal inner_calls
            inner_calls += 1
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)

        prompt: Messages = [{"role": "user", "content": [{"text": "hi"}]}]
        params = model.config.get("params")
        assert isinstance(params, dict)

        # Mutation 1: Increase max_completion_tokens to 2048
        params["max_completion_tokens"] = 2048
        with pytest.raises(
            GroqEnvelopeRefusedError, match="max_completion_tokens cannot be modified"
        ):
            async for _ in model.stream(prompt):
                pass
        assert inner_calls == 0
        assert model.sent == 0

        # Mutation 2: Add deprecated max_tokens
        params["max_completion_tokens"] = FIXED_MAX_COMPLETION_TOKENS
        params["max_tokens"] = 100
        with pytest.raises(
            GroqEnvelopeRefusedError, match="Deprecated max_tokens parameter is forbidden"
        ):
            async for _ in model.stream(prompt):
                pass
        assert inner_calls == 0
        assert model.sent == 0

        # Mutation 3: Change reasoning_effort to "high"
        del params["max_tokens"]
        params["reasoning_effort"] = "high"
        with pytest.raises(GroqEnvelopeRefusedError, match="reasoning_effort cannot be modified"):
            async for _ in model.stream(prompt):
                pass
        assert inner_calls == 0
        assert model.sent == 0

        # Mutation 4: Change n to 2
        params["reasoning_effort"] = FIXED_REASONING_EFFORT
        params["n"] = 2
        with pytest.raises(GroqEnvelopeRefusedError, match="Field n must be absent or 1"):
            async for _ in model.stream(prompt):
                pass
        assert inner_calls == 0
        assert model.sent == 0

    asyncio.run(_test())


def test_mutation_via_kwargs_rejected() -> None:
    """Caller-supplied kwargs attempting to override or widen bounds fail closed without debit."""

    async def _test() -> None:
        inner_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal inner_calls
            inner_calls += 1
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)
        prompt: Messages = [{"role": "user", "content": [{"text": "hi"}]}]

        # 1. Stream kwargs max_completion_tokens
        with pytest.raises(
            GroqEnvelopeRefusedError, match="max_completion_tokens cannot be modified"
        ):
            async for _ in model.stream(prompt, max_completion_tokens=2048):
                pass
        assert inner_calls == 0
        assert model.sent == 0

        # 2. Stream kwargs max_tokens
        with pytest.raises(
            GroqEnvelopeRefusedError, match="Deprecated max_tokens parameter is forbidden"
        ):
            async for _ in model.stream(prompt, max_tokens=500):
                pass
        assert inner_calls == 0
        assert model.sent == 0

        # 3. Stream kwargs reasoning_effort
        with pytest.raises(GroqEnvelopeRefusedError, match="reasoning_effort cannot be modified"):
            async for _ in model.stream(prompt, reasoning_effort="high"):
                pass
        assert inner_calls == 0
        assert model.sent == 0

        # 4. Stream kwargs n
        with pytest.raises(GroqEnvelopeRefusedError, match="Field n must be absent or 1"):
            async for _ in model.stream(prompt, n=2):
                pass
        assert inner_calls == 0
        assert model.sent == 0

        # 5. Structured output kwargs max_completion_tokens
        with pytest.raises(
            GroqEnvelopeRefusedError, match="max_completion_tokens cannot be modified"
        ):
            async for _ in model.structured_output(_Extraction, prompt, max_completion_tokens=4096):
                pass
        assert inner_calls == 0
        assert model.sent == 0

        # 6. Structured output kwargs max_tokens
        with pytest.raises(
            GroqEnvelopeRefusedError, match="Deprecated max_tokens parameter is forbidden"
        ):
            async for _ in model.structured_output(_Extraction, prompt, max_tokens=100):
                pass
        assert inner_calls == 0
        assert model.sent == 0

        # 7. Structured output kwargs reasoning_effort
        with pytest.raises(GroqEnvelopeRefusedError, match="reasoning_effort cannot be modified"):
            async for _ in model.structured_output(_Extraction, prompt, reasoning_effort="medium"):
                pass
        assert inner_calls == 0
        assert model.sent == 0

    asyncio.run(_test())


# ===========================================================================
# 6. Sticky Budget Preservation & Seventh-Send Refusal
# ===========================================================================


def test_sticky_budget_preservation_and_seventh_send_refusal() -> None:
    """Rejected envelopes do not consume sends; exactly 6 sends allowed, 7th is refused."""

    async def _test() -> None:
        inner_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal inner_calls
            inner_calls += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=_make_sse_chunk(content="Hello"),
            )

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, max_sends=6, transport=transport)
        prompt: Messages = [{"role": "user", "content": [{"text": "hi"}]}]

        # Dispatch 3 rejected requests (should NOT charge budget)
        for _ in range(3):
            with pytest.raises(GroqEnvelopeRefusedError):
                async for _ in model.stream(prompt, max_tokens=100):
                    pass

        assert model.sent == 0
        assert model.budget.remaining == 6
        assert inner_calls == 0

        # Now dispatch 6 valid sends
        for i in range(1, 7):
            async for _ in model.stream(prompt):
                pass
            assert model.sent == i
            assert inner_calls == i

        assert model.sent == 6
        assert model.exhausted
        assert model.budget.remaining == 0

        # Attempt 7th send: refused by sticky budget
        with pytest.raises(GroqSendBudgetExceededError, match="Send budget exhausted: 6/6"):
            async for _ in model.stream(prompt):
                pass

        assert inner_calls == 6
        assert model.sent == 6

    asyncio.run(_test())


# ===========================================================================
# 7. Incomplete & Length-Limited Response Handling
# ===========================================================================


def test_structured_output_length_finish_reason_fails_explicitly() -> None:
    """finish_reason 'length' in structured output raises ValueError and never returns a draft."""

    async def _test() -> None:
        # Synthetic length-truncated response
        truncated_response = {
            "id": "chatcmpl-truncated",
            "object": "chat.completion",
            "created": 1725880000,
            "model": GROQ_MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": '{"borrower_label": "Incompl',
                        "refusal": None,
                    },
                    "finish_reason": "length",
                }
            ],
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=truncated_response)

        transport = httpx.MockTransport(handler)
        model = GroqModel(api_key=DUMMY_KEY, transport=transport)
        prompt: Messages = [{"role": "user", "content": [{"text": "extract"}]}]

        with pytest.raises(
            ValueError, match="Groq structured output truncated due to length limit"
        ):
            async for _ in model.structured_output(_Extraction, prompt):
                pass

    asyncio.run(_test())


# ===========================================================================
# 8. Error Sanitization (No Prompts, Secrets, or Payloads in Messages)
# ===========================================================================


def test_error_sanitization_contains_no_sensitive_values() -> None:
    """Envelope error messages contain no credentials, user prompts, or raw payload text."""

    async def _test() -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
        model = GroqModel(api_key="gsk_secret_token_12345", transport=transport)

        secret_prompt = "CONFIDENTIAL_PATIENT_MEDICAL_HISTORY"
        secret_content = "SECRET_INTAKE_NOTE_FOR_MEMBER"

        bad_payload = json.dumps(
            {
                "model": "openai/gpt-oss-20b",
                "messages": [{"role": "user", "content": secret_prompt}],
                "max_completion_tokens": 9999,  # invalid
                "reasoning_effort": "low",
                "extra_confidential": secret_content,
            }
        ).encode("utf-8")

        req = httpx.Request("POST", f"{GROQ_BASE_URL}/chat/completions", content=bad_payload)

        try:
            await model._groq_transport.handle_async_request(req)
            pytest.fail("Should have raised GroqEnvelopeRefusedError")
        except GroqEnvelopeRefusedError as exc:
            err_msg = str(exc)
            assert "gsk_" not in err_msg
            assert "secret" not in err_msg.lower()
            assert secret_prompt not in err_msg
            assert secret_content not in err_msg
            assert "9999" not in err_msg

    asyncio.run(_test())
