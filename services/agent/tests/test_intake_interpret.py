"""POST /api/intake/interpret: contract shape, protection, limits and read-only behaviour.

Every test here uses a deterministic fake interpreter. No real inference runs in
ordinary pytest; the live proof is a separately labelled script.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from borrowed_steps.application.errors import (
    AssistantInvalidOutputError,
    AssistantUnavailableError,
)
from borrowed_steps.application.interpreter import (
    DraftRequest,
    Interpretation,
    InventoryCount,
    InventoryReader,
)
from borrowed_steps.config import Settings
from borrowed_steps.domain.models import EquipmentKind
from borrowed_steps.interfaces.http.app import create_app
from conftest import FakeClock
from support import (
    ALLOWED_ORIGIN,
    FOREIGN_ORIGIN,
    NOW,
    body,
    create_request,
    equipment_of,
    error_code,
    reserve,
    snapshot,
    start_workspace,
)

INTERPRET_URL = "/api/intake/interpret"
SAMPLE_TEXT = "Meena R needs a wheelchair at the Velachery room by 2026-09-14T09:00:00Z."


def _interpretation(
    *,
    borrower_label: str | None = "Meena R",
    equipment_kind: EquipmentKind | None = EquipmentKind.WHEELCHAIR,
    pickup_location: str | None = "the Velachery room",
    due_at: datetime | None = None,
    missing: tuple[str, ...] = (),
    tool_calls: int = 1,
) -> Interpretation:
    return Interpretation(
        draft=DraftRequest(
            borrower_label=borrower_label,
            equipment_kind=equipment_kind,
            pickup_location=pickup_location,
            due_at=NOW + timedelta(days=7) if due_at is None else due_at,
        ),
        missing_fields=missing,
        framework="strands",
        provider="ollama",
        model="llama3.2:3b",
        inventory_tool_calls=tool_calls,
        completed_at=NOW,
    )


class FakeInterpreter:
    """Deterministic stand-in for the real Strands adapter."""

    def __init__(
        self,
        *,
        result: Interpretation | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
        delay_calls: int | None = None,
        resist_cancel: bool = False,
    ) -> None:
        self.result = result if result is not None else _interpretation()
        self.error = error
        self.delay = delay
        self.delay_calls = delay_calls
        self.resist_cancel = resist_cancel
        self.calls = 0
        self.seen_text: list[str] = []
        self.seen_counts: list[list[InventoryCount]] = []
        self.cancelled = False

    async def interpret(
        self, text: str, inventory: InventoryReader, cancel: Event
    ) -> Interpretation:
        self.calls += 1
        self.seen_text.append(text)
        self.seen_counts.append(inventory.kind_state_counts())
        slow = self.delay_calls is None or self.calls <= self.delay_calls
        if self.delay and slow:
            if self.resist_cancel:
                # Stands in for a run that cannot be interrupted where it is —
                # a provider call already awaiting a reply. It records the
                # cancellation and finishes its own way, holding the slot.
                await self._resist(self.delay)
            else:
                try:
                    await asyncio.sleep(self.delay)
                except asyncio.CancelledError:
                    # Recorded, then propagated: a cancelled run really ends.
                    self.cancelled = True
                    raise
        self.cancelled = self.cancelled or cancel.is_set()
        if self.error is not None:
            raise self.error
        return self.result

    async def _resist(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                self.cancelled = True


def _enabled(settings: Settings) -> Settings:
    return dataclasses.replace(settings, assistant_enabled=True)


def _client(
    settings: Settings,
    clock: FakeClock,
    interpreter: FakeInterpreter | None = None,
    *,
    deadline: float = 5.0,
    cancel_grace: float = 5.0,
) -> Iterator[TestClient]:
    app = create_app(
        _enabled(settings),
        clock=clock,
        interpreter=interpreter if interpreter is not None else FakeInterpreter(),
        assistant_deadline_seconds=deadline,
        assistant_cancel_grace_seconds=cancel_grace,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def assistant(settings: Settings, clock: FakeClock) -> Iterator[tuple[TestClient, FakeInterpreter]]:
    fake = FakeInterpreter()
    for client in _client(settings, clock, fake):
        yield client, fake


def _interpret(
    client: TestClient, text: str = SAMPLE_TEXT, headers: dict[str, str] | None = None
) -> httpx.Response:
    response: httpx.Response = client.post(INTERPRET_URL, json={"text": text}, headers=headers)
    return response


# --- configuration ------------------------------------------------------


def test_assistant_is_disabled_by_default(client: TestClient) -> None:
    start_workspace(client)
    response = _interpret(client)
    assert response.status_code == 503
    assert error_code(response) == "ASSISTANT_DISABLED"


def test_disabled_startup_keeps_the_structured_workflow_usable(client: TestClient) -> None:
    """With no provider at all, every M1 route still works end to end."""
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]
    reserved = reserve(
        client, request_id=request_id, equipment_id=wheelchair["id"], expected_version=1
    )
    assert reserved.status_code == 200
    assert snapshot(client)["agent_mode"] == "disabled"


def test_enabled_mode_is_reported_in_health_and_snapshot(
    assistant: tuple[TestClient, FakeInterpreter],
) -> None:
    client, _ = assistant
    health = body(client.get("/api/health"))
    assert health == {"status": "ok", "milestone": "M2B", "agent_mode": "strands_ollama"}
    start_workspace(client)
    assert snapshot(client)["agent_mode"] == "strands_ollama"


# --- protection ---------------------------------------------------------


def test_interpretation_requires_a_session(
    assistant: tuple[TestClient, FakeInterpreter],
) -> None:
    client, fake = assistant
    response = _interpret(client)
    assert response.status_code == 401
    assert error_code(response) == "SESSION_REQUIRED"
    assert fake.calls == 0


def test_interpretation_rejects_a_foreign_origin(
    assistant: tuple[TestClient, FakeInterpreter],
) -> None:
    client, fake = assistant
    start_workspace(client)
    response = _interpret(client, headers={"Origin": FOREIGN_ORIGIN})
    assert response.status_code == 403
    assert error_code(response) == "ORIGIN_FORBIDDEN"
    assert fake.calls == 0


def test_interpretation_accepts_the_configured_origin(
    assistant: tuple[TestClient, FakeInterpreter],
) -> None:
    client, _ = assistant
    start_workspace(client)
    assert _interpret(client, headers={"Origin": ALLOWED_ORIGIN}).status_code == 200


def test_inventory_is_bound_to_the_caller_workspace(settings: Settings, clock: FakeClock) -> None:
    fake = FakeInterpreter()
    app = create_app(_enabled(settings), clock=clock, interpreter=fake)
    with TestClient(app) as first, TestClient(app) as second:
        start_workspace(first)
        start_workspace(second)

        # Change only the first workspace, then interpret in each.
        wheelchair = equipment_of(snapshot(first), "WHEELCHAIR")
        request_id = body(create_request(first))["request"]["id"]
        reserve(first, request_id=request_id, equipment_id=wheelchair["id"], expected_version=1)

        assert _interpret(first).status_code == 200
        assert _interpret(second).status_code == 200

    first_counts = {(row.kind, row.state): row.count for row in fake.seen_counts[0]}
    second_counts = {(row.kind, row.state): row.count for row in fake.seen_counts[1]}
    assert first_counts[("WHEELCHAIR", "RESERVED")] == 1
    assert ("WHEELCHAIR", "RESERVED") not in second_counts
    assert second_counts[("WHEELCHAIR", "AVAILABLE")] == 1

    # Counts only, never identities.
    for row in fake.seen_counts[0]:
        assert isinstance(row, InventoryCount)
        assert set(dataclasses.asdict(row)) == {"kind", "state", "count"}


# --- request schema -----------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": ""},
        {"text": "   "},
        {"text": "x" * 2001},
        {"text": 42},
        {"text": None},
        {"text": "valid", "extra": True},
        {"message": "wrong field name"},
    ],
)
def test_interpretation_schema_is_strict(
    assistant: tuple[TestClient, FakeInterpreter], payload: dict[str, Any]
) -> None:
    client, fake = assistant
    start_workspace(client)
    response = client.post(INTERPRET_URL, json=payload)
    assert response.status_code == 422, response.text
    assert error_code(response) == "VALIDATION_ERROR"
    assert fake.calls == 0


def test_text_is_measured_and_passed_after_trimming(
    assistant: tuple[TestClient, FakeInterpreter],
) -> None:
    client, fake = assistant
    start_workspace(client)
    assert _interpret(client, text="  " + "x" * 2000 + "  ").status_code == 200
    assert fake.seen_text[-1] == "x" * 2000


def test_no_idempotency_key_is_required(
    assistant: tuple[TestClient, FakeInterpreter],
) -> None:
    """This read-only route is explicitly exempt from the M1 idempotency rule."""
    client, _ = assistant
    start_workspace(client)
    first = _interpret(client)
    second = _interpret(client)
    assert first.status_code == second.status_code == 200
    assert "Idempotency-Key" not in first.request.headers


# --- response shape -----------------------------------------------------


def test_successful_interpretation_has_the_exact_contract_shape(
    assistant: tuple[TestClient, FakeInterpreter],
) -> None:
    client, _ = assistant
    start_workspace(client)
    payload = body(_interpret(client))

    assert set(payload) == {"draft", "missing_fields", "provenance"}
    assert set(payload["draft"]) == {
        "borrower_label",
        "equipment_kind",
        "pickup_location",
        "due_at",
    }
    assert payload["draft"] == {
        "borrower_label": "Meena R",
        "equipment_kind": "WHEELCHAIR",
        "pickup_location": "the Velachery room",
        "due_at": "2026-09-14T09:00:00Z",
    }
    assert payload["missing_fields"] == []
    assert payload["provenance"] == {
        "framework": "strands",
        "provider": "ollama",
        "model": "llama3.2:3b",
        "inventory_tool_calls": 1,
        "completed_at": "2026-09-07T09:00:00Z",
    }


def test_missing_fields_are_reported_in_order(settings: Settings, clock: FakeClock) -> None:
    fake = FakeInterpreter(
        result=_interpretation(
            borrower_label=None,
            pickup_location=None,
            due_at=datetime(2026, 9, 14, 9, 0, tzinfo=UTC),
            missing=("borrower_label", "pickup_location"),
        )
    )
    for client in _client(settings, clock, fake):
        start_workspace(client)
        payload = body(_interpret(client))
    assert payload["missing_fields"] == ["borrower_label", "pickup_location"]
    assert payload["draft"]["borrower_label"] is None
    assert payload["draft"]["equipment_kind"] == "WHEELCHAIR"


# --- read-only ----------------------------------------------------------


def test_interpretation_changes_no_business_state(
    assistant: tuple[TestClient, FakeInterpreter],
) -> None:
    client, _ = assistant
    start_workspace(client)
    before = snapshot(client)

    assert _interpret(client).status_code == 200
    assert _interpret(client, text="another wheelchair please").status_code == 200

    after = snapshot(client)
    assert after == before
    assert after["requests"] == []
    assert after["loans"] == []
    assert after["events"] == []


# --- failures -----------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (AssistantUnavailableError("down"), 503, "ASSISTANT_UNAVAILABLE"),
        (AssistantInvalidOutputError("garbage"), 502, "ASSISTANT_INVALID_OUTPUT"),
    ],
)
def test_adapter_failures_map_to_safe_errors(
    settings: Settings, clock: FakeClock, error: Exception, status: int, code: str
) -> None:
    for client in _client(settings, clock, FakeInterpreter(error=error)):
        start_workspace(client)
        response = _interpret(client)
        assert response.status_code == status
        assert error_code(response) == code
        assert set(body(response)) == {"error"}
        assert snapshot(client)["events"] == []


def test_an_unexpected_adapter_failure_is_a_generic_500(
    settings: Settings, clock: FakeClock
) -> None:
    fake = FakeInterpreter(error=RuntimeError("prompt leak: secret internals"))
    app = create_app(_enabled(settings), clock=clock, interpreter=fake)
    with TestClient(app, raise_server_exceptions=False) as client:
        start_workspace(client)
        response = _interpret(client)
    assert response.status_code == 500
    assert body(response) == {
        "error": {"code": "INTERNAL_ERROR", "message": "The request could not be completed."}
    }
    assert "prompt leak" not in response.text


def test_timeout_holds_the_slot_while_the_run_is_still_winding_down(
    settings: Settings, clock: FakeClock
) -> None:
    """504 for the caller, 429 for anyone else until the owned run really ends.

    Replaces the BS-001-R1 test of the same invariant, which relied on the run
    being left alone after a timeout. A timeout now cancels the run, so to keep
    proving that a *still-running* run holds its slot the fake here refuses
    cancellation for its first call — the honest way to hold a slot open.
    """
    fake = FakeInterpreter(delay=0.6, delay_calls=1, resist_cancel=True)
    for client in _client(settings, clock, fake, deadline=0.05, cancel_grace=0.05):
        start_workspace(client)

        timed_out = _interpret(client)
        assert timed_out.status_code == 504
        assert error_code(timed_out) == "ASSISTANT_TIMEOUT"

        busy = _interpret(client)
        assert busy.status_code == 429
        assert error_code(busy) == "ASSISTANT_BUSY"

        time.sleep(1.0)
        recovered = _interpret(client)
        assert recovered.status_code == 200

        # The abandoned run wrote nothing on its way out.
        assert snapshot(client)["events"] == []

    assert fake.calls == 2
    assert fake.cancelled, "the timed-out run was cancelled, not merely signalled"


def test_a_timeout_really_cancels_the_run_and_then_frees_the_slot(
    settings: Settings, clock: FakeClock
) -> None:
    """The behaviour BS-003-R4 adds: the run is stopped, not left to finish.

    Before this, a timeout only set a threading Event, which a provider call
    already awaiting a reply can never see; the run kept going and the slot
    stayed held for its full duration. Now the task is cancelled, so a run that
    accepts cancellation ends promptly and the next caller is served.
    """
    fake = FakeInterpreter(delay=30.0, delay_calls=1)
    for client in _client(settings, clock, fake, deadline=0.05, cancel_grace=5.0):
        start_workspace(client)

        timed_out = _interpret(client)
        assert timed_out.status_code == 504
        assert error_code(timed_out) == "ASSISTANT_TIMEOUT"
        assert fake.cancelled, "the run was cancelled, not left running"

        # No 30-second wait: the slot was freed as soon as the run really ended.
        served = _interpret(client)
        assert served.status_code == 200

        assert snapshot(client)["events"] == []

    assert fake.calls == 2


def test_error_responses_carry_no_draft_or_provenance(settings: Settings, clock: FakeClock) -> None:
    for client in _client(settings, clock, FakeInterpreter(error=AssistantUnavailableError("x"))):
        start_workspace(client)
        payload = body(_interpret(client))
    assert "draft" not in payload
    assert "provenance" not in payload
    assert set(payload["error"]) == {"code", "message"}
