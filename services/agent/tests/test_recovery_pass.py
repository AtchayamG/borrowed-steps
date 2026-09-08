"""The single corrective continuation for a first pass that skipped the tool.

Deterministic stubs only. No model is invoked.
"""

from __future__ import annotations

import asyncio
from threading import Event
from typing import Any

import pytest

from borrowed_steps.application.errors import AssistantInvalidOutputError
from borrowed_steps.application.interpreter import (
    Interpretation,
    InventoryCount,
    InventoryReader,
)
from borrowed_steps.infrastructure import strands_interpreter as adapter
from test_strands_adapter import (
    TEXT,
    StubInventory,
    StubOwnedModel,
    _interpreter,
    _metrics,
    _Result,
)


class FailingInventory:
    """A workspace read that always fails, so attempts rise but successes do not."""

    def kind_state_counts(self) -> list[InventoryCount]:
        msg = "inventory unavailable"
        raise RuntimeError(msg)


def install_agent(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tool_calls_per_pass: tuple[int, ...],
    stop_reasons: tuple[str, ...] = ("end_turn", "end_turn"),
) -> dict[str, Any]:
    """Stub agent whose passes each run the tool a chosen number of times."""
    captured: dict[str, Any] = {"passes": 0, "prompts": []}

    class SequencedAgent:
        def __init__(self, **kwargs: object) -> None:
            tools = kwargs["tools"]
            assert isinstance(tools, list)
            self.read = tools[0]
            model = kwargs["model"]
            assert isinstance(model, StubOwnedModel)
            self.model = model

        async def invoke_async(self, prompt: str, **kwargs: object) -> _Result:
            index = captured["passes"]
            captured["passes"] += 1
            captured["prompts"].append(prompt)
            self.model.charge()
            runs = tool_calls_per_pass[index] if index < len(tool_calls_per_pass) else 0
            executed = 0
            for _ in range(runs):
                try:
                    self.read()
                    executed += 1
                except RuntimeError:
                    pass
            stop = stop_reasons[index] if index < len(stop_reasons) else "end_turn"
            return _Result(stop, _metrics(executed))

    monkeypatch.setattr(adapter, "Agent", SequencedAgent)
    monkeypatch.setattr(
        adapter,
        "OwnedOllamaModel",
        lambda **kwargs: StubOwnedModel(captured, **kwargs),
    )
    return captured


def _run(inventory: InventoryReader | None = None) -> Interpretation:
    return asyncio.run(_interpreter().interpret(TEXT, inventory or StubInventory(), Event()))


def test_a_first_pass_that_skipped_the_tool_gets_one_corrective_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = install_agent(monkeypatch, tool_calls_per_pass=(0, 1))

    result = _run()

    assert captured["passes"] == 2, "exactly one recovery, no more"
    assert "did not call read_inventory" in captured["prompts"][1]
    assert result.inventory_tool_calls == 1


def test_no_recovery_when_the_first_pass_already_used_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = install_agent(monkeypatch, tool_calls_per_pass=(1,))

    result = _run()

    assert captured["passes"] == 1
    assert result.inventory_tool_calls == 1


def test_still_no_tool_after_recovery_fails_the_interpretation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = install_agent(monkeypatch, tool_calls_per_pass=(0, 0))

    with pytest.raises(AssistantInvalidOutputError):
        _run()

    assert captured["passes"] == 2, "recovery is attempted once and only once"


def test_no_recovery_after_a_model_bound_was_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = install_agent(
        monkeypatch, tool_calls_per_pass=(0, 1), stop_reasons=("limit_turns", "end_turn")
    )

    with pytest.raises(AssistantInvalidOutputError):
        _run()

    assert captured["passes"] == 1, "a budget breach must not be retried"


def test_no_recovery_after_a_cancelled_run(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = install_agent(
        monkeypatch, tool_calls_per_pass=(0, 1), stop_reasons=("cancelled", "end_turn")
    )

    with pytest.raises(Exception):  # noqa: B017, PT011 - timeout error type asserted elsewhere
        _run()

    assert captured["passes"] == 1


def test_recovery_shares_the_tool_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two failed attempts exhaust the tool budget, so no recovery is possible."""
    captured = install_agent(monkeypatch, tool_calls_per_pass=(2, 1))

    with pytest.raises(AssistantInvalidOutputError):
        _run(FailingInventory())

    assert captured["passes"] == 1, "no room left in the shared tool budget"


def test_recovery_shares_one_owned_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = install_agent(monkeypatch, tool_calls_per_pass=(0, 1))
    _run()
    assert captured["passes"] == 2
    # One owned model, therefore one client, across both passes.
    assert isinstance(captured["owned_model_kwargs"], dict)
    assert captured["owned_model_kwargs"]["host"].startswith("http://127.0.0.1")


def test_recovery_and_extraction_are_charged_to_the_same_send_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three sends, one budget: first pass, corrective continuation, extraction."""
    captured = install_agent(monkeypatch, tool_calls_per_pass=(0, 1))

    _run()

    budget = captured["owned_model_kwargs"]["budget"]
    assert budget.sent == 3
    assert len(captured["extraction_calls"]) == 1
