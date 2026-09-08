"""Pinned provider, invocation-level tool budget, and finite stall/cancel bounds.

Every test here uses a local fake transport or a stubbed agent. **No model is
invoked and no network call is made.** Where a test drives the real Strands
agent loop, it does so against a fake ``Model`` implementation, which is stated
in that test's docstring.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from datetime import UTC, datetime
from threading import Event
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from strands import Agent, tool
from strands.agent.agent_result import AgentResult
from strands.event_loop._retry import ModelRetryStrategy
from strands.models.model import Model
from strands.types.agent import Limits
from strands.types.exceptions import ModelThrottledException
from typing_extensions import Buffer

from borrowed_steps.application.errors import (
    AssistantInvalidOutputError,
    AssistantTimeoutError,
)
from borrowed_steps.config import (
    ASSISTANT_MAX_MODEL_REQUESTS,
    ASSISTANT_TRANSPORT_TIMEOUT_SECONDS,
    DEFAULT_ASSISTANT_HOST,
    DEFAULT_ASSISTANT_MODEL,
    Settings,
    load_settings,
)
from borrowed_steps.infrastructure import strands_interpreter as adapter
from borrowed_steps.infrastructure.owned_ollama import (
    LOCAL_AUTHORIZATION_MARKER,
    OwnedOllamaModel,
    RequestBudget,
)
from borrowed_steps.infrastructure.strands_interpreter import StrandsOllamaInterpreter
from conftest import FakeClock
from support import body, error_code, start_workspace
from test_strands_adapter import (
    TEXT,
    StubClock,
    StubInventory,
    _interpreter,
    _metrics,
    _Result,
    install_stub_agent,
)

# --- 1. the local provider is pinned ------------------------------------


def test_environment_cannot_move_the_assistant_off_its_pinned_provider() -> None:
    """A remote host or a different model must be refused, never silently used."""
    for key, value in (
        ("BS_ASSISTANT_HOST", "https://example.invalid"),
        ("BS_ASSISTANT_MODEL", "other-model"),
    ):
        with pytest.raises(ValueError, match="cannot be changed"):
            load_settings({"BS_ASSISTANT_ENABLED": "true", key: value})


def test_pinned_values_are_used_when_no_override_is_present() -> None:
    settings = load_settings({"BS_ASSISTANT_ENABLED": "true"})
    assert settings.assistant_host == DEFAULT_ASSISTANT_HOST
    assert settings.assistant_model == DEFAULT_ASSISTANT_MODEL


def test_an_override_equal_to_the_pinned_value_is_accepted() -> None:
    settings = load_settings(
        {
            "BS_ASSISTANT_HOST": DEFAULT_ASSISTANT_HOST,
            "BS_ASSISTANT_MODEL": DEFAULT_ASSISTANT_MODEL,
        }
    )
    assert settings.assistant_host == DEFAULT_ASSISTANT_HOST
    assert settings.assistant_model == DEFAULT_ASSISTANT_MODEL


def test_the_adapter_is_built_on_the_pinned_endpoint_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = install_stub_agent(monkeypatch)
    asyncio.run(_interpreter().interpret(TEXT, StubInventory(), Event()))
    model_kwargs = captured["owned_model_kwargs"]
    assert model_kwargs["host"] == DEFAULT_ASSISTANT_HOST
    assert model_kwargs["model_id"] == DEFAULT_ASSISTANT_MODEL


# --- 2. the tool budget bounds the whole invocation ----------------------


def test_a_refused_third_tool_attempt_fails_the_whole_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK turns a tool exception into a result and carries on.

    Two successful reads plus a refused third must still fail the invocation,
    even though the run then returns valid structured output with
    ``success_count == 2``.
    """

    class BudgetBreachingAgent:
        def __init__(self, **kwargs: object) -> None:
            tools = kwargs["tools"]
            assert isinstance(tools, list)
            self.read = tools[0]

        async def invoke_async(self, *args: object, **kwargs: object) -> _Result:
            self.read()
            self.read()
            # Exactly what the SDK does with an ordinary tool exception.
            with contextlib.suppress(RuntimeError):
                self.read()
            return _Result("end_turn", _metrics(2))

    monkeypatch.setattr(adapter, "Agent", BudgetBreachingAgent)

    with pytest.raises(AssistantInvalidOutputError):
        asyncio.run(_interpreter().interpret(TEXT, StubInventory(), Event()))


