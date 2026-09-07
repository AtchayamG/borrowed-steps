"""Session isolation, expiry, origin protection and strict request schemas."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from borrowed_steps.config import Settings
from borrowed_steps.infrastructure.sqlite_store import SqliteUnitOfWork
from borrowed_steps.interfaces.http.app import SESSION_COOKIE, create_app
from conftest import FakeClock
from support import (
    ALLOWED_ORIGIN,
    DUE_AT,
    FOREIGN_ORIGIN,
    PICKUP_LOCATION,
    body,
    create_request,
    equipment_of,
    error_code,
    inspect,
    key,
    reserve,
    snapshot,
    start_workspace,
    transition,
)


def test_snapshot_without_session_is_401(client: TestClient) -> None:
    response = client.get("/api/snapshot")
    assert response.status_code == 401
    assert error_code(response) == "SESSION_REQUIRED"


def test_unknown_cookie_is_401(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, "not-a-real-session")
    response = client.get("/api/snapshot")
    assert response.status_code == 401
    assert error_code(response) == "SESSION_REQUIRED"


def test_session_expires_after_24_hours(client: TestClient, clock: FakeClock) -> None:
    start_workspace(client)
    clock.advance(timedelta(hours=23, minutes=59))
    assert client.get("/api/snapshot").status_code == 200

    clock.advance(timedelta(minutes=2))
    response = client.get("/api/snapshot")
    assert response.status_code == 401
    assert error_code(response) == "SESSION_REQUIRED"


def test_mutation_without_session_is_401(client: TestClient) -> None:
    response = create_request(client)
    assert response.status_code == 401
    assert error_code(response) == "SESSION_REQUIRED"


def test_foreign_origin_is_403(client: TestClient) -> None:
    start_workspace(client)
    response = client.post(
        "/api/requests",
        json={
            "borrower_label": "Borrower A",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": PICKUP_LOCATION,
            "due_at": DUE_AT,
        },
        headers={"Idempotency-Key": key("origin"), "Origin": FOREIGN_ORIGIN},
    )
    assert response.status_code == 403
    assert error_code(response) == "ORIGIN_FORBIDDEN"
    assert snapshot(client)["requests"] == []


def test_configured_origin_is_accepted(client: TestClient) -> None:
    start_workspace(client)
    response = client.post(
        "/api/requests",
        json={
            "borrower_label": "Borrower A",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": PICKUP_LOCATION,
            "due_at": DUE_AT,
        },
        headers={"Idempotency-Key": key("origin"), "Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200


def test_foreign_origin_cannot_create_a_workspace(client: TestClient) -> None:
    response = client.post("/api/workspaces", json={}, headers={"Origin": FOREIGN_ORIGIN})
    assert response.status_code == 403
    assert error_code(response) == "ORIGIN_FORBIDDEN"


def test_workspaces_are_isolated(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    with TestClient(app) as first, TestClient(app) as second:
        start_workspace(first)
        start_workspace(second)

        first_wheelchair = equipment_of(snapshot(first), "WHEELCHAIR")
        second_wheelchair = equipment_of(snapshot(second), "WHEELCHAIR")
        assert first_wheelchair["id"] != second_wheelchair["id"]

        request_id = body(create_request(first))["request"]["id"]
        assert snapshot(second)["requests"] == []

        # A foreign workspace identifier does not resolve.
        response = reserve(
            second,
            request_id=request_id,
            equipment_id=second_wheelchair["id"],
            expected_version=1,
        )
        assert response.status_code == 404
        assert error_code(response) == "NOT_FOUND"

        response = reserve(
            first,
            request_id=request_id,
            equipment_id=second_wheelchair["id"],
            expected_version=1,
        )
        assert response.status_code == 404
        assert error_code(response) == "NOT_FOUND"


def test_unknown_loan_and_equipment_are_404(client: TestClient) -> None:
    start_workspace(client)
    response = transition(client, loan_id="missing", action="pickup", expected_version=1)
    assert response.status_code == 404
    response = inspect(client, equipment_id="missing", outcome="AVAILABLE", expected_version=1)
    assert response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {
            "borrower_label": "",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "x",
            "due_at": DUE_AT,
        },
        {
            "borrower_label": "a" * 61,
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "x",
            "due_at": DUE_AT,
        },
        {
            "borrower_label": "a",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "b" * 121,
            "due_at": DUE_AT,
        },
        {
            "borrower_label": "a",
            "equipment_kind": "SCOOTER",
            "pickup_location": "x",
            "due_at": DUE_AT,
        },
        {"borrower_label": "a", "equipment_kind": "WHEELCHAIR", "pickup_location": "x"},
        {
            "borrower_label": "a",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "x",
            "due_at": DUE_AT,
            "notes": "extra",
        },
        {
            "borrower_label": 12,
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "x",
            "due_at": DUE_AT,
        },
        {
            "borrower_label": "a",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "x",
            "due_at": "2026-09-14T09:00:00",
        },
        {
            "borrower_label": "a",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "x",
            "due_at": "not-a-date",
        },
    ],
)
def test_request_schema_is_strict(client: TestClient, payload: dict[str, Any]) -> None:
    start_workspace(client)
    response = client.post(
        "/api/requests", json=payload, headers={"Idempotency-Key": key("strict")}
    )
    assert response.status_code == 422, response.text
    assert error_code(response) == "VALIDATION_ERROR"


@pytest.mark.parametrize("due_at", ["2026-09-07T08:00:00Z", "2026-10-31T09:00:00Z"])
def test_due_at_must_be_future_and_within_thirty_days(client: TestClient, due_at: str) -> None:
    start_workspace(client)
    response = create_request(client, due_at=due_at)
    assert response.status_code == 422
    assert error_code(response) == "VALIDATION_ERROR"


def test_due_at_accepts_a_non_utc_offset_and_normalises_it(client: TestClient) -> None:
    start_workspace(client)
    created = body(create_request(client, due_at="2026-09-14T14:30:00+05:30"))["request"]
    assert created["due_at"] == "2026-09-14T09:00:00Z"


@pytest.mark.parametrize("header", [None, "short"])
def test_idempotency_key_header_is_required(client: TestClient, header: str | None) -> None:
    start_workspace(client)
    headers = {} if header is None else {"Idempotency-Key": header}
    response = client.post(
        "/api/requests",
        json={
            "borrower_label": "Borrower A",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": PICKUP_LOCATION,
            "due_at": DUE_AT,
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert error_code(response) == "VALIDATION_ERROR"


@pytest.mark.parametrize("approved", [None, False])
def test_reservation_requires_explicit_human_approval(
    client: TestClient, approved: bool | None
) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]

    response = reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=1,
        human_approved=approved,
    )
    assert response.status_code == 422
    assert error_code(response) == "APPROVAL_REQUIRED"
    assert snapshot(client)["loans"] == []


@pytest.mark.parametrize("approved", [None, False])
def test_quarantine_release_requires_human_approval(
    client: TestClient, approved: bool | None
) -> None:
    start_workspace(client)
    crutches = equipment_of(snapshot(client), "CRUTCHES")
    response = inspect(
        client,
        equipment_id=crutches["id"],
        outcome="AVAILABLE",
        expected_version=1,
        human_approved=approved,
    )
    assert response.status_code == 422
    assert error_code(response) == "APPROVAL_REQUIRED"
    assert equipment_of(snapshot(client), "CRUTCHES")["state"] == "QUARANTINED"


def test_quarantined_and_uninspected_items_cannot_be_reserved(client: TestClient) -> None:
    start_workspace(client)
    snap = snapshot(client)
    crutches = equipment_of(snap, "CRUTCHES")
    request_id = body(create_request(client, kind="CRUTCHES"))["request"]["id"]

    response = reserve(
        client,
        request_id=request_id,
        equipment_id=crutches["id"],
        expected_version=crutches["version"],
    )
    assert response.status_code == 409
    assert error_code(response) == "STATE_CONFLICT"


def test_wrong_kind_is_rejected(client: TestClient) -> None:
    start_workspace(client)
    walker = equipment_of(snapshot(client), "WALKER")
    request_id = body(create_request(client, kind="WHEELCHAIR"))["request"]["id"]

    response = reserve(
        client,
        request_id=request_id,
        equipment_id=walker["id"],
        expected_version=walker["version"],
    )
    assert response.status_code == 409
    assert error_code(response) == "STATE_CONFLICT"


def test_stale_version_is_rejected_and_changes_nothing(client: TestClient) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]

    response = reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=99,
    )
    assert response.status_code == 409
    assert error_code(response) == "STATE_CONFLICT"

    snap = snapshot(client)
    assert snap["loans"] == []
    assert equipment_of(snap, "WHEELCHAIR")["state"] == "AVAILABLE"
    assert equipment_of(snap, "WHEELCHAIR")["version"] == 1
    assert [event["action"] for event in snap["events"]] == ["REQUEST_CREATED"]


def test_invalid_transitions_are_409(client: TestClient) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]
    loan = body(
        reserve(
            client,
            request_id=request_id,
            equipment_id=wheelchair["id"],
            expected_version=1,
        )
    )["loan"]

    # Return before pickup.
    response = transition(client, loan_id=loan["id"], action="return", expected_version=2)
    assert response.status_code == 409
    assert error_code(response) == "STATE_CONFLICT"

    # Inspect an item that is still reserved.
    response = inspect(
        client, equipment_id=wheelchair["id"], outcome="AVAILABLE", expected_version=2
    )
    assert response.status_code == 409

    # Pick up twice.
    assert (
        transition(client, loan_id=loan["id"], action="pickup", expected_version=2).status_code
        == 200
    )
    response = transition(client, loan_id=loan["id"], action="pickup", expected_version=3)
    assert response.status_code == 409


def test_an_item_on_loan_cannot_be_reserved_again(client: TestClient) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    first = body(create_request(client))["request"]["id"]
    second = body(create_request(client, borrower_label="Borrower B"))["request"]["id"]

    loan = body(
        reserve(client, request_id=first, equipment_id=wheelchair["id"], expected_version=1)
    )["loan"]
    transition(client, loan_id=loan["id"], action="pickup", expected_version=2)

    response = reserve(client, request_id=second, equipment_id=wheelchair["id"], expected_version=3)
    assert response.status_code == 409
    assert error_code(response) == "STATE_CONFLICT"
    assert len(snapshot(client)["loans"]) == 1


@pytest.mark.parametrize("payload", ["null", "[]", '{"seed": true}', '"{}"', "123"])
def test_workspace_creation_requires_an_empty_json_object(client: TestClient, payload: str) -> None:
    response = client.post(
        "/api/workspaces", content=payload, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text
    assert error_code(response) == "VALIDATION_ERROR"
    assert client.cookies.get(SESSION_COOKIE) is None
    assert client.get("/api/snapshot").status_code == 401


def test_workspace_creation_requires_a_body(client: TestClient) -> None:
    response = client.post("/api/workspaces")
    assert response.status_code == 422
    assert error_code(response) == "VALIDATION_ERROR"
    assert client.cookies.get(SESSION_COOKIE) is None


def test_a_rejected_workspace_body_creates_nothing(client: TestClient, db_path: Path) -> None:
    response = client.post(
        "/api/workspaces", content="null", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422

    connection = sqlite3.connect(db_path)
    try:
        for table in ("workspaces", "sessions", "equipment"):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, table
    finally:
        connection.close()


def test_unexpected_storage_failure_returns_the_generic_envelope(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected failure mid-transaction must roll back and leak nothing."""
    start_workspace(client)

    def fail(*_args: object, **_kwargs: object) -> None:
        msg = "injected storage failure - must never reach the client"
        raise RuntimeError(msg)

    monkeypatch.setattr(SqliteUnitOfWork, "save_idempotency", fail)

    with TestClient(client.app, raise_server_exceptions=False) as faulty:
        faulty.cookies.update(client.cookies)
        response = create_request(faulty)

        assert response.status_code == 500
        assert response.headers["content-type"].startswith("application/json")
        assert body(response) == {
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "The request could not be completed.",
            }
        }
        for leak in ("injected", "RuntimeError", "Traceback", "save_idempotency"):
            assert leak not in response.text

        snap = snapshot(faulty)

    # The effect rolled back with the transaction: no request, no event.
    assert snap["requests"] == []
    assert snap["events"] == []

    # And nothing was cached, so the key stays usable once storage recovers.
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    finally:
        connection.close()


def test_the_same_key_still_works_after_an_unexpected_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_workspace(client)
    reuse = key("recovered")

    def fail(*_args: object, **_kwargs: object) -> None:
        msg = "injected storage failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(SqliteUnitOfWork, "save_idempotency", fail)
    with TestClient(client.app, raise_server_exceptions=False) as faulty:
        faulty.cookies.update(client.cookies)
        assert create_request(faulty, idempotency_key=reuse).status_code == 500

    monkeypatch.undo()
    recovered = create_request(client, idempotency_key=reuse)
    assert recovered.status_code == 200
    assert len(snapshot(client)["requests"]) == 1
