"""Reviewer regressions: indefinitely stalled reads must be interrupted."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from strands import Agent
from strands.event_loop._retry import ModelRetryStrategy
from strands.types.content import Messages

from borrowed_steps.infrastructure.groq_model import (
    GROQ_BASE_URL,
    GroqDeadlineExpiredError,
    GroqModel,
    GroqRequestTimeoutError,
    GroqSendBudgetExceededError,
    GroqTargetRefusedError,
)
from borrowed_steps.infrastructure.strands_interpreter import _Extraction


class StalledStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False
        self.reading = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.reading.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("request_limit", [False, True])
def test_stalled_body_is_interrupted(structured: bool, request_limit: bool) -> None:
    async def run() -> None:
        body = StalledStream()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=body)

        async with GroqModel(
            "dummy",
            transport=httpx.MockTransport(handler),
            operation_deadline_seconds=3.0 if request_limit else 0.6,
            request_timeout_seconds=0.6 if request_limit else 3.0,
        ) as model:
            prompt: Messages = [{"role": "user", "content": [{"text": "synthetic"}]}]

            async def consume() -> None:
                events = (
                    model.structured_output(_Extraction, prompt)
                    if structured
                    else model.stream(prompt)
                )
                async for _ in events:
                    pass

            expected = GroqRequestTimeoutError if request_limit else GroqDeadlineExpiredError
            with pytest.raises(expected):
                await asyncio.wait_for(consume(), timeout=2.0)
            assert body.closed
            assert not model._active_clients
            assert model.sent == 1

    asyncio.run(run())


@pytest.mark.parametrize("structured", [False, True])
def test_cancel_stalled_body_closes_client(structured: bool) -> None:
    async def run() -> None:
        body = StalledStream()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=body)

        async with GroqModel("dummy", transport=httpx.MockTransport(handler)) as model:
            prompt: Messages = [{"role": "user", "content": [{"text": "synthetic"}]}]

            async def consume() -> None:
                events = (
                    model.structured_output(_Extraction, prompt)
                    if structured
                    else model.stream(prompt)
                )
                async for _ in events:
                    pass

            task = asyncio.create_task(consume())
            await asyncio.wait_for(body.reading.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            assert body.closed
            assert not model._active_clients

    asyncio.run(run())


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_limits_refused(value: float) -> None:
    with pytest.raises(ValueError, match="operation_deadline_seconds"):
        GroqModel("dummy", operation_deadline_seconds=value)
    with pytest.raises(ValueError, match="request_timeout_seconds"):
        GroqModel("dummy", request_timeout_seconds=value)


@pytest.mark.parametrize(
    "suffix", [":8443/openai/v1/chat/completions", "/openai/v1/chat/completions?key=synthetic"]
)
def test_target_pin_includes_port_and_query(suffix: str) -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("Refused target reached transport")

        async with GroqModel("dummy", transport=httpx.MockTransport(handler)) as model:
            with pytest.raises(GroqTargetRefusedError):
                await model._groq_transport.handle_async_request(
                    httpx.Request("POST", "https://api.groq.com" + suffix)
                )
            assert model.sent == 0

    asyncio.run(run())


def test_failed_transport_close_remains_retryable() -> None:
    class FailOnceTransport(httpx.AsyncBaseTransport):
        closes = 0

        async def aclose(self) -> None:
            self.closes += 1
            if self.closes == 1:
                raise RuntimeError("synthetic close failure")

    async def run() -> None:
        transport = FailOnceTransport()
        model = GroqModel("dummy", transport=transport)
        with pytest.raises(RuntimeError, match="synthetic close failure"):
            await model.aclose()
        await model.aclose()
        assert model._groq_transport._closed
        assert transport.closes == 2
        assert GROQ_BASE_URL.startswith("https://")

    asyncio.run(run())


def test_real_strands_retry_cannot_exceed_transport_budget() -> None:
    async def run() -> None:
        sends = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal sends
            sends += 1
            return httpx.Response(429, json={"error": {"message": "synthetic throttle"}})

        async with GroqModel("dummy", max_sends=2, transport=httpx.MockTransport(handler)) as model:
            agent = Agent(
                model=model,
                callback_handler=None,
                load_tools_from_directory=False,
                retry_strategy=ModelRetryStrategy(max_attempts=6, initial_delay=0, max_delay=0),
            )
            with pytest.raises(GroqSendBudgetExceededError):
                await asyncio.wait_for(agent.invoke_async("synthetic"), timeout=2)
            assert sends == model.sent == 2
            assert model.exhausted
            prompt: Messages = [{"role": "user", "content": [{"text": "synthetic"}]}]
            with pytest.raises(GroqSendBudgetExceededError):
                async for _ in model.structured_output(_Extraction, prompt):
                    pass
            assert sends == 2

    asyncio.run(run())


def test_parent_config_cannot_change_wire_model() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("Changed model reached transport")

        async with GroqModel("dummy", transport=httpx.MockTransport(handler)) as model:
            model.config["model_id"] = "unapproved-model"
            prompt: Messages = [{"role": "user", "content": [{"text": "synthetic"}]}]
            with pytest.raises(GroqTargetRefusedError, match="Request model refused"):
                async for _ in model.stream(prompt):
                    pass
            assert model.sent == 0

    asyncio.run(run())
