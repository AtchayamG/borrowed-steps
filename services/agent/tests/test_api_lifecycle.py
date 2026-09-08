"""Health, workspace seeding and the full request to inspection lifecycle."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from borrowed_steps.interfaces.http.app import SESSION_COOKIE
from support import (
    DUE_AT,
    body,
    create_request,
    equipment_of,
    inspect,
    reserve,
    snapshot,
    start_workspace,
    transition,
)


def test_health_declares_milestone_and_agent_mode(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert body(response) == {
        "status": "ok",
        "milestone": "M2A",
        "agent_mode": "disabled",
    }


def test_workspace_creation_seeds_the_equipment_room(client: TestClient) -> None:
    payload = start_workspace(client)
    assert set(payload) == {"workspace", "snapshot"}
    assert set(payload["workspace"]) == {"id"}

    snap = payload["snapshot"]
    assert set(snap) == {"equipment", "requests", "loans", "events", "agent_mode"}
    assert snap["agent_mode"] == "disabled"
    assert snap["requests"] == []
    assert snap["loans"] == []
    assert snap["events"] == []

    seeded = [(item["kind"], item["state"]) for item in snap["equipment"]]
    assert seeded == [
        ("WHEELCHAIR", "AVAILABLE"),
        ("WALKER", "AVAILABLE"),
        ("CRUTCHES", "QUARANTINED"),
    ]
    for item in snap["equipment"]:
        assert set(item) == {"id", "label", "kind", "state", "version"}
        assert item["version"] == 1

    cookie = client.cookies.get(SESSION_COOKIE)
    assert cookie is not None
    assert cookie != payload["workspace"]["id"]
    assert len(cookie) >= 32


def test_separate_workspace_per_creation(client: TestClient) -> None:
    first = start_workspace(client)
    second = start_workspace(client)
    assert first["workspace"]["id"] != second["workspace"]["id"]


def _lifecycle(client: TestClient) -> dict[str, Any]:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")

    created = body(create_request(client))["request"]
    assert created["status"] == "REQUESTED"
    assert created["due_at"] == DUE_AT
    assert set(created) == {
        "id",
        "borrower_label",
        "equipment_kind",
        "pickup_location",
        "due_at",
        "status",
        "created_at",
    }

    reserved = body(
        reserve(
            client,
            request_id=created["id"],
            equipment_id=wheelchair["id"],
            expected_version=wheelchair["version"],
        )
    )
    assert set(reserved) == {"request", "equipment", "loan"}
    assert reserved["request"]["status"] == "RESERVED"
    assert reserved["equipment"]["state"] == "RESERVED"
    assert reserved["equipment"]["version"] == 2
    assert reserved["loan"]["status"] == "RESERVED"
    assert reserved["loan"]["due_at"] == DUE_AT
    assert set(reserved["loan"]) == {
        "id",
        "request_id",
        "equipment_id",
        "status",
        "due_at",
        "created_at",
    }
    return {"request": created, "reserved": reserved, "equipment": wheelchair}


def test_full_lifecycle_request_to_inspection(client: TestClient) -> None:
    state = _lifecycle(client)
    loan_id = state["reserved"]["loan"]["id"]
    equipment_id = state["equipment"]["id"]

    picked = body(transition(client, loan_id=loan_id, action="pickup", expected_version=2))
    assert picked["request"]["status"] == "ON_LOAN"
    assert picked["equipment"]["state"] == "ON_LOAN"
    assert picked["equipment"]["version"] == 3
    assert picked["loan"]["status"] == "ON_LOAN"

    returned = body(transition(client, loan_id=loan_id, action="return", expected_version=3))
    assert returned["request"]["status"] == "RETURNED"
    assert returned["equipment"]["state"] == "AWAITING_INSPECTION"
    assert returned["equipment"]["version"] == 4
    assert returned["loan"]["status"] == "RETURNED"

    inspected = body(
        inspect(client, equipment_id=equipment_id, outcome="AVAILABLE", expected_version=4)
    )
    assert set(inspected) == {"equipment", "request", "loan"}
    assert inspected["equipment"]["state"] == "AVAILABLE"
    assert inspected["equipment"]["version"] == 5
    assert inspected["request"]["status"] == "CLOSED"
    assert inspected["loan"]["status"] == "CLOSED"

    snap = snapshot(client)
    actions = [event["action"] for event in snap["events"]]
    assert actions == [
        "INSPECTED_AVAILABLE",
        "RETURNED",
        "PICKED_UP",
        "RESERVED",
        "REQUEST_CREATED",
    ]
    for event in snap["events"]:
        assert set(event) == {"id", "entity_type", "entity_id", "action", "at"}
        assert event["entity_type"] in {"EQUIPMENT", "REQUEST", "LOAN"}


def test_later_reinspection_returns_null_loan_and_request(client: TestClient) -> None:
    state = _lifecycle(client)
    loan_id = state["reserved"]["loan"]["id"]
    equipment_id = state["equipment"]["id"]

    transition(client, loan_id=loan_id, action="pickup", expected_version=2)
    transition(client, loan_id=loan_id, action="return", expected_version=3)
    inspect(client, equipment_id=equipment_id, outcome="REPAIR", expected_version=4)

    again = body(
        inspect(client, equipment_id=equipment_id, outcome="AVAILABLE", expected_version=5)
    )
    assert again["equipment"]["state"] == "AVAILABLE"
    assert again["request"] is None
    assert again["loan"] is None


def test_inspection_closes_the_loan_for_every_outcome(client: TestClient) -> None:
    state = _lifecycle(client)
    loan_id = state["reserved"]["loan"]["id"]
    equipment_id = state["equipment"]["id"]
    transition(client, loan_id=loan_id, action="pickup", expected_version=2)
    transition(client, loan_id=loan_id, action="return", expected_version=3)

    inspected = body(
        inspect(client, equipment_id=equipment_id, outcome="QUARANTINED", expected_version=4)
    )
    assert inspected["equipment"]["state"] == "QUARANTINED"
    assert inspected["request"]["status"] == "CLOSED"
    assert inspected["loan"]["status"] == "CLOSED"


def test_reservation_leaves_other_requests_untouched(client: TestClient) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    first = body(create_request(client, borrower_label="Borrower A"))["request"]
    second = body(create_request(client, borrower_label="Borrower B"))["request"]

    reserve(
        client,
        request_id=first["id"],
        equipment_id=wheelchair["id"],
        expected_version=1,
    )

    snap = snapshot(client)
    statuses = {item["id"]: item["status"] for item in snap["requests"]}
    assert statuses[first["id"]] == "RESERVED"
    assert statuses[second["id"]] == "REQUESTED"
    assert len(snap["loans"]) == 1
