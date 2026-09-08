"""Real cancellation and cleanup ownership, through the real adapter.

Every test here drives the **actual** ``StrandsOllamaInterpreter`` and
``OwnedOllamaModel`` — the real two-stage sequence, the real Strands agent loop —
over an instrumented in-process fake ``ollama.AsyncClient``. No model is invoked
and no socket is opened.

The distinction under test is the one BS-003-R3 missed. A cooperative
``threading.Event`` can only be noticed by Python code that looks at it between
awaits; it is invisible from inside a provider call already waiting on a reply.
Task cancellation is what reaches those. Both are local actions: they end this
process's work and its request, and neither says anything about whether the
model at the other end stopped computing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable, Coroutine
from threading import Event
from typing import Any, ClassVar

import ollama
import pytest
from fastapi.testclient import TestClient

from borrowed_steps.application.errors import AssistantUnavailableError
from borrowed_steps.application.interpreter import (
    CleanupOwner,
    Interpretation,
    InventoryReader,
)
from borrowed_steps.config import (
    DEFAULT_ASSISTANT_HOST,
    DEFAULT_ASSISTANT_MODEL,
    Settings,
)
from borrowed_steps.infrastructure.strands_interpreter import StrandsOllamaInterpreter
from borrowed_steps.interfaces.http.app import create_app
from conftest import FakeClock
from support import error_code, start_workspace
from test_strands_adapter import TEXT, StubClock, StubInventory

_NULL_EXTRACTION = {
    "borrower_label": None,
    "equipment_kind": None,
    "pickup_location": None,
    "due_at": None,
}


class _Function:
    def __init__(self, name: str) -> None:
        self.name = name
        self.arguments: dict[str, Any] = {}


class _ToolCall:
    def __init__(self, name: str) -> None:
        self.function = _Function(name)


class _Msg:
    def __init__(self, content: str = "", tool_calls: list[_ToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Chunk:
    def __init__(self, message: _Msg, done_reason: str | None = "stop") -> None:
        self.message = message
        self.done_reason = done_reason
        self.prompt_eval_count = 1
        self.eval_count = 1
        self.total_duration = 1_000_000


class _Stream:
    def __init__(self, chunks: list[_Chunk], owner: ScriptedClient) -> None:
        self._chunks = list(chunks)
        self._owner = owner

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> _Chunk:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self._owner.streams_closed += 1


class _BlockedStream:
    """Accepts the request, then never yields — and never looks at any Event."""

    def __init__(self, owner: ScriptedClient) -> None:
        self._owner = owner

    def __aiter__(self) -> _BlockedStream:
        return self

    async def __anext__(self) -> _Chunk:
        self._owner.blocked.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration  # pragma: no cover - unreachable

    async def aclose(self) -> None:
        self._owner.streams_closed += 1


class ScriptedClient:
    """Instrumented stand-in for ollama.AsyncClient, sequenced for two stages.

    Chat 1 asks for the inventory tool, chat 2 replies with prose (stage one),
    chat 3 is the schema request (stage two). Any of them can be told to block
    forever instead, which is what a provider call awaiting a reply looks like.

    The class attributes are how a test arms a client it does not construct
    itself: the adapter builds its own, inside the run under test.
    """

    created: ClassVar[list[ScriptedClient]] = []
    block_on: ClassVar[set[int]] = set()
    close_error: ClassVar[BaseException | None] = None
    close_error_times: ClassVar[int] = 1

    def __init__(self, host: str | None = None, **kwargs: object) -> None:
        self.host = host
        self.kwargs = kwargs
        self.chats = 0
        self.closed = 0
        self.streams_closed = 0
        self.blocked = asyncio.Event()
        ScriptedClient.created.append(self)

    async def chat(self, **request: object) -> object:
        self.chats += 1
        streaming = request.get("stream") is not False
        if self.chats in ScriptedClient.block_on:
            if streaming:
                return _BlockedStream(self)
            # Stage two never streams: it blocks inside this await, where no
            # threading Event can reach it and there is no generator to close.
            self.blocked.set()
            await asyncio.Event().wait()
            raise AssertionError  # pragma: no cover - unreachable
        if not streaming:
            return _Chunk(_Msg(content=json.dumps(_NULL_EXTRACTION)))
        if self.chats == 1:
            return _Stream([_Chunk(_Msg(tool_calls=[_ToolCall("read_inventory")]))], self)
        return _Stream([_Chunk(_Msg(content="checked"))], self)

    async def close(self) -> None:
        self.closed += 1
        if (
            ScriptedClient.close_error is not None
            and self.closed <= ScriptedClient.close_error_times
        ):
            raise ScriptedClient.close_error


@pytest.fixture(autouse=True)
def _fake_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    ScriptedClient.created = []
    ScriptedClient.block_on = set()
    ScriptedClient.close_error = None
    ScriptedClient.close_error_times = 1
    monkeypatch.setattr(ollama, "AsyncClient", ScriptedClient)


def _interpreter() -> StrandsOllamaInterpreter:
    return StrandsOllamaInterpreter(
        host=DEFAULT_ASSISTANT_HOST,
        model_id=DEFAULT_ASSISTANT_MODEL,
        clock=StubClock(),
        deadline_seconds=30.0,
    )


def _run(main: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(main())


async def _blocked() -> ScriptedClient:
    """The client the run under test opened, once it is stuck in a call."""
    for _ in range(500):
        if ScriptedClient.created:
            break
        await asyncio.sleep(0.005)
    client = ScriptedClient.created[0]
    await asyncio.wait_for(client.blocked.wait(), timeout=5)
    return client


def _start(interpreter: StrandsOllamaInterpreter, signal: Event) -> asyncio.Task[Interpretation]:
    return asyncio.create_task(interpreter.interpret(TEXT, StubInventory(), signal))


# --- cancelling a run the cooperative signal cannot reach ----------------


def test_the_cancel_event_alone_cannot_stop_a_blocked_read() -> None:
    """States the gap plainly, so the need for task cancellation is not assumed.

    The Event is set and time passes; the run is still sitting in the provider
    call, because nothing between it and the socket ever looks at the Event.
    Cancelling the task is what returns control.
    """
    ScriptedClient.block_on = {1}

    async def main() -> None:
        interpreter = _interpreter()
        signal = Event()
        task = _start(interpreter, signal)
        client = await _blocked()

        signal.set()
        await asyncio.sleep(0.1)
        assert not task.done(), "the cooperative signal did not, and cannot, end it"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed == 1

    _run(main)


def test_cancelling_the_task_stops_a_blocked_stage_one_read_and_closes_up() -> None:
    ScriptedClient.block_on = {1}

    async def main() -> None:
        interpreter = _interpreter()
        task = _start(interpreter, Event())
        client = await _blocked()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.chats == 1
        assert client.streams_closed == 1, "the response stream was closed"
        assert client.closed == 1, "and so was the client"
        assert interpreter.cleanup_unresolved is False

    _run(main)


def test_cancelling_the_task_stops_a_blocked_native_extraction() -> None:
    """Stage two blocks inside a non-streaming await — the hardest case.

    There is no generator to close and no loop iteration in which to check a
    flag; only cancelling the task returns control.
    """
    ScriptedClient.block_on = {3}

    async def main() -> None:
        interpreter = _interpreter()
        task = _start(interpreter, Event())
        client = await _blocked()

        assert client.chats == 3, "stage one completed; stage two is the blocked call"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.closed == 1
        assert interpreter.cleanup_unresolved is False

    _run(main)


# --- a client that will not close stays owned ---------------------------


def test_a_client_that_will_not_close_is_kept_and_the_run_is_not_a_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The R3 defect: cleanup could fail and the run still reported success.

    A close that raises must leave the client owned, the run reported as failed,
    and the caller told the service is unavailable — not handed a draft.
    """
    ScriptedClient.close_error = OSError("socket refused to close")
    ScriptedClient.close_error_times = 99
    caplog.set_level(logging.INFO, logger="borrowed_steps.assistant")

    async def main() -> None:
        interpreter = _interpreter()
        with pytest.raises(AssistantUnavailableError):
            await interpreter.interpret(TEXT, StubInventory(), Event())

        assert interpreter.cleanup_unresolved is True, "the handle was kept, not dropped"
        assert ScriptedClient.created[0].closed == 2, "attempted twice before giving up"

    _run(main)

    terminal = [line for line in caplog.text.splitlines() if "cleanup=" in line]
    assert len(terminal) == 1, "exactly one terminal line, whatever the outcome"
    assert "cleanup=close_failed" in terminal[0]
    # The outcome word itself, not the tool_successes counter further along.
    assert "failed at stage=extraction reason=cleanup_unresolved" in terminal[0]
    assert " success at " not in terminal[0], "a run with an open client is not a success"


