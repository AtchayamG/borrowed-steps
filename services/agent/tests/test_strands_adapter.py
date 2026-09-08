"""The real Strands adapter over both stages, with stubs. No inference runs here.

These tests exercise the actual StrandsOllamaInterpreter code path: the tool it
builds, the bounds it enforces and the provenance it refuses to invent. Only the
SDK Agent (stage one) and the owned provider (stage two) are replaced, so
everything the adapter itself decides is under test.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from threading import Event
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from strands.telemetry.metrics import EventLoopMetrics, ToolMetrics
from strands.types.exceptions import StructuredOutputException

from borrowed_steps.application.errors import (
    AssistantInvalidOutputError,
    AssistantTimeoutError,
    AssistantUnavailableError,
)
from borrowed_steps.application.interpreter import Interpretation, InventoryCount
from borrowed_steps.domain.models import EquipmentKind
from borrowed_steps.infrastructure import strands_interpreter as adapter
from borrowed_steps.infrastructure.owned_ollama import RequestBudget
from borrowed_steps.infrastructure.strands_interpreter import StrandsOllamaInterpreter

NOW = datetime(2026, 9, 7, 9, 0, 0, tzinfo=UTC)
TEXT = "Meena R needs a wheelchair at the Velachery room by 2026-09-14T09:00:00Z."


class StubClock:
    def now(self) -> datetime:
        return NOW


class StubInventory:
    def __init__(self) -> None:
        self.reads = 0

    def kind_state_counts(self) -> list[InventoryCount]:
        self.reads += 1
        return [InventoryCount(kind="WHEELCHAIR", state="AVAILABLE", count=1)]


def _metrics(tool_successes: int, *, name: str = "read_inventory") -> EventLoopMetrics:
    metrics = EventLoopMetrics()
    if tool_successes >= 0:
        metrics.tool_metrics[name] = ToolMetrics(
            tool={"toolUseId": "t1", "name": name, "input": {}},
            call_count=max(tool_successes, 0),
            success_count=tool_successes,
            error_count=0,
            total_time=0.01,
        )
    return metrics


def _extraction(**overrides: object) -> BaseModel:
    fields: dict[str, Any] = {
        "borrower_label": "Meena R",
        "equipment_kind": "wheelchair",
        "pickup_location": "the Velachery room",
        "due_at": "2026-09-14T09:00:00Z",
    }
    fields.update(overrides)
    return adapter._Extraction(**fields)


class _Result:
    """Minimal stand-in for AgentResult, carrying only what the adapter reads.

    Deliberately has **no** ``structured_output`` attribute. Stage one is invoked
    without ``structured_output_model``, so an agent result that carried one
    would be a fiction; if the adapter ever reached for it again these tests
    would fail with AttributeError rather than quietly passing.
    """

    def __init__(self, stop_reason: str, metrics: EventLoopMetrics) -> None:
        self.stop_reason = stop_reason
        self.metrics = metrics


class StubOwnedModel:
    """Stands in for the owned provider across *both* stages.

    Stage two really runs through this object's ``structured_output``, so tests
    can assert exactly what the adapter sends and what it does with the reply.
    Every send — stage one and stage two — is charged against the same real
    ``RequestBudget`` the adapter constructed, so budget sharing is observable
    rather than assumed.
    """

    def __init__(
        self,
        captured: dict[str, Any],
        *,
        extraction: object | None = None,
        extraction_raises: Exception | None = None,
        yield_output: bool = True,
        close_raises: BaseException | None = None,
        close_raises_times: int = 1,
        **kwargs: object,
    ) -> None:
        self.kwargs = kwargs
        self.budget = kwargs.get("budget")
        self.closed = False
        self.close_attempts = 0
        self._captured = captured
        self._extraction = extraction
        self._extraction_raises = extraction_raises
        self._yield_output = yield_output
        self._close_raises = close_raises
        self._close_raises_times = close_raises_times
        captured["owned_model_kwargs"] = kwargs
        captured.setdefault("extraction_calls", [])
        captured["owned_model"] = self

    @property
    def client_open(self) -> bool:
        """Mirrors the real provider: open until a close actually succeeds."""
        return not self.closed

    def charge(self) -> None:
        """Charge one send, exactly as a real transport request would."""
        budget = self.budget
        if isinstance(budget, RequestBudget):
            budget.charge()

    async def __aenter__(self) -> StubOwnedModel:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close, or fail to — leaving the handle intact, as the real one does."""
        self.close_attempts += 1
        if self._close_raises is not None and self.close_attempts <= self._close_raises_times:
            raise self._close_raises
        self.closed = True

    async def structured_output(
        self,
        output_model: type[BaseModel],
        prompt: object,
        system_prompt: str | None = None,
        **kwargs: object,
    ) -> AsyncGenerator[dict[str, Any], None]:
        self._captured["extraction_calls"].append(
            {
                "output_model": output_model,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "kwargs": kwargs,
            }
        )
        self.charge()
        if self._extraction_raises is not None:
            raise self._extraction_raises
        if self._yield_output:
            yield {"output": _extraction() if self._extraction is None else self._extraction}


