"""Owned provider transport: client lifecycle and the real outgoing-request budget.

Every test drives the **actual** ``OwnedOllamaModel`` code path with an
instrumented in-process fake ``ollama.AsyncClient``. No model is invoked and no
socket is opened; the fake records construction, every ``chat`` call, every
stream close and every client close.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from threading import Event
from typing import Any, ClassVar

import httpx
import ollama
import pytest
from pydantic import BaseModel
from strands import Agent, tool
from strands.types.agent import Limits

from borrowed_steps.config import (
    ASSISTANT_MAX_MODEL_REQUESTS,
    ASSISTANT_TRANSPORT_TIMEOUT_SECONDS,
    DEFAULT_ASSISTANT_HOST,
    DEFAULT_ASSISTANT_MODEL,
)
from borrowed_steps.infrastructure.owned_ollama import (
    LOCAL_AUTHORIZATION_MARKER,
    OwnedOllamaModel,
    RequestBudget,
    RequestBudgetExceededError,
)


class _Function:
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.function = _Function(name, arguments)


class _Message:
    def __init__(self, content: str = "", tool_calls: list[_ToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Event:
    def __init__(self, message: _Message, done_reason: str | None = "stop") -> None:
        self.message = message
        self.done_reason = done_reason
        self.prompt_eval_count = 1
        self.eval_count = 1
        self.total_duration = 1_000_000


class _Stream:
    """Async stream that records whether it was closed."""

    def __init__(self, events: list[_Event], owner: FakeAsyncClient) -> None:
        self._events = list(events)
        self._owner = owner

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> _Event:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def aclose(self) -> None:
        self._owner.streams_closed += 1


class _StallingStream:
    """Accepts the request and then never yields a chunk."""

    def __init__(self, owner: FakeAsyncClient) -> None:
        self._owner = owner
        self.started = asyncio.Event()

    def __aiter__(self) -> _StallingStream:
        return self

    async def __anext__(self) -> _Event:
        self.started.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration  # pragma: no cover - unreachable

    async def aclose(self) -> None:
        self._owner.streams_closed += 1


class FakeAsyncClient:
    """Instrumented stand-in for ollama.AsyncClient. Opens no socket."""

    created: ClassVar[list[FakeAsyncClient]] = []

    def __init__(self, host: str | None = None, **kwargs: object) -> None:
        self.host = host
        self.kwargs = kwargs
        self.chats = 0
        self.closed = 0
        self.streams_closed = 0
        self.behaviour: str = "tool_then_text"
        self.error: Exception | None = None
        self.stall: _StallingStream | None = None
        FakeAsyncClient.created.append(self)

    async def chat(self, **request: object) -> object:
        self.chats += 1
        if self.error is not None:
            raise self.error
        if self.behaviour == "stall":
            self.stall = _StallingStream(self)
            return self.stall
        if request.get("stream") is False:
            payload = {
                "borrower_label": None,
                "equipment_kind": None,
                "pickup_location": None,
                "due_at": None,
            }
            return _Event(_Message(content=json.dumps(payload)))
        if self.behaviour == "text_only":
            return _Stream([_Event(_Message(content="no tool for you"))], self)
        return _Stream(
            [_Event(_Message(content="", tool_calls=[_ToolCall("read_inventory", {})]))],
            self,
        )

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture(autouse=True)
def _fake_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.created = []
    monkeypatch.setattr(ollama, "AsyncClient", FakeAsyncClient)


class _Empty(BaseModel):
    pass


def _model(budget: RequestBudget) -> OwnedOllamaModel:
    return OwnedOllamaModel(
        host=DEFAULT_ASSISTANT_HOST,
        model_id=DEFAULT_ASSISTANT_MODEL,
        budget=budget,
        timeout_seconds=ASSISTANT_TRANSPORT_TIMEOUT_SECONDS,
    )


async def _drain(model: OwnedOllamaModel) -> None:
    async for _ in model.stream([{"role": "user", "content": [{"text": "hi"}]}]):
        pass


# --- client ownership ----------------------------------------------------


def test_one_client_is_opened_and_closed_for_the_interpretation() -> None:
    budget = RequestBudget(6)

    async def run() -> None:
        async with _model(budget) as model:
            await _drain(model)
            await _drain(model)

    asyncio.run(run())

    assert len(FakeAsyncClient.created) == 1, "a client per request would leak"
    client = FakeAsyncClient.created[0]
    assert client.chats == 2
    assert client.closed == 1
    assert client.streams_closed == 2


def test_the_client_closes_after_a_provider_error() -> None:
    budget = RequestBudget(6)

    async def run() -> None:
        async with _model(budget) as model:
            FakeAsyncClient.created[0].error = ConnectionError("refused")
            with pytest.raises(ConnectionError):
                await _drain(model)

    asyncio.run(run())
    assert FakeAsyncClient.created[0].closed == 1


def test_the_client_and_stream_close_when_a_consumer_abandons_the_stream() -> None:
    budget = RequestBudget(6)

    async def run() -> None:
        async with _model(budget) as model:
            stream = model.stream([{"role": "user", "content": [{"text": "hi"}]}])
            await stream.__anext__()
            await stream.aclose()  # abandon part-way

    asyncio.run(run())
    client = FakeAsyncClient.created[0]
    assert client.streams_closed == 1
    assert client.closed == 1


def test_the_client_closes_after_a_no_chunk_stall_is_cancelled() -> None:
    budget = RequestBudget(6)

    async def run() -> None:
        async with _model(budget) as model:
            FakeAsyncClient.created[0].behaviour = "stall"
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.3):
                    await _drain(model)

    asyncio.run(run())
    client = FakeAsyncClient.created[0]
    assert client.stall is not None
    assert client.stall.started.is_set()
    assert client.streams_closed == 1
    assert client.closed == 1


def test_the_client_closes_when_the_caller_is_cancelled() -> None:
    budget = RequestBudget(6)
    entered = asyncio.Event()

    async def run() -> None:
        async with _model(budget) as model:
            FakeAsyncClient.created[0].behaviour = "stall"
            entered.set()
            await _drain(model)

    async def main() -> None:
        task = asyncio.create_task(run())
        await entered.wait()
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert FakeAsyncClient.created[0].closed == 1


def test_the_client_closes_when_the_budget_refuses_a_request() -> None:
    budget = RequestBudget(1)

    async def run() -> None:
        async with _model(budget) as model:
            await _drain(model)
            with pytest.raises(RequestBudgetExceededError):
                await _drain(model)

    asyncio.run(run())
    client = FakeAsyncClient.created[0]
    assert client.chats == 1, "the refused request must not reach the transport"
    assert client.closed == 1


# --- the real outgoing-request budget ------------------------------------


def test_a_refused_request_never_reaches_the_transport() -> None:
    budget = RequestBudget(ASSISTANT_MAX_MODEL_REQUESTS)

    async def run() -> None:
        async with _model(budget) as model:
            for _ in range(ASSISTANT_MAX_MODEL_REQUESTS):
                await _drain(model)
            with pytest.raises(RequestBudgetExceededError):
                await _drain(model)

    asyncio.run(run())

    client = FakeAsyncClient.created[0]
    assert budget.sent == ASSISTANT_MAX_MODEL_REQUESTS == 6
    assert client.chats == 6, "the seventh request must not be sent"
    assert budget.exhausted


def test_the_budget_is_shared_across_stream_and_structured_output() -> None:
    budget = RequestBudget(3)

    async def run() -> None:
        async with _model(budget) as model:
            await _drain(model)
            async for _ in model.structured_output(
                _Empty, [{"role": "user", "content": [{"text": "hi"}]}]
            ):
                pass
            await _drain(model)
            with pytest.raises(RequestBudgetExceededError):
                async for _ in model.structured_output(
                    _Empty, [{"role": "user", "content": [{"text": "hi"}]}]
                ):
                    pass

    asyncio.run(run())
    assert budget.sent == 3
    assert FakeAsyncClient.created[0].chats == 3


def test_the_budget_failure_is_sticky_even_if_a_caller_swallows_it() -> None:
    """A swallowed refusal must not let a later request through."""
    budget = RequestBudget(1)

    async def run() -> None:
        async with _model(budget) as model:
            await _drain(model)
            for _ in range(3):
                with pytest.raises(RequestBudgetExceededError):
                    await _drain(model)

    asyncio.run(run())
    assert budget.sent == 1
    assert budget.exhausted
    assert FakeAsyncClient.created[0].chats == 1


def test_the_real_agent_loop_cannot_exceed_the_send_budget() -> None:
    """The budget, not Limits(turns), is what the transport actually obeys.

    The real Strands loop is given six turns but a budget of two. The loop wants
    to keep going; the transport still sees exactly two requests.
    """

    @tool(name="read_inventory", description="counts")
    def read_inventory() -> dict[str, Any]:
        """Counts."""
        return {"counts": []}

    budget = RequestBudget(2)

    async def run() -> None:
        async with _model(budget) as model:
            agent = Agent(
                model=model,
                tools=[read_inventory],
                callback_handler=None,
                load_tools_from_directory=False,
                retry_strategy=None,
            )
            await agent.invoke_async(
                "go",
                structured_output_model=_Empty,
                limits=Limits(turns=ASSISTANT_MAX_MODEL_REQUESTS),
                cancel_signal=Event(),
            )

    # However the SDK chooses to surface the refusal, the transport bound holds.
    with contextlib.suppress(BaseException):
        asyncio.run(run())

    client = FakeAsyncClient.created[0]
    assert client.chats == 2, "the loop sent more requests than the budget allowed"
    assert budget.sent == 2
    assert budget.exhausted
    assert client.closed == 1


def test_pinned_client_arguments_reach_the_transport() -> None:
    budget = RequestBudget(6)

    async def run() -> None:
        async with _model(budget) as model:
            await _drain(model)

    asyncio.run(run())
    client = FakeAsyncClient.created[0]
    assert client.host == DEFAULT_ASSISTANT_HOST
    assert client.kwargs == {
        "timeout": ASSISTANT_TRANSPORT_TIMEOUT_SECONDS,
        "follow_redirects": False,
        "trust_env": False,
        "headers": {"Authorization": LOCAL_AUTHORIZATION_MARKER},
    }


# --- an inherited credential cannot reach an outgoing request ------------
#
# These two tests deliberately do NOT use FakeAsyncClient: replacing the client
# would skip the very code that reads OLLAMA_API_KEY. They drive the real
# ollama.AsyncClient with an in-process httpx MockTransport underneath, so real
# header assembly runs and the actual outgoing request can be inspected.

# Fabricated in this file for this test only. It is not a credential, it
# authenticates nothing, and it reaches no service: the transport below is
# in-process and opens no socket.
SENTINEL = "synthetic-not-a-real-key-2f4c9a"


class _RecordingClient(ollama.AsyncClient):
    """The real ollama client, with an in-process transport and no socket."""

    requests: ClassVar[list[httpx.Request]] = []

    def __init__(self, host: str | None = None, **kwargs: object) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            _RecordingClient.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "model": DEFAULT_ASSISTANT_MODEL,
                    "created_at": "2026-09-07T09:00:00Z",
                    "message": {"role": "assistant", "content": "{}"},
                    "done": True,
                    "done_reason": "stop",
                },
            )

        kwargs["transport"] = httpx.MockTransport(handle)
        super().__init__(host, **kwargs)


@pytest.fixture
def _recording_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingClient.requests = []
    monkeypatch.setattr(ollama, "AsyncClient", _RecordingClient)


@pytest.mark.usefixtures("_recording_transport")
def test_an_inherited_api_key_never_reaches_an_outgoing_request(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A synthetic OLLAMA_API_KEY in the environment must not be sent anywhere.

    The installed client adds ``Authorization: Bearer <OLLAMA_API_KEY>`` whenever
    the slot is empty, and ``trust_env=False`` does not disable that. The owned
    provider occupies the slot with a fixed non-secret marker, so the inherited
    value has nowhere to go. Asserting on constructor arguments alone would not
    show this; the assertions below are made on the real request the transport
    was handed.
    """
    monkeypatch.setenv("OLLAMA_API_KEY", SENTINEL)
    caplog.set_level(logging.DEBUG)

    async def run() -> None:
        async with _model(RequestBudget(6)) as model:
            async for _ in model.structured_output(
                _Empty, [{"role": "user", "content": [{"text": "hi"}]}]
            ):
                pass

    asyncio.run(run())

    assert len(_RecordingClient.requests) == 1
    request = _RecordingClient.requests[0]
    assert request.headers["authorization"] == LOCAL_AUTHORIZATION_MARKER
    for name, value in request.headers.items():
        assert SENTINEL not in value, f"the inherited key leaked into {name}"
    assert SENTINEL not in str(request.url)
    assert SENTINEL.encode() not in request.content
    assert SENTINEL not in caplog.text, "the inherited key was written to a log"


@pytest.mark.usefixtures("_recording_transport")
def test_the_marker_is_sent_even_with_no_key_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slot is occupied unconditionally, not only when a key is present."""
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    async def run() -> None:
        async with _model(RequestBudget(6)) as model:
            async for _ in model.structured_output(
                _Empty, [{"role": "user", "content": [{"text": "hi"}]}]
            ):
                pass

    asyncio.run(run())

    assert _RecordingClient.requests[0].headers["authorization"] == LOCAL_AUTHORIZATION_MARKER