def test_two_tool_reads_remain_acceptable(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stub_agent(monkeypatch, tool_successes=2)
    result = asyncio.run(_interpreter().interpret(TEXT, StubInventory(), Event()))
    assert result.inventory_tool_calls == 2


# --- 3. finite transport, stall and cancellation bounds ------------------


def test_a_finite_transport_timeout_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ollama client defaults to no timeout; the adapter must set one."""
    captured = install_stub_agent(monkeypatch)
    asyncio.run(_interpreter().interpret(TEXT, StubInventory(), Event()))
    assert captured["owned_model_kwargs"]["timeout_seconds"] == ASSISTANT_TRANSPORT_TIMEOUT_SECONDS

    # ...and the owned provider turns that into real, pinned client arguments.
    owned = OwnedOllamaModel(
        host=DEFAULT_ASSISTANT_HOST,
        model_id=DEFAULT_ASSISTANT_MODEL,
        budget=RequestBudget(6),
        timeout_seconds=ASSISTANT_TRANSPORT_TIMEOUT_SECONDS,
    )
    assert owned.client_args == {
        "timeout": ASSISTANT_TRANSPORT_TIMEOUT_SECONDS,
        "follow_redirects": False,
        "trust_env": False,
        "headers": {"Authorization": LOCAL_AUTHORIZATION_MARKER},
    }
    assert owned.host == DEFAULT_ASSISTANT_HOST
    assert owned.get_config()["model_id"] == DEFAULT_ASSISTANT_MODEL


class _Owned:
    """Lifecycle surface the adapter needs from its owned provider.

    The adapter owns one provider object for the whole interpretation and closes
    it on every exit path, so a fake model standing in for that provider must
    offer the same entry/close surface rather than being wrapped in one.
    """

    def __init__(self) -> None:
        self.closed = 0

    @property
    def client_open(self) -> bool:
        """Mirrors the real provider: open until a close actually succeeds."""
        return self.closed == 0

    async def __aenter__(self) -> _Owned:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self.closed += 1


class _StallingModel(_Owned, Model):
    """Fake transport that accepts the call and then never yields a chunk."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = False

    def get_config(self) -> dict[str, object]:
        return {}

    def update_config(self, **model_config: object) -> None:
        return None

    async def structured_output(
        self,
        output_model: type[BaseModel],
        prompt: object,
        system_prompt: object | None = None,
        **kwargs: object,
    ) -> AsyncGenerator[dict[str, Any], None]:
        await self._stall()
        yield {}

    async def stream(self, *args: object, **kwargs: object) -> AsyncIterable[Any]:
        await self._stall()
        yield {}

    async def _stall(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()  # never set: a no-chunk stall
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_a_stalled_transport_is_bounded_and_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that never sends a chunk must not hold the slot forever.

    Uses the real Strands agent loop against a fake stalling Model. No inference.
    """
    stalling = _StallingModel()
    monkeypatch.setattr(adapter, "OwnedOllamaModel", lambda **kwargs: stalling)

    interpreter = StrandsOllamaInterpreter(
        host=DEFAULT_ASSISTANT_HOST,
        model_id=DEFAULT_ASSISTANT_MODEL,
        clock=StubClock(),
        deadline_seconds=0.3,
    )

    async def run() -> None:
        with pytest.raises(AssistantTimeoutError):
            await interpreter.interpret(TEXT, StubInventory(), Event())

    started = datetime.now(UTC)
    asyncio.run(run())
    elapsed = (datetime.now(UTC) - started).total_seconds()

    assert elapsed < 5, "the deadline did not bound the stall"
    assert stalling.started.is_set()
    assert stalling.cancelled, "the stalled run was not actually cancelled"


def test_the_slot_recovers_after_a_stalled_run(
    settings: Settings, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a bounded stall the service accepts work again: no orphan, no leak.

    Real agent loop, fake stalling Model, then a stubbed agent for the recovery
    call. No inference at any point.
    """
    import dataclasses

    from borrowed_steps.interfaces.http.app import create_app

    stalling = _StallingModel()
    monkeypatch.setattr(adapter, "OwnedOllamaModel", lambda **kwargs: stalling)

    interpreter = StrandsOllamaInterpreter(
        host=DEFAULT_ASSISTANT_HOST,
        model_id=DEFAULT_ASSISTANT_MODEL,
        clock=StubClock(),
        deadline_seconds=0.3,
    )
    app = create_app(
        dataclasses.replace(settings, assistant_enabled=True),
        clock=clock,
        interpreter=interpreter,
        assistant_deadline_seconds=5.0,
    )

    with TestClient(app) as client:
        start_workspace(client)
        stalled = client.post("/api/intake/interpret", json={"text": TEXT})
        assert stalled.status_code == 504
        assert error_code(stalled) == "ASSISTANT_TIMEOUT"
        assert stalling.cancelled

        # The slot is free again, so a second caller is served rather than 429.
        install_stub_agent(monkeypatch)
        recovered = client.post("/api/intake/interpret", json={"text": TEXT})
        assert recovered.status_code == 200
        assert body(recovered)["provenance"]["inventory_tool_calls"] == 1

        assert client.get("/api/snapshot").json()["events"] == []


def test_caller_cancellation_propagates_and_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outer cancel must cancel the run, not be converted into a result."""
    stalling = _StallingModel()
    monkeypatch.setattr(adapter, "OwnedOllamaModel", lambda **kwargs: stalling)

    interpreter = StrandsOllamaInterpreter(
        host=DEFAULT_ASSISTANT_HOST,
        model_id=DEFAULT_ASSISTANT_MODEL,
        clock=StubClock(),
        deadline_seconds=30.0,
    )

    async def run() -> None:
        task = asyncio.create_task(interpreter.interpret(TEXT, StubInventory(), Event()))
        await asyncio.wait_for(stalling.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert stalling.cancelled


# --- actual model attempts, not just the argument we pass ----------------


class _CountingModel(Model):
    """Fake transport that always asks for a tool, counting every real attempt."""

    def __init__(self) -> None:
        self.attempts = 0

    def get_config(self) -> dict[str, object]:
        return {}

    def update_config(self, **model_config: object) -> None:
        return None

    async def structured_output(
        self,
        output_model: type[BaseModel],
        prompt: object,
        system_prompt: object | None = None,
        **kwargs: object,
    ) -> AsyncGenerator[dict[str, Any], None]:
        self.attempts += 1
        yield {"output": output_model()}

    async def stream(self, *args: object, **kwargs: object) -> AsyncIterable[Any]:
        self.attempts += 1
        tool_use_id = f"tooluse_{uuid.uuid4().hex[:24]}"
        yield {"messageStart": {"role": "assistant"}}
        yield {
            "contentBlockStart": {
                "start": {"toolUse": {"name": "count_me", "toolUseId": tool_use_id}}
            }
        }
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


OLLAMA_PORT = 11434


class _Empty(BaseModel):
    pass


def test_limits_turns_bounds_loop_cycles_not_transport_retries() -> None:
    """Correction to a BS-003-R1 claim.

    The R1 test that carried the "including SDK retries" name never raised a
    retryable error, so it only ever proved that a well-behaved model is called
    once per loop cycle. It did **not** prove a bound on outgoing requests. This
    test states only what it really shows: with nothing retryable happening,
    ``Limits(turns=N)`` gives N model invocations. The real send bound lives in
    ``tests/test_owned_transport.py``, which counts requests at the transport.
    """

    @tool(name="count_me", description="does nothing")
    def count_me() -> dict[str, Any]:
        """Nothing."""
        return {}

    counting = _CountingModel()

    async def run() -> AgentResult:
        agent = Agent(
            model=counting,
            tools=[count_me],
            callback_handler=None,
            load_tools_from_directory=False,
        )
        return await agent.invoke_async(
            "go",
            structured_output_model=_Empty,
            limits=Limits(turns=ASSISTANT_MAX_MODEL_REQUESTS),
        )

    result = asyncio.run(run())

    assert counting.attempts == ASSISTANT_MAX_MODEL_REQUESTS
    assert result.stop_reason == "limit_turns"


class _ThrottlingModel(_CountingModel):
    """Fake model that throttles on alternate attempts, as Codex's repro does."""

    def __init__(self) -> None:
        super().__init__()
        self.raw_attempts = 0

    async def stream(self, *args: object, **kwargs: object) -> AsyncIterable[Any]:
        self.raw_attempts += 1
        if self.raw_attempts % 2:
            msg = "synthetic throttle"
            raise ModelThrottledException(msg)
        async for event in super().stream(*args, **kwargs):
            yield event


def test_default_retry_strategy_sends_more_requests_than_the_turn_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex's saved reproduction: Limits(turns) does not bound raw attempts.

    Adapted from reviews/BS-003-R1-retry-regression.py. With the SDK's default
    retry strategy a throttled model is retried, so raw attempts exceed the turn
    limit. Retry delay is disabled in the test only. This documents the gap the
    owned request budget exists to close; the exact count is SDK-dependent, so
    only the inequality is asserted.
    """
    monkeypatch.setattr(ModelRetryStrategy, "_calculate_delay", lambda self, attempt: 0)
    model = _ThrottlingModel()

    async def run() -> AgentResult:
        agent = Agent(model=model, callback_handler=None, load_tools_from_directory=False)
        return await agent.invoke_async(
            "synthetic", structured_output_model=_Empty, limits=Limits(turns=6)
        )

    asyncio.run(run())

    assert model.raw_attempts > ASSISTANT_MAX_MODEL_REQUESTS


def test_disabling_the_retry_strategy_keeps_attempts_within_the_turn_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mitigation the adapter applies: retry_strategy=None, as in production."""
    monkeypatch.setattr(ModelRetryStrategy, "_calculate_delay", lambda self, attempt: 0)
    model = _ThrottlingModel()

    async def run() -> AgentResult:
        agent = Agent(
            model=model,
            callback_handler=None,
            load_tools_from_directory=False,
            retry_strategy=None,
        )
        return await agent.invoke_async(
            "synthetic", structured_output_model=_Empty, limits=Limits(turns=6)
        )

    with pytest.raises(ModelThrottledException):
        asyncio.run(run())

    assert model.raw_attempts <= ASSISTANT_MAX_MODEL_REQUESTS


def test_the_adapter_refuses_a_run_that_hit_the_model_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_stub_agent(monkeypatch, stop_reason="limit_turns")
    with pytest.raises(AssistantInvalidOutputError):
        asyncio.run(_interpreter().interpret(TEXT, StubInventory(), Event()))


@pytest.fixture(autouse=True)
def _no_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard guarantee that no test in this module can reach the Ollama endpoint.

    Only the provider port is blocked. The event loop's own loopback socketpair
    on Windows still has to work.
    """
    import socket

    original = socket.socket.connect

    def guarded(
        self: socket.socket,
        address: tuple[Any, ...] | str | Buffer,
        *args: object,
        **kwargs: object,
    ) -> object:
        if isinstance(address, tuple) and len(address) >= 2 and int(address[1]) == OLLAMA_PORT:
            msg = "these tests must never contact the local model provider"
            raise AssertionError(msg)
        return original(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)