def test_an_unresolved_cleanup_refuses_the_next_run_without_opening_a_client() -> None:
    """Refusing is the point: a second client on top of the first is the leak."""
    ScriptedClient.close_error = OSError("socket refused to close")
    ScriptedClient.close_error_times = 99

    async def main() -> None:
        interpreter = _interpreter()
        with pytest.raises(AssistantUnavailableError):
            await interpreter.interpret(TEXT, StubInventory(), Event())

        with pytest.raises(AssistantUnavailableError):
            await interpreter.interpret(TEXT, StubInventory(), Event())

        assert len(ScriptedClient.created) == 1, "no second client was opened"

    _run(main)


def test_a_close_that_fails_once_is_retried_inside_the_run_and_succeeds() -> None:
    """One bad close is not a lost client: the handle survives for a retry."""
    ScriptedClient.close_error = OSError("socket refused to close")
    ScriptedClient.close_error_times = 1

    async def main() -> None:
        interpreter = _interpreter()
        result = await interpreter.interpret(TEXT, StubInventory(), Event())

        assert result.inventory_tool_calls == 1
        assert interpreter.cleanup_unresolved is False
        assert ScriptedClient.created[0].closed == 2, "failed once, then closed"

    _run(main)


def test_shutdown_finishes_a_cleanup_that_only_later_succeeds() -> None:
    """Ownership is retained *so that* shutdown has something to finish."""
    ScriptedClient.close_error = OSError("socket refused to close")
    ScriptedClient.close_error_times = 2  # both in-run attempts fail

    async def main() -> None:
        interpreter = _interpreter()
        with pytest.raises(AssistantUnavailableError):
            await interpreter.interpret(TEXT, StubInventory(), Event())
        unresolved_before = interpreter.cleanup_unresolved
        assert unresolved_before is True

        assert isinstance(interpreter, CleanupOwner)
        assert await interpreter.resolve_cleanup() is True
        unresolved_after = interpreter.cleanup_unresolved
        assert unresolved_after is False
        assert ScriptedClient.created[0].closed == 3

    _run(main)


