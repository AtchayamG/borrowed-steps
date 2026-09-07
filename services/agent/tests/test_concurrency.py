"""Contention for one item: exactly one winner, no partial state."""

from __future__ import annotations

import threading

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
    key,
    reserve,
    snapshot,
    start_workspace,
)


def test_second_request_loses_the_only_ready_wheelchair(client: TestClient) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    first = body(create_request(client, borrower_label="Borrower A"))["request"]["id"]
    second = body(create_request(client, borrower_label="Borrower B"))["request"]["id"]

    winner = reserve(client, request_id=first, equipment_id=wheelchair["id"], expected_version=1)
    assert winner.status_code == 200

    # The coordinator's second tab still holds version 1.
    loser = reserve(client, request_id=second, equipment_id=wheelchair["id"], expected_version=1)
    assert loser.status_code == 409
    assert error_code(loser) == "STATE_CONFLICT"

    snap = snapshot(client)
    assert len(snap["loans"]) == 1
    statuses = {item["id"]: item["status"] for item in snap["requests"]}
    assert statuses[first] == "RESERVED"
    assert statuses[second] == "REQUESTED"


def test_simultaneous_reservations_through_independent_connections(
    settings: Settings, clock: FakeClock
) -> None:
    """Two application instances on one database file race for the same item."""
    setup_app = create_app(settings, clock=clock)
    with TestClient(setup_app) as setup:
        start_workspace(setup)
        session_cookie = setup.cookies.get(SESSION_COOKIE)
        wheelchair = equipment_of(snapshot(setup), "WHEELCHAIR")
        request_ids = [
            body(create_request(setup, borrower_label=f"Borrower {name}"))["request"]["id"]
            for name in ("A", "B")
        ]

    assert session_cookie is not None

    barrier = threading.Barrier(2)
    results: dict[int, httpx.Response] = {}
    lock = threading.Lock()

    def contend(index: int, request_id: str) -> None:
        # A separate application object, so this thread uses its own DB connections.
        with TestClient(create_app(settings, clock=clock)) as racer:
            racer.cookies.set(SESSION_COOKIE, session_cookie)
            barrier.wait(timeout=10)
            response = reserve(
                racer,
                request_id=request_id,
                equipment_id=wheelchair["id"],
                expected_version=1,
                idempotency_key=key(f"race-{index}"),
            )
        with lock:
            results[index] = response

    threads = [
        threading.Thread(target=contend, args=(index, request_id))
        for index, request_id in enumerate(request_ids)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    codes = sorted(response.status_code for response in results.values())
    assert codes == [200, 409], {i: r.text for i, r in results.items()}

    loser = next(r for r in results.values() if r.status_code == 409)
    assert error_code(loser) == "STATE_CONFLICT"

    with TestClient(create_app(settings, clock=clock)) as checker:
        checker.cookies.set(SESSION_COOKIE, session_cookie)
        snap = snapshot(checker)

    assert len(snap["loans"]) == 1
    assert equipment_of(snap, "WHEELCHAIR")["state"] == "RESERVED"
    assert equipment_of(snap, "WHEELCHAIR")["version"] == 2
    reserved_requests = [item for item in snap["requests"] if item["status"] == "RESERVED"]
    assert len(reserved_requests) == 1
    assert [event["action"] for event in snap["events"]].count("RESERVED") == 1