def install_stub_agent(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stop_reason: str = "end_turn",
    tool_successes: int = 1,
    structured: object | None = None,
    yield_output: bool = True,
    raises: Exception | None = None,
    extraction_raises: Exception | None = None,
    close_raises: BaseException | None = None,
    close_raises_times: int = 1,
    run_tool: bool = True,
    set_cancel: bool = False,
) -> dict[str, Any]:
    """Replace the SDK Agent and the owned model with stubs, and report captures.

    ``structured``/``yield_output``/``extraction_raises`` drive **stage two**,
    which is where structured output now comes from.
    """
    captured: dict[str, Any] = {}

    class StubAgent:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def invoke_async(self, prompt: str, **kwargs: object) -> _Result:
            captured["prompt"] = prompt
            captured["invoke_kwargs"] = kwargs
            model = captured["owned_model"]
            assert isinstance(model, StubOwnedModel)
            model.charge()
            if run_tool:
                for _ in range(max(tool_successes, 0)):
                    captured.setdefault("tool_returns", []).append(captured["tools"][0]())
            if set_cancel:
                signal = kwargs["cancel_signal"]
                assert isinstance(signal, Event)
                signal.set()
            if raises is not None:
                raise raises
            return _Result(stop_reason, _metrics(tool_successes))

    monkeypatch.setattr(adapter, "Agent", StubAgent)
    monkeypatch.setattr(
        adapter,
        "OwnedOllamaModel",
        lambda **kwargs: StubOwnedModel(
            captured,
            extraction=structured,
            extraction_raises=extraction_raises,
            yield_output=yield_output,
            close_raises=close_raises,
            close_raises_times=close_raises_times,
            **kwargs,
        ),
    )
    return captured


def _interpreter() -> StrandsOllamaInterpreter:
    return StrandsOllamaInterpreter(
        host="http://127.0.0.1:11434",
        model_id="llama3.2:3b",
        clock=StubClock(),
    )


async def _run(interpreter: StrandsOllamaInterpreter, inventory: StubInventory) -> Interpretation:
    return await interpreter.interpret(TEXT, inventory, Event())


def test_happy_path_grounds_every_field_and_builds_real_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = install_stub_agent(monkeypatch)
    inventory = StubInventory()

    result = asyncio.run(_run(_interpreter(), inventory))

    assert result.draft.borrower_label == "Meena R"
    assert result.draft.equipment_kind is EquipmentKind.WHEELCHAIR
    assert result.draft.pickup_location == "the Velachery room"
    assert result.draft.due_at == datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    assert result.missing_fields == ()
    assert (result.framework, result.provider, result.model) == (
        "strands",
        "ollama",
        "llama3.2:3b",
    )
    assert result.inventory_tool_calls == 1
    assert result.completed_at == NOW
    assert inventory.reads == 1

    # A fresh agent, with exactly one read-only tool and no printing handler.
    assert captured["callback_handler"] is None
    assert captured["load_tools_from_directory"] is False
    assert len(captured["tools"]) == 1

    # Real SDK bounds were passed, not invented ones.
    assert captured["invoke_kwargs"]["limits"] == {"turns": 6}