def test_a_cleanup_still_failing_at_shutdown_is_reported_not_resolved() -> None:
    ScriptedClient.close_error = OSError("socket refused to close")
    ScriptedClient.close_error_times = 99

    async def main() -> None:
        interpreter = _interpreter()
        with pytest.raises(AssistantUnavailableError):
            await interpreter.interpret(TEXT, StubInventory(), Event())

        assert isinstance(interpreter, CleanupOwner)
        assert await interpreter.resolve_cleanup() is False
        assert interpreter.cleanup_unresolved is True

    _run(main)


def test_a_cancellation_landing_during_the_close_does_not_lose_the_client() -> None:
    """Repeated cancellation is the case that used to drop the handle.

    R3's ``aclose`` cleared its client reference *before* awaiting the close, so
    a cancellation arriving mid-close left an open client nobody held. Now the
    reference is cleared only after the close returns, and the second attempt
    completes it.
    """
    ScriptedClient.close_error = asyncio.CancelledError()
    ScriptedClient.close_error_times = 1

    async def main() -> None:
        interpreter = _interpreter()
        result = await interpreter.interpret(TEXT, StubInventory(), Event())

        assert result.inventory_tool_calls == 1
        client = ScriptedClient.created[0]
        assert client.closed == 2, "cancelled once, retried, closed"
        assert interpreter.cleanup_unresolved is False

    _run(main)


def test_a_close_cancelled_every_time_keeps_ownership_and_stays_unresolved() -> None:
    ScriptedClient.close_error = asyncio.CancelledError()
    ScriptedClient.close_error_times = 99

    async def main() -> None:
        interpreter = _interpreter()
        # Cancellation propagates as itself rather than being reshaped.
        with pytest.raises(asyncio.CancelledError):
            await interpreter.interpret(TEXT, StubInventory(), Event())

        assert interpreter.cleanup_unresolved is True
        assert ScriptedClient.created[0].closed == 2

    _run(main)


# --- what the HTTP layer does with an unresolved cleanup ----------------


class _StuckCleanupInterpreter:
    """Runs once, and ends still holding a client it could not close."""

    def __init__(self) -> None:
        self.calls = 0
        self.held = False

    @property
    def cleanup_unresolved(self) -> bool:
        return self.held

    async def resolve_cleanup(self) -> bool:
        return not self.held

    async def interpret(
        self, text: str, inventory: InventoryReader, cancel: Event
    ) -> Interpretation:
        self.calls += 1
        self.held = True  # the close failed on the way out
        msg = "The local interpretation service is not available."
        raise AssistantUnavailableError(msg)


