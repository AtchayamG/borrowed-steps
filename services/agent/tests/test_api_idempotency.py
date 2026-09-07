"""Idempotent retry, changed-payload conflict and rollback of failed mutations."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from borrowed_steps.config import Settings
from borrowed_steps.interfaces.http.app import SESSION_COOKIE, create_app
from conftest import FakeClock
from support import (
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


def test_identical_retry_replays_the_stored_response(client: TestClient) -> None:
    start_workspace(client)
    reuse = key("replay")

    first = create_request(client, idempotency_key=reuse)
    second = create_request(client, idempotency_key=reuse)

    assert first.status_code == second.status_code == 200
    assert first.content == second.content

    snap = snapshot(client)
    assert len(snap["requests"]) == 1
    assert [event["action"] for event in snap["events"]] == ["REQUEST_CREATED"]


def test_retrying_a_reservation_does_not_create_a_second_loan(client: TestClient) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]
    reuse = key("reserve-replay")

    first = reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=1,
        idempotency_key=reuse,
    )
    second = reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=1,
        idempotency_key=reuse,
    )

    assert first.status_code == second.status_code == 200
    assert first.content == second.content

    snap = snapshot(client)
    assert len(snap["loans"]) == 1
    assert equipment_of(snap, "WHEELCHAIR")["version"] == 2
    assert [event["action"] for event in snap["events"]] == ["RESERVED", "REQUEST_CREATED"]


def test_changed_payload_under_the_same_key_is_409(client: TestClient) -> None:
    start_workspace(client)
    reuse = key("conflict")

    assert create_request(client, idempotency_key=reuse).status_code == 200
    response = create_request(client, borrower_label="Borrower B", idempotency_key=reuse)

    assert response.status_code == 409
    assert error_code(response) == "IDEMPOTENCY_CONFLICT"
    assert len(snapshot(client)["requests"]) == 1


def test_changed_route_under_the_same_key_is_409(client: TestClient) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]
    reuse = key("route")

    loan = body(
        reserve(
            client,
            request_id=request_id,
            equipment_id=wheelchair["id"],
            expected_version=1,
            idempotency_key=reuse,
        )
    )["loan"]

    response = transition(
        client,
        loan_id=loan["id"],
        action="pickup",
        expected_version=2,
        idempotency_key=reuse,
    )
    assert response.status_code == 409
    assert error_code(response) == "IDEMPOTENCY_CONFLICT"
    assert snapshot(client)["loans"][0]["status"] == "RESERVED"


def test_a_failed_mutation_is_not_cached_and_leaves_no_state(client: TestClient) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]
    reuse = key("rollback")

    failed = reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=42,
        idempotency_key=reuse,
    )
    assert failed.status_code == 409

    snap = snapshot(client)
    assert snap["loans"] == []
    assert equipment_of(snap, "WHEELCHAIR") == wheelchair
    assert [event["action"] for event in snap["events"]] == ["REQUEST_CREATED"]

    # The same key is still usable for the corrected request.
    retried = reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=1,
        idempotency_key=reuse,
    )
    assert retried.status_code == 200


def test_idempotency_is_scoped_to_one_workspace(client: TestClient) -> None:
    shared = key("scoped")
    start_workspace(client)
    first = create_request(client, idempotency_key=shared)
    assert first.status_code == 200

    start_workspace(client)
    second = create_request(client, idempotency_key=shared)
    assert second.status_code == 200
    assert second.json() != first.json()
    assert len(snapshot(client)["requests"]) == 1


def test_inspection_retry_does_not_double_close(client: TestClient) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]
    loan = body(
        reserve(client, request_id=request_id, equipment_id=wheelchair["id"], expected_version=1)
    )["loan"]
    transition(client, loan_id=loan["id"], action="pickup", expected_version=2)
    transition(client, loan_id=loan["id"], action="return", expected_version=3)

    reuse = key("inspect-replay")
    first = inspect(
        client,
        equipment_id=wheelchair["id"],
        outcome="AVAILABLE",
        expected_version=4,
        idempotency_key=reuse,
    )
    second = inspect(
        client,
        equipment_id=wheelchair["id"],
        outcome="AVAILABLE",
        expected_version=4,
        idempotency_key=reuse,
    )
    assert first.status_code == second.status_code == 200
    assert first.content == second.content

    snap = snapshot(client)
    assert equipment_of(snap, "WHEELCHAIR")["version"] == 5
    assert [event["action"] for event in snap["events"]].count("INSPECTED_AVAILABLE") == 1


def _assert_identical_replay(first: httpx.Response, second: httpx.Response) -> None:
    """The replay must be the same bytes, not merely the same decoded value."""
    assert first.status_code == 200, first.text
    assert second.status_code == first.status_code
    assert first.content == second.content
    assert first.headers["content-type"] == "application/json"
    assert second.headers["content-type"] == first.headers["content-type"]


def test_replay_serves_identical_bytes_for_every_mutation_family(client: TestClient) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")

    request_key = key("bytes-request")
    created = create_request(client, idempotency_key=request_key)
    _assert_identical_replay(created, create_request(client, idempotency_key=request_key))
    request_id = body(created)["request"]["id"]

    reserve_key = key("bytes-reserve")
    reservation = reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=1,
        idempotency_key=reserve_key,
    )
    _assert_identical_replay(
        reservation,
        reserve(
            client,
            request_id=request_id,
            equipment_id=wheelchair["id"],
            expected_version=1,
            idempotency_key=reserve_key,
        ),
    )
    loan_id = body(reservation)["loan"]["id"]

    for action, version in (("pickup", 2), ("return", 3)):
        action_key = key(f"bytes-{action}")
        moved = transition(
            client,
            loan_id=loan_id,
            action=action,
            expected_version=version,
            idempotency_key=action_key,
        )
        _assert_identical_replay(
            moved,
            transition(
                client,
                loan_id=loan_id,
                action=action,
                expected_version=version,
                idempotency_key=action_key,
            ),
        )

    inspect_key = key("bytes-inspect")
    inspected = inspect(
        client,
        equipment_id=wheelchair["id"],
        outcome="AVAILABLE",
        expected_version=4,
        idempotency_key=inspect_key,
    )
    _assert_identical_replay(
        inspected,
        inspect(
            client,
            equipment_id=wheelchair["id"],
            outcome="AVAILABLE",
            expected_version=4,
            idempotency_key=inspect_key,
        ),
    )

    snap = snapshot(client)
    assert [event["action"] for event in snap["events"]] == [
        "INSPECTED_AVAILABLE",
        "RETURNED",
        "PICKED_UP",
        "RESERVED",
        "REQUEST_CREATED",
    ]
    assert len(snap["loans"]) == 1


def test_replay_bytes_match_for_a_non_ascii_label(client: TestClient) -> None:
    start_workspace(client)
    label = "பயனாளர் ஆதரவு"
    reuse = key("bytes-unicode")

    first = create_request(client, borrower_label=label, idempotency_key=reuse)
    second = create_request(client, borrower_label=label, idempotency_key=reuse)

    _assert_identical_replay(first, second)
    assert body(first)["request"]["borrower_label"] == label
    assert label.encode("utf-8") in first.content
    assert len(snapshot(client)["requests"]) == 1


def test_replay_after_reopening_the_database_serves_identical_bytes(
    settings: Settings, clock: FakeClock
) -> None:
    reuse = key("bytes-reopen")

    with TestClient(create_app(settings, clock=clock)) as first_client:
        start_workspace(first_client)
        session_cookie = first_client.cookies.get(SESSION_COOKIE)
        first = create_request(first_client, idempotency_key=reuse)

    assert session_cookie is not None

    # A brand-new application object on the same file, as after a process restart.
    with TestClient(create_app(settings, clock=clock)) as second_client:
        second_client.cookies.set(SESSION_COOKIE, session_cookie)
        second = create_request(second_client, idempotency_key=reuse)
        assert len(snapshot(second_client)["requests"]) == 1

    _assert_identical_replay(first, second)