def test_stage_one_asks_for_tools_only_and_never_a_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replaces the R2 assertion that stage one carried structured_output_model.

    The approved design separates tool selection from schema generation, so the
    agent invocation must carry no output model at all. The schema request is
    asserted separately, in the stage-two tests below.
    """
    captured = install_stub_agent(monkeypatch)

    asyncio.run(_run(_interpreter(), StubInventory()))

    assert "structured_output_model" not in captured["invoke_kwargs"]
    assert TEXT in captured["prompt"]
    assert "read_inventory" in captured["prompt"]


def test_stage_two_makes_exactly_one_schema_request_from_the_source_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extraction request carries the intake text and nothing else.

    No agent conversation, tool result or inventory count may travel with it,
    or the model could echo one back as if the message had said it.
    """
    captured = install_stub_agent(monkeypatch)
    inventory = StubInventory()

    asyncio.run(_run(_interpreter(), inventory))

    calls = captured["extraction_calls"]
    assert len(calls) == 1, "exactly one schema request per interpretation"
    call = calls[0]
    assert call["output_model"] is adapter._Extraction
    # BS-003-R11: the system prompt now carries the generated schema after the
    # same approved rules. This test's subject - that the *user* message is the
    # source and nothing else - is unchanged; the outgoing prompt itself is
    # examined in tests/test_extraction_request.py.
    assert call["system_prompt"] == adapter._EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA

    prompt = call["prompt"]
    assert prompt == [
        {"role": "user", "content": [{"text": adapter._EXTRACTION_USER_PROMPT.format(text=TEXT)}]}
    ]
    rendered = repr(prompt)
    assert TEXT in rendered
    for leak in ("read_inventory", "AVAILABLE", "counts", "assistant"):
        assert leak not in rendered, f"stage two must not carry {leak!r}"


def test_both_stages_are_charged_against_one_send_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = install_stub_agent(monkeypatch)

    asyncio.run(_run(_interpreter(), StubInventory()))

    budget = captured["owned_model_kwargs"]["budget"]
    assert budget.sent == 2, "one agent send plus one extraction send"
    assert budget.limit == 6


def test_one_owned_client_serves_both_stages_and_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = install_stub_agent(monkeypatch)

    asyncio.run(_run(_interpreter(), StubInventory()))

    model = captured["owned_model"]
    assert captured["model"] is model, "stage one used the owned client"
    assert model.closed is True, "the owned client was closed on the way out"


def test_inventory_tool_takes_no_workspace_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = install_stub_agent(monkeypatch)

    asyncio.run(_run(_interpreter(), StubInventory()))

    spec = captured["tools"][0].tool_spec
    assert spec["name"] == "read_inventory"
    assert spec["inputSchema"]["json"]["properties"] == {}

    payload = captured["tool_returns"][0]
    assert payload == {"counts": [{"kind": "WHEELCHAIR", "state": "AVAILABLE", "count": 1}]}
    for row in payload["counts"]:
        assert set(row) == {"kind", "state", "count"}


def test_no_tool_execution_is_a_failure_not_a_fabricated_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_stub_agent(monkeypatch, tool_successes=0, run_tool=False)

    with pytest.raises(AssistantInvalidOutputError):
        asyncio.run(_run(_interpreter(), StubInventory()))


def test_missing_tool_metrics_entirely_is_also_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_stub_agent(monkeypatch, tool_successes=-1, run_tool=False)

    with pytest.raises(AssistantInvalidOutputError):
        asyncio.run(_run(_interpreter(), StubInventory()))


def test_too_many_tool_executions_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stub_agent(monkeypatch, tool_successes=3, run_tool=False)

    with pytest.raises(AssistantInvalidOutputError):
        asyncio.run(_run(_interpreter(), StubInventory()))


