"""File-backed durability: reopen, constraints and quarantine release."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from borrowed_steps.config import Settings
from borrowed_steps.interfaces.http.app import SESSION_COOKIE, create_app
from conftest import FakeClock
from support import (
    body,
    create_request,
    equipment_of,
    inspect,
    reserve,
    snapshot,
    start_workspace,
    transition,
)


def test_state_survives_reopening_the_database_file(
    settings: Settings, clock: FakeClock, db_path: Path
) -> None:
    with TestClient(create_app(settings, clock=clock)) as first:
        start_workspace(first)
        wheelchair = equipment_of(snapshot(first), "WHEELCHAIR")
        request_id = body(create_request(first))["request"]["id"]
        loan = body(
            reserve(
                first,
                request_id=request_id,
                equipment_id=wheelchair["id"],
                expected_version=1,
            )
        )["loan"]
        transition(first, loan_id=loan["id"], action="pickup", expected_version=2)
        session_cookie = first.cookies.get(SESSION_COOKIE)

    assert session_cookie is not None
    assert db_path.exists()

    # A brand-new application object on the same file, as after a process restart.
    with TestClient(create_app(settings, clock=clock)) as second:
        second.cookies.set(SESSION_COOKIE, session_cookie)
        snap = snapshot(second)

    assert equipment_of(snap, "WHEELCHAIR")["state"] == "ON_LOAN"
    assert equipment_of(snap, "WHEELCHAIR")["version"] == 3
    assert [item["status"] for item in snap["loans"]] == ["ON_LOAN"]
    assert [item["status"] for item in snap["requests"]] == ["ON_LOAN"]
    assert [event["action"] for event in snap["events"]] == [
        "PICKED_UP",
        "RESERVED",
        "REQUEST_CREATED",
    ]


def test_migrations_are_applied_once(settings: Settings, clock: FakeClock, db_path: Path) -> None:
    create_app(settings, clock=clock)
    create_app(settings, clock=clock)

    connection = sqlite3.connect(db_path)
    try:
        versions = connection.execute("SELECT version FROM schema_migrations").fetchall()
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()

    # Both migrations, each recorded exactly once however often the app starts.
    assert versions == [(1,), (2,)]
    assert journal.lower() == "wal"


def test_foreign_keys_and_the_active_loan_index_are_enforced(
    client: TestClient, db_path: Path
) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    request_id = body(create_request(client))["request"]["id"]
    loan = body(
        reserve(client, request_id=request_id, equipment_id=wheelchair["id"], expected_version=1)
    )["loan"]

    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        row = connection.execute(
            "SELECT workspace_id FROM loans WHERE id = ?", (loan["id"],)
        ).fetchone()
        workspace_id = row[0]

        # A second open loan on the same item is impossible at the storage level.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO loans"
                " (id, workspace_id, request_id, equipment_id, status, due_at, created_at)"
                " VALUES (?, ?, ?, ?, 'RESERVED', ?, ?)",
                (
                    "second-loan",
                    workspace_id,
                    request_id,
                    wheelchair["id"],
                    "2026-09-14T09:00:00Z",
                    "2026-09-07T09:00:00Z",
                ),
            )

        # A loan pointing at an equipment row that does not exist is impossible.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO loans"
                " (id, workspace_id, request_id, equipment_id, status, due_at, created_at)"
                " VALUES (?, ?, ?, 'no-such-equipment', 'CLOSED', ?, ?)",
                (
                    "orphan-loan",
                    workspace_id,
                    request_id,
                    "2026-09-14T09:00:00Z",
                    "2026-09-07T09:00:00Z",
                ),
            )
    finally:
        connection.rollback()
        connection.close()


def test_quarantine_release_makes_the_item_available_again(client: TestClient) -> None:
    start_workspace(client)
    crutches = equipment_of(snapshot(client), "CRUTCHES")
    assert crutches["state"] == "QUARANTINED"

    released = body(
        inspect(
            client,
            equipment_id=crutches["id"],
            outcome="AVAILABLE",
            expected_version=crutches["version"],
        )
    )
    assert released["equipment"]["state"] == "AVAILABLE"
    assert released["equipment"]["version"] == 2
    assert released["request"] is None
    assert released["loan"] is None

    request_id = body(create_request(client, kind="CRUTCHES"))["request"]["id"]
    reserved = reserve(
        client,
        request_id=request_id,
        equipment_id=crutches["id"],
        expected_version=2,
    )
    assert reserved.status_code == 200
    assert [event["action"] for event in snapshot(client)["events"]] == [
        "RESERVED",
        "REQUEST_CREATED",
        "INSPECTED_AVAILABLE",
    ]


def test_a_returned_item_is_unavailable_until_it_is_inspected(client: TestClient) -> None:
    start_workspace(client)
    wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
    first = body(create_request(client))["request"]["id"]
    second = body(create_request(client, borrower_label="Borrower B"))["request"]["id"]

    loan = body(
        reserve(client, request_id=first, equipment_id=wheelchair["id"], expected_version=1)
    )["loan"]
    transition(client, loan_id=loan["id"], action="pickup", expected_version=2)
    transition(client, loan_id=loan["id"], action="return", expected_version=3)

    assert equipment_of(snapshot(client), "WHEELCHAIR")["state"] == "AWAITING_INSPECTION"
    blocked = reserve(client, request_id=second, equipment_id=wheelchair["id"], expected_version=4)
    assert blocked.status_code == 409

    inspect(client, equipment_id=wheelchair["id"], outcome="AVAILABLE", expected_version=4)
    allowed = reserve(client, request_id=second, equipment_id=wheelchair["id"], expected_version=5)
    assert allowed.status_code == 200
