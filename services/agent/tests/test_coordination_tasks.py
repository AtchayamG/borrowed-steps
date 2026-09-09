"""Tasks created and resolved inside the human lifecycle, over real HTTP.

Every test drives the real routes against a real file-backed SQLite database.
The coordination runner is off here (the ``settings`` fixture) so these assert
what the human actions do, not what a background thread did meanwhile; the
runner has its own tests.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from support import (
    DUE_AT,
    body,
    create_request,
    equipment_of,
    inspect,
    key,
    reserve,
    snapshot,
    start_workspace,
    transition,
)

TASK_FIELDS = {"id", "loan_id", "kind", "status", "due_at", "created_at"}


def _tasks(client: TestClient) -> list[dict[str, Any]]:
    return list(snapshot(client)["tasks"])


def _status_by_kind(client: TestClient) -> dict[str, str]:
    """Tasks keyed by kind.

    Used instead of positional assertions wherever two tasks can share a
    ``created_at``: the contract orders by ``created_at`` then ``id``, so with a
    tie the opaque id decides, and asserting insertion order would be asserting
    something the contract does not promise. Ordering itself has its own test.
    """
    return {task["kind"]: task["status"] for task in _tasks(client)}


def _reserve_one(client: TestClient) -> dict[str, Any]:
    """Take a workspace from empty to one RESERVED loan."""
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]
    reserved = body(
        reserve(
            client,
            request_id=request_id,
            equipment_id=wheelchair["id"],
            expected_version=wheelchair["version"],
        )
    )
    return dict(reserved)


def test_reservation_creates_one_pending_pickup_task_due_now(client: TestClient) -> None:
    """Arrange pickup now - not a promised time, not an overdue penalty."""
    reserved = _reserve_one(client)
    loan = reserved["loan"]

    tasks = _tasks(client)
    assert len(tasks) == 1
    task = tasks[0]
    assert set(task) == TASK_FIELDS, "the public shape is exactly six fields"
    assert "workspace_id" not in task
    assert task["loan_id"] == loan["id"]
    assert task["kind"] == "PICKUP_DUE"
    assert task["status"] == "PENDING"
    # due_at is the reservation instant itself, and created_at is the same
    # transaction clock, so nothing about a future pickup time is invented.
    assert task["due_at"] == task["created_at"] == loan["created_at"]


def test_reservation_response_body_is_unchanged(client: TestClient) -> None:
    """Mutation responses keep their M1 shape; tasks travel in the snapshot."""
    reserved = _reserve_one(client)
    assert set(reserved) == {"request", "equipment", "loan"}
    assert "tasks" not in reserved
    assert "task" not in reserved


def test_pickup_resolves_the_pickup_task_and_opens_the_return_task(
    client: TestClient,
) -> None:
    reserved = _reserve_one(client)
    loan, equipment = reserved["loan"], reserved["equipment"]

    transition(
        client,
        loan_id=loan["id"],
        action="pickup",
        expected_version=equipment["version"],
    )

    assert _status_by_kind(client) == {"PICKUP_DUE": "RESOLVED", "RETURN_DUE": "PENDING"}
    (return_task,) = [t for t in _tasks(client) if t["kind"] == "RETURN_DUE"]
    # The return notice carries the loan's real due instant, unchanged.
    assert return_task["due_at"] == DUE_AT == loan["due_at"]
    assert return_task["created_at"] != return_task["due_at"]


def test_return_resolves_the_return_task(client: TestClient) -> None:
    reserved = _reserve_one(client)
    loan = reserved["loan"]

    picked = body(
        transition(
            client,
            loan_id=loan["id"],
            action="pickup",
            expected_version=reserved["equipment"]["version"],
        )
    )
    transition(
        client,
        loan_id=loan["id"],
        action="return",
        expected_version=picked["equipment"]["version"],
    )

    assert _status_by_kind(client) == {"PICKUP_DUE": "RESOLVED", "RETURN_DUE": "RESOLVED"}


def test_the_whole_lifecycle_ends_with_every_task_resolved(client: TestClient) -> None:
    """Inspection closes the loan; nothing coordination-shaped is left open."""
    reserved = _reserve_one(client)
    loan = reserved["loan"]
    picked = body(
        transition(
            client,
            loan_id=loan["id"],
            action="pickup",
            expected_version=reserved["equipment"]["version"],
        )
    )
    returned = body(
        transition(
            client,
            loan_id=loan["id"],
            action="return",
            expected_version=picked["equipment"]["version"],
        )
    )
    inspect(
        client,
        equipment_id=returned["equipment"]["id"],
        outcome="AVAILABLE",
        expected_version=returned["equipment"]["version"],
    )

    snap = snapshot(client)
    assert all(task["status"] == "RESOLVED" for task in snap["tasks"])
    assert snap["loans"][0]["status"] == "CLOSED"
    # The M1 event story is untouched: five events, same order, no due events
    # because nothing was ever processed as due in this test.
    assert [event["action"] for event in snap["events"]] == [
        "INSPECTED_AVAILABLE",
        "RETURNED",
        "PICKED_UP",
        "RESERVED",
        "REQUEST_CREATED",
    ]


def test_tasks_are_ordered_by_created_at_then_id(client: TestClient) -> None:
    """The contract's order, including how a tie on created_at is broken.

    The test clock does not advance, so the pickup and return notices here
    share a ``created_at`` and the opaque id decides between them. That is the
    ordering the contract specifies; it is asserted directly rather than
    assumed to match the order the rows were written in.
    """
    reserved = _reserve_one(client)
    transition(
        client,
        loan_id=reserved["loan"]["id"],
        action="pickup",
        expected_version=reserved["equipment"]["version"],
    )
    tasks = _tasks(client)
    assert len(tasks) == 2
    assert tasks[0]["created_at"] == tasks[1]["created_at"], "this test needs the tie"
    assert tasks == sorted(tasks, key=lambda t: (t["created_at"], t["id"]))
    assert tasks[0]["id"] < tasks[1]["id"], "the id breaks the tie, ascending"


def test_an_exact_replay_creates_no_second_task(client: TestClient) -> None:
    """The stored response is replayed; the effect does not happen twice."""
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]
    reuse = key("reserve")

    first = reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=wheelchair["version"],
        idempotency_key=reuse,
    )
    second = reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=wheelchair["version"],
        idempotency_key=reuse,
    )

    assert first.status_code == second.status_code == 200
    assert first.content == second.content, "byte-identical replay"
    assert len(_tasks(client)) == 1


def test_a_replayed_pickup_creates_no_second_return_task(client: TestClient) -> None:
    reserved = _reserve_one(client)
    reuse = key("pickup")
    for _ in range(2):
        transition(
            client,
            loan_id=reserved["loan"]["id"],
            action="pickup",
            expected_version=reserved["equipment"]["version"],
            idempotency_key=reuse,
        )

    assert sorted(task["kind"] for task in _tasks(client)) == ["PICKUP_DUE", "RETURN_DUE"]


def test_a_refused_reservation_leaves_no_task_behind(client: TestClient) -> None:
    """Rollback takes the task with it: no approval, no state, no notice."""
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]

    refused = reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=wheelchair["version"],
        human_approved=None,
    )
    assert refused.status_code == 422

    snap = snapshot(client)
    assert snap["tasks"] == []
    assert snap["loans"] == []


def test_a_stale_version_conflict_leaves_no_task_behind(client: TestClient) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]

    conflicted = reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=wheelchair["version"] + 5,
    )
    assert conflicted.status_code == 409
    assert snapshot(client)["tasks"] == []


def test_tasks_never_leak_between_workspaces(client: TestClient) -> None:
    """A second workspace sees its own coordination and nothing else."""
    _reserve_one(client)
    first = _tasks(client)
    assert len(first) == 1

    client.cookies.clear()
    start_workspace(client)
    assert _tasks(client) == []

    second = _reserve_one(client)
    assert [task["loan_id"] for task in _tasks(client)] == [second["loan"]["id"]]


def test_both_endpoints_carry_the_same_task_shape(client: TestClient) -> None:
    """The contract puts tasks on the workspace response and on the snapshot."""
    created = start_workspace(client)
    assert created["snapshot"]["tasks"] == []
    assert "tasks" in created["snapshot"]

    _reserve_one_in_place(client)

    from_snapshot = snapshot(client)["tasks"]
    assert len(from_snapshot) == 1
    assert set(from_snapshot[0]) == TASK_FIELDS


def _reserve_one_in_place(client: TestClient) -> None:
    """Reserve inside the workspace this client already holds."""
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]
    reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=wheelchair["version"],
    )