def test_the_tool_itself_refuses_a_third_call(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = install_stub_agent(monkeypatch, tool_successes=2)

    asyncio.run(_run(_interpreter(), StubInventory()))
    read_inventory = captured["tools"][0]
    with pytest.raises(RuntimeError):
        read_inventory()


@pytest.mark.parametrize(
    "stop_reason",
    ["limit_turns", "limit_output_tokens", "limit_total_tokens", "max_tokens"],
)
def test_hitting_a_model_bound_is_an_invalid_output(
    monkeypatch: pytest.MonkeyPatch, stop_reason: str
) -> None:
    install_stub_agent(monkeypatch, stop_reason=stop_reason)

    with pytest.raises(AssistantInvalidOutputError):
        asyncio.run(_run(_interpreter(), StubInventory()))


def test_cancelled_run_is_reported_as_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stub_agent(monkeypatch, stop_reason="cancelled")

    with pytest.raises(AssistantTimeoutError):
        asyncio.run(_run(_interpreter(), StubInventory()))


def test_a_set_cancel_signal_is_reported_as_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stub_agent(monkeypatch, set_cancel=True)

    with pytest.raises(AssistantTimeoutError):
        asyncio.run(_run(_interpreter(), StubInventory()))


def test_malformed_structured_output_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage two yielding something that is not the schema type must fail."""
    install_stub_agent(monkeypatch, structured="not a model")

    with pytest.raises(AssistantInvalidOutputError):
        asyncio.run(_run(_interpreter(), StubInventory()))


def test_absent_structured_output_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage two ending without an output event must fail, not default to null."""
    install_stub_agent(monkeypatch, yield_output=False)

    with pytest.raises(AssistantInvalidOutputError):
        asyncio.run(_run(_interpreter(), StubInventory()))


def test_a_structured_output_error_is_an_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider refusing to honour the schema is a failure, not a blank draft."""
    install_stub_agent(monkeypatch, extraction_raises=StructuredOutputException("schema refused"))

    with pytest.raises(AssistantInvalidOutputError):
        asyncio.run(_run(_interpreter(), StubInventory()))


def test_a_provider_error_during_extraction_maps_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_stub_agent(monkeypatch, extraction_raises=httpx.ConnectError("refused"))

    with pytest.raises(AssistantUnavailableError):
        asyncio.run(_run(_interpreter(), StubInventory()))


def test_a_run_with_no_send_left_never_reaches_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exhausted send budget fails the run instead of guessing the fields.

    The budget is exhausted by stage one here, so ``_require_real_inventory``
    is what refuses; ``_extract``'s own capacity check is a second, defensive
    guard on the same invariant and is not what this test exercises.
    """
    captured = install_stub_agent(monkeypatch)
    interpreter = StrandsOllamaInterpreter(
        host="http://127.0.0.1:11434",
        model_id="llama3.2:3b",
        clock=StubClock(),
        max_model_requests=1,  # stage one consumes the only send
    )

    with pytest.raises(AssistantInvalidOutputError):
        asyncio.run(_run(interpreter, StubInventory()))

    assert captured["extraction_calls"] == [], "no schema request was attempted"


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("refused"),
        httpx.ConnectError("refused"),
        OSError("socket gone"),
    ],
)
def test_provider_failures_map_to_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    install_stub_agent(monkeypatch, raises=error, run_tool=False)

    with pytest.raises(AssistantUnavailableError):
        asyncio.run(_run(_interpreter(), StubInventory()))


def test_ungrounded_values_become_null_and_are_reported_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that invents a borrower, a date or a kind gets nulls, not a draft."""
    install_stub_agent(
        monkeypatch,
        structured=_extraction(
            borrower_label="Someone Else",
            due_at="next Tuesday",
            equipment_kind="walker",
        ),
    )

    result = asyncio.run(_run(_interpreter(), StubInventory()))

    assert result.draft.borrower_label is None
    assert result.draft.equipment_kind is None
    assert result.draft.due_at is None
    assert result.draft.pickup_location == "the Velachery room"
    assert result.missing_fields == ("borrower_label", "equipment_kind", "due_at")
