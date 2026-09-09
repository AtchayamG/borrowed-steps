"""Due processing against a real database: boundaries, races and restarts.

``process_due_tasks`` is called directly with a controlled clock, so every
boundary is exact and no test sleeps waiting for a real deadline. The store,
the transactions and the SQLite locking are all real.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from borrowed_steps.application.coordination import TickReport, process_due_tasks
from borrowed_steps.application.errors import StorageBusyError
from borrowed_steps.config import TASK_TICK_CANDIDATE_LIMIT, Settings
from borrowed_steps.domain.models import TaskStatus
from borrowed_steps.infrastructure.sqlite_store import SqliteStore
from borrowed_steps.infrastructure.system import SecretsIdGenerator
from conftest import FakeClock
from support import (
    body,
    create_request,
    equipment_of,
    key,
    reserve,
    snapshot,
    start_workspace,
    transition,
)

LIMIT = TASK_TICK_CANDIDATE_LIMIT


def _reserve(client: TestClient) -> dict[str, Any]:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]
    return dict(
        body(
            reserve(
                client,
                request_id=request_id,
                equipment_id=wheelchair["id"],
                expected_version=wheelchair["version"],
            )
        )
    )


def _tick(db_path: Path, clock: FakeClock, *, limit: int = LIMIT) -> TickReport:
    """One tick over its own store handle, as a separate process would."""
    return process_due_tasks(SqliteStore(db_path), clock, SecretsIdGenerator(), limit=limit)


def _events(client: TestClient) -> list[str]:
    return [event["action"] for event in snapshot(client)["events"]]


def _task(client: TestClient, kind: str) -> dict[str, Any]:
    for task in snapshot(client)["tasks"]:
        if task["kind"] == kind:
            return dict(task)
    msg = f"no {kind} task in this workspace"
    raise AssertionError(msg)


# Some deadlines here are days away, and the session cookie only lives 24 hours,
# so those assertions read the database directly rather than through a session
# the test itself has outlived. Reading the same rows the HTTP layer serves.


def _db_task_status(db_path: Path, kind: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT status FROM tasks WHERE kind = ?", (kind,)).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no {kind} task in the database"
    return str(row[0])


def _db_actions(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT action FROM events ORDER BY rowid DESC").fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


# --- the due boundary ---------------------------------------------------


def test_a_task_due_exactly_now_is_due(client: TestClient, clock: FakeClock, db_path: Path) -> None:
    """Equality counts. The pickup task is due at the reservation instant."""
    _reserve(client)
    assert _task(client, "PICKUP_DUE")["due_at"] == clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    report = _tick(db_path, clock)

    assert report.marked_due == 1
    assert _task(client, "PICKUP_DUE")["status"] == "DUE"
    assert _events(client)[0] == "PICKUP_DUE"


def test_a_task_due_one_second_later_is_not_yet_due(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    _reserve(client)
    earlier = FakeClock(clock.now() - timedelta(seconds=1))

    report = _tick(db_path, earlier)

    assert report.considered == 0
    assert _task(client, "PICKUP_DUE")["status"] == "PENDING"
    assert "PICKUP_DUE" not in _events(client)


def test_a_return_task_becomes_due_only_at_the_loans_own_deadline(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    reserved = _reserve(client)
    transition(
        client,
        loan_id=reserved["loan"]["id"],
        action="pickup",
        expected_version=reserved["equipment"]["version"],
    )
    _tick(db_path, clock)  # the pickup notice, already due
    assert _task(client, "RETURN_DUE")["status"] == "PENDING"

    # One second before the loan's own deadline: still not due.
    clock.advance(timedelta(days=7) - timedelta(seconds=1))
    assert _tick(db_path, clock).considered == 0
    assert _db_task_status(db_path, "RETURN_DUE") == "PENDING"

    clock.advance(timedelta(seconds=1))
    report = _tick(db_path, clock)

    assert report.marked_due == 1
    assert _db_task_status(db_path, "RETURN_DUE") == "DUE"
    assert _db_actions(db_path)[0] == "RETURN_DUE"


# --- repeats, restarts and staleness ------------------------------------


def test_a_second_tick_does_not_re_notify(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    _reserve(client)
    first = _tick(db_path, clock)
    second = _tick(db_path, clock)

    assert (first.marked_due, second.marked_due) == (1, 0)
    assert _events(client).count("PICKUP_DUE") == 1


def test_a_restart_catches_up_an_overdue_task(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    """Nothing is held in memory, so a process that was down misses nothing.

    Two loans in one workspace: a wheelchair still waiting to be collected, and
    a walker already out. That leaves one pending notice of each kind, which is
    what a service coming back up after a gap has to catch up on.
    """
    start_workspace(client)
    snap = snapshot(client)
    wheelchair = equipment_of(snap, "WHEELCHAIR")
    walker = equipment_of(snap, "WALKER")

    waiting_request = body(create_request(client, kind="WHEELCHAIR"))["request"]["id"]
    reserve(
        client,
        request_id=waiting_request,
        equipment_id=wheelchair["id"],
        expected_version=wheelchair["version"],
    )

    out_request = body(create_request(client, kind="WALKER"))["request"]["id"]
    out_loan = body(
        reserve(
            client,
            request_id=out_request,
            equipment_id=walker["id"],
            expected_version=walker["version"],
        )
    )
    transition(
        client,
        loan_id=out_loan["loan"]["id"],
        action="pickup",
        expected_version=out_loan["equipment"]["version"],
    )

    # No tick at all while both deadlines pass - as if the service were off.
    clock.advance(timedelta(days=30))

    report = _tick(db_path, clock)

    assert report.marked_due == 2, "both overdue notices are caught up"
    assert set(_db_actions(db_path)[:2]) == {"RETURN_DUE", "PICKUP_DUE"}
    assert _db_task_status(db_path, "RETURN_DUE") == "DUE"
    # The wheelchair's pickup notice is the one still open; the walker's was
    # resolved by its pickup before the gap.
    conn = sqlite3.connect(db_path)
    try:
        statuses = conn.execute(
            "SELECT status FROM tasks WHERE kind = 'PICKUP_DUE' ORDER BY status"
        ).fetchall()
    finally:
        conn.close()
    assert [row[0] for row in statuses] == ["DUE", "RESOLVED"]


def test_a_pending_task_for_an_already_collected_loan_resolves_without_an_event(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    """The human action won the race. Announcing it now would be untrue."""
    reserved = _reserve(client)
    # Pickup happens before any tick, so the pickup notice is resolved already;
    # force it back to PENDING to stand in for a tick that discovered it just
    # before the human acted.
    transition(
        client,
        loan_id=reserved["loan"]["id"],
        action="pickup",
        expected_version=reserved["equipment"]["version"],
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status = 'PENDING' WHERE kind = 'PICKUP_DUE'",
        )
        conn.commit()
    finally:
        conn.close()

    report = _tick(db_path, clock)

    assert report.resolved_stale == 1
    assert report.marked_due == 0
    assert _task(client, "PICKUP_DUE")["status"] == "RESOLVED"
    assert "PICKUP_DUE" not in _events(client)


def test_pickup_resolves_a_task_that_is_already_due(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    """A notice already processed still closes when the human acts."""
    reserved = _reserve(client)
    _tick(db_path, clock)
    assert _task(client, "PICKUP_DUE")["status"] == "DUE"

    transition(
        client,
        loan_id=reserved["loan"]["id"],
        action="pickup",
        expected_version=reserved["equipment"]["version"],
    )

    assert _task(client, "PICKUP_DUE")["status"] == "RESOLVED"
    assert _task(client, "RETURN_DUE")["status"] == "PENDING"


def test_return_resolves_a_return_task_that_is_already_due(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    """End to end over HTTP: due first, then the human records the return.

    The loan is given a deadline twelve hours out so the whole thing happens
    inside one 24-hour session, rather than stepping over the session lifetime
    to reach the deadline.
    """
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    soon = (clock.now() + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    request_id = body(create_request(client, due_at=soon))["request"]["id"]
    reserved = body(
        reserve(
            client,
            request_id=request_id,
            equipment_id=wheelchair["id"],
            expected_version=wheelchair["version"],
        )
    )
    picked = body(
        transition(
            client,
            loan_id=reserved["loan"]["id"],
            action="pickup",
            expected_version=reserved["equipment"]["version"],
        )
    )
    assert _task(client, "RETURN_DUE")["due_at"] == soon

    clock.advance(timedelta(hours=12))
    _tick(db_path, clock)
    assert _task(client, "RETURN_DUE")["status"] == "DUE"

    transition(
        client,
        loan_id=reserved["loan"]["id"],
        action="return",
        expected_version=picked["equipment"]["version"],
    )

    assert _task(client, "RETURN_DUE")["status"] == "RESOLVED"
    assert _events(client).count("RETURN_DUE") == 1, "resolving writes no second event"


# --- concurrency and contention -----------------------------------------


def test_two_independent_processors_produce_exactly_one_due_event(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    """Separate stores, separate connections, separate threads, one event."""
    _reserve(client)
    reports: list[Any] = []
    barrier = threading.Barrier(2)

    def run() -> None:
        store = SqliteStore(db_path)
        barrier.wait(timeout=10)
        reports.append(process_due_tasks(store, clock, SecretsIdGenerator(), limit=LIMIT))

    threads = [threading.Thread(target=run, name=f"proc-{i}") for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a processor thread did not finish"

    assert sum(report.marked_due for report in reports) == 1
    assert _events(client).count("PICKUP_DUE") == 1
    assert _task(client, "PICKUP_DUE")["status"] == "DUE"


def test_a_held_write_lock_is_reported_and_retried_not_swallowed(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    """Contention leaves the task PENDING and says so, then a later tick wins."""
    _reserve(client)
    # Built before the lock is taken: opening a store runs the migration check,
    # which is itself a write transaction, and this test is about the tick.
    store = SqliteStore(db_path)

    blocker = sqlite3.connect(db_path, timeout=0.1)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "INSERT INTO workspaces (id, created_at) VALUES ('lock-holder', ?)",
            (clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        contended = process_due_tasks(store, clock, SecretsIdGenerator(), limit=LIMIT)
    finally:
        blocker.rollback()
        blocker.close()

    assert contended.contended == 1
    assert contended.marked_due == 0
    assert _task(client, "PICKUP_DUE")["status"] == "PENDING", "nothing was written"
    assert "PICKUP_DUE" not in _events(client)

    recovered = _tick(db_path, clock)
    assert recovered.marked_due == 1
    assert _events(client).count("PICKUP_DUE") == 1


def test_storage_raises_a_port_level_error_when_the_writer_is_held(
    settings: Settings, db_path: Path, clock: FakeClock
) -> None:
    """The application layer never has to know what database this is."""
    store = SqliteStore(db_path)
    del settings
    blocker = sqlite3.connect(db_path, timeout=0.1)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "INSERT INTO workspaces (id, created_at) VALUES ('holder', ?)",
            (clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        with pytest.raises(StorageBusyError), store.transaction("anything"):
            pass  # pragma: no cover - the transaction never opens
    finally:
        blocker.rollback()
        blocker.close()


# --- bounds and isolation ------------------------------------------------


def test_a_tick_considers_at_most_its_candidate_limit(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    """The bound is real: three due tasks, a limit of two, two processed."""
    _reserve(client)
    client.cookies.clear()
    _reserve(client)
    client.cookies.clear()
    _reserve(client)

    first = _tick(db_path, clock, limit=2)
    assert first.considered == 2
    assert first.marked_due == 2

    second = _tick(db_path, clock, limit=2)
    assert second.marked_due == 1, "the backlog is worked off over later ticks"


def test_processing_one_workspace_writes_nothing_into_another(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    first = _reserve(client)
    first_events = len(_events(client))
    client.cookies.clear()
    second = _reserve(client)

    _tick(db_path, clock)

    second_snapshot = snapshot(client)
    assert [t["loan_id"] for t in second_snapshot["tasks"]] == [second["loan"]["id"]]
    assert second_snapshot["events"][0]["entity_id"] == second["loan"]["id"]
    assert len(second_snapshot["events"]) == first_events + 1
    assert first["loan"]["id"] != second["loan"]["id"]


def test_processing_never_changes_equipment_request_or_loan(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    """A notice is a record. It moves no inventory and approves nothing."""
    _reserve(client)
    before = snapshot(client)

    _tick(db_path, clock)

    after = snapshot(client)
    assert after["equipment"] == before["equipment"]
    assert after["requests"] == before["requests"]
    assert after["loans"] == before["loans"]
    assert after["tasks"][0]["status"] == TaskStatus.DUE.value


def test_a_due_event_is_attached_to_the_loan(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    reserved = _reserve(client)
    _tick(db_path, clock)

    newest = snapshot(client)["events"][0]
    assert newest["action"] == "PICKUP_DUE"
    assert newest["entity_type"] == "LOAN"
    assert newest["entity_id"] == reserved["loan"]["id"]
    assert newest["at"] == clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")


def test_an_idempotent_replay_after_processing_still_creates_nothing(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    """Replay returns stored bytes; it does not re-run against a DUE task."""
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]
    reuse = key("reserve")
    reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=wheelchair["version"],
        idempotency_key=reuse,
    )
    _tick(db_path, clock)

    replay = reserve(
        client,
        request_id=request_id,
        equipment_id=wheelchair["id"],
        expected_version=wheelchair["version"],
        idempotency_key=reuse,
    )

    assert replay.status_code == 200
    tasks = snapshot(client)["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "DUE", "the replay did not reset the notice"
    assert _events(client).count("PICKUP_DUE") == 1