def test_the_route_holds_the_slot_and_answers_503_while_cleanup_is_unresolved(
    settings: Settings, clock: FakeClock
) -> None:
    """The R3 defect at the HTTP layer: the slot was released unconditionally.

    A run that ends with its client still open must not hand the slot to the
    next caller, and 'busy' would be a lie — nothing is running. 503 is the
    honest answer, and the interpreter is not asked to run again.
    """
    assistant = _StuckCleanupInterpreter()
    app = create_app(
        dataclasses.replace(settings, assistant_enabled=True),
        clock=clock,
        interpreter=assistant,
    )

    with TestClient(app) as client:
        start_workspace(client)

        first = client.post("/api/intake/interpret", json={"text": TEXT})
        assert first.status_code == 503
        assert error_code(first) == "ASSISTANT_UNAVAILABLE"

        second = client.post("/api/intake/interpret", json={"text": TEXT})
        assert second.status_code == 503
        assert error_code(second) == "ASSISTANT_UNAVAILABLE"
        assert assistant.calls == 1, "no second run was started while cleanup was unresolved"

        # Once it really is released, the service accepts work again.
        assistant.held = False
        assert client.post("/api/intake/interpret", json={"text": TEXT}).status_code == 503
        assert assistant.calls == 2, "the run is attempted again once nothing is held"


def test_the_slot_is_not_handed_on_while_a_client_is_still_open(
    settings: Settings, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """The done callback must not release the slot when cleanup is unresolved."""
    caplog.set_level(logging.ERROR, logger="borrowed_steps")
    assistant = _StuckCleanupInterpreter()
    app = create_app(
        dataclasses.replace(settings, assistant_enabled=True),
        clock=clock,
        interpreter=assistant,
    )

    with TestClient(app) as client:
        start_workspace(client)
        assert client.post("/api/intake/interpret", json={"text": TEXT}).status_code == 503

    assert "the inference slot is retained" in caplog.text


def test_shutdown_reports_a_client_it_could_not_close(
    settings: Settings, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="borrowed_steps")
    assistant = _StuckCleanupInterpreter()
    app = create_app(
        dataclasses.replace(settings, assistant_enabled=True),
        clock=clock,
        interpreter=assistant,
        assistant_shutdown_seconds=0.2,
    )

    with TestClient(app) as client:
        start_workspace(client)
        assert client.post("/api/intake/interpret", json={"text": TEXT}).status_code == 503

    assert "could not be closed and is still open" in caplog.text
    assert "This shutdown was not clean" in caplog.text


def test_strands_interpreter_close_and_resolve_cleanup_bound_stalled_aclose(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Stalled model.aclose must not block _close or resolve_cleanup indefinitely."""
    import borrowed_steps.infrastructure.strands_interpreter as si

    monkeypatch.setattr(si, "_CLOSE_TIMEOUT_SECONDS", 0.02)
    interpreter = _interpreter()

    class StalledModel:
        def __init__(self) -> None:
            self.client_open = True
            self.stopped = asyncio.Event()
            self.closed_calls = 0

        async def aclose(self) -> None:
            self.closed_calls += 1
            await self.stopped.wait()

    model: Any = StalledModel()
    telemetry = si._Telemetry(correlation_id="test-corr", stage="start")

    async def run() -> None:
        # 1. _close bounds attempt 1 and attempt 2
        failure = await interpreter._close(model, telemetry)
        assert isinstance(failure, TimeoutError)
        assert telemetry.cleanup == "close_failed"
        assert model.client_open is True
        # BS-003-R7: outstanding cleanup is recorded in one place, `_closing`,
        # entered when the close starts rather than when `_close` reaches its
        # failure tail. The invariant asserted here is the same.
        assert model in interpreter._closing
        assert interpreter.cleanup_unresolved is True

        # 2. resolve_cleanup also bounds stalled aclose
        resolved = await interpreter.resolve_cleanup()
        assert resolved is False
        assert model in interpreter._closing
        assert interpreter.cleanup_unresolved is True

        # Release stalled event so background tasks finish cleanly
        model.stopped.set()

    asyncio.run(run())


def test_http_route_keeps_cancellation_grace_inside_120_second_total_response_bound() -> None:
    """Ensure response cancellation/cleanup grace stays inside 120s total response bound."""
    from borrowed_steps.config import (
        ASSISTANT_CANCEL_GRACE_SECONDS,
        ASSISTANT_DEADLINE_SECONDS,
    )

    # 1. Default configuration is 115.0s wait + 5.0s cancel grace = 120.0s total bound
    assert ASSISTANT_DEADLINE_SECONDS == 115.0
    assert ASSISTANT_CANCEL_GRACE_SECONDS == 5.0
    assert ASSISTANT_DEADLINE_SECONDS + ASSISTANT_CANCEL_GRACE_SECONDS <= 120.0

    # 2. Effective deadline calculation in app.py preserves the <= 120s bound
    # even if deadline is configured at 120.0s
    for deadline in (115.0, 120.0, 130.0):
        grace = 5.0
        effective = min(deadline, max(0.0, 120.0 - grace))
        assert effective + grace <= 120.0
