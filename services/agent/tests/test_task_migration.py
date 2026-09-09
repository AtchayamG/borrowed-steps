"""Upgrading a real v1 database to v2, with real rows already in it.

These build an actual version-1 database by running the v1 statements alone,
insert loans in every status, and then open a ``SqliteStore`` over that file -
which is exactly what happens to a database that existed before this milestone.
Nothing is faked: the migration under test is the one production runs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from borrowed_steps.infrastructure.sqlite_store import _SCHEMA_V1, SqliteStore

WORKSPACE = "workspace-one"
OTHER_WORKSPACE = "workspace-two"
CREATED = "2026-09-07T09:00:00Z"
DUE = "2026-09-14T09:00:00Z"


def _v1_database(path: Path, loans: list[tuple[str, str, str]]) -> None:
    """Write a genuine v1 database containing ``(loan_id, workspace, status)``."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for statement in _SCHEMA_V1:
            conn.execute(statement)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations"
            " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)", (CREATED,)
        )
        for workspace in {workspace for _, workspace, _ in loans}:
            conn.execute(
                "INSERT INTO workspaces (id, created_at) VALUES (?, ?)", (workspace, CREATED)
            )
        for loan_id, workspace, status in loans:
            equipment_id = f"eq-{loan_id}"
            request_id = f"rq-{loan_id}"
            conn.execute(
                "INSERT INTO equipment (id, workspace_id, label, kind, state, version)"
                " VALUES (?, ?, ?, 'WHEELCHAIR', 'ON_LOAN', 1)",
                (equipment_id, workspace, f"Wheelchair {loan_id}"),
            )
            conn.execute(
                "INSERT INTO requests"
                " (id, workspace_id, borrower_label, equipment_kind, pickup_location,"
                "  due_at, status, created_at)"
                " VALUES (?, ?, 'Borrower A', 'WHEELCHAIR', 'Velachery room', ?, 'ON_LOAN', ?)",
                (request_id, workspace, DUE, CREATED),
            )
            conn.execute(
                "INSERT INTO loans"
                " (id, workspace_id, request_id, equipment_id, status, due_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (loan_id, workspace, request_id, equipment_id, status, DUE, CREATED),
            )
        conn.commit()
    finally:
        conn.close()


def _tasks(path: Path) -> list[tuple[str, str, str, str, str]]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT loan_id, workspace_id, kind, status, due_at FROM tasks ORDER BY loan_id, kind"
        ).fetchall()
    finally:
        conn.close()
    return [(str(a), str(b), str(c), str(d), str(e)) for a, b, c, d, e in rows]


def test_v1_upgrades_in_place_and_backfills_only_open_loans(tmp_path: Path) -> None:
    """The upgrade adds tasks for open loans and touches nothing else."""
    path = tmp_path / "v1.db"
    _v1_database(
        path,
        [
            ("loan-reserved", WORKSPACE, "RESERVED"),
            ("loan-on-loan", WORKSPACE, "ON_LOAN"),
            ("loan-returned", WORKSPACE, "RETURNED"),
            ("loan-closed", WORKSPACE, "CLOSED"),
        ],
    )

    SqliteStore(path)

    assert _tasks(path) == [
        # A reserved loan still needs collecting: due at the loan's own
        # creation instant, which is the moment coordination became possible.
        ("loan-on-loan", WORKSPACE, "RETURN_DUE", "PENDING", DUE),
        ("loan-reserved", WORKSPACE, "PICKUP_DUE", "PENDING", CREATED),
    ]

    conn = sqlite3.connect(path)
    try:
        versions = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        loans = conn.execute("SELECT COUNT(*) FROM loans").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()

    assert versions == [(1,), (2,)]
    assert loans == 4, "the upgrade must not delete or rewrite existing loans"
    # A backfilled task records work still to do. Inventing a due event for a
    # deadline that passed before this code existed would be writing history.
    assert events == 0


def test_the_backfill_records_the_migration_instant_not_a_fabricated_one(tmp_path: Path) -> None:
    """created_at is when the row appeared; due_at is the deadline it inherited."""
    path = tmp_path / "v1.db"
    _v1_database(path, [("loan-on-loan", WORKSPACE, "ON_LOAN")])

    SqliteStore(path)

    conn = sqlite3.connect(path)
    try:
        due_at, created_at = conn.execute("SELECT due_at, created_at FROM tasks").fetchone()
    finally:
        conn.close()

    assert due_at == DUE, "the return deadline is the loan's real one"
    assert created_at != DUE, "created_at is the migration instant, not the loan's due date"
    assert created_at.endswith("Z")
    assert len(created_at) == len("2026-09-07T09:00:00Z")


def test_reopening_the_store_repeatedly_creates_no_duplicate_tasks(tmp_path: Path) -> None:
    """Restart is not an event. Three opens, one set of tasks."""
    path = tmp_path / "v1.db"
    _v1_database(
        path,
        [("loan-reserved", WORKSPACE, "RESERVED"), ("loan-on-loan", OTHER_WORKSPACE, "ON_LOAN")],
    )

    SqliteStore(path)
    first = _tasks(path)
    SqliteStore(path)
    SqliteStore(path)

    assert _tasks(path) == first
    assert len(first) == 2


def test_existing_workspaces_are_upgraded_alongside_new_ones(tmp_path: Path) -> None:
    """Two workspaces already in the file both get their own tasks."""
    path = tmp_path / "v1.db"
    _v1_database(
        path,
        [
            ("loan-a", WORKSPACE, "RESERVED"),
            ("loan-b", OTHER_WORKSPACE, "ON_LOAN"),
        ],
    )

    SqliteStore(path)

    assert _tasks(path) == [
        ("loan-a", WORKSPACE, "PICKUP_DUE", "PENDING", CREATED),
        ("loan-b", OTHER_WORKSPACE, "RETURN_DUE", "PENDING", DUE),
    ]


def test_a_fresh_database_gets_both_migrations_and_no_tasks(tmp_path: Path) -> None:
    """Nothing to backfill when there is nothing there yet."""
    path = tmp_path / "fresh.db"

    SqliteStore(path)

    conn = sqlite3.connect(path)
    try:
        versions = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()

    assert versions == [(1,), (2,)]
    assert tasks == 0


def test_storage_refuses_a_task_whose_loan_is_in_another_workspace(tmp_path: Path) -> None:
    """The composite foreign key, not a caller's memory, enforces this."""
    path = tmp_path / "v1.db"
    _v1_database(path, [("loan-a", WORKSPACE, "RESERVED")])
    SqliteStore(path)

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute(
                "INSERT INTO tasks"
                " (id, workspace_id, loan_id, kind, status, due_at, created_at)"
                " VALUES ('t1', ?, 'loan-a', 'RETURN_DUE', 'PENDING', ?, ?)",
                (OTHER_WORKSPACE, DUE, CREATED),
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover - only reached if the constraint is missing
            msg = "a task was accepted for a loan in another workspace"
            raise AssertionError(msg)
    finally:
        conn.close()


def test_storage_refuses_a_second_task_of_the_same_kind_for_one_loan(tmp_path: Path) -> None:
    """UNIQUE(loan_id, kind) is the durable no-duplicates guarantee."""
    path = tmp_path / "v1.db"
    _v1_database(path, [("loan-a", WORKSPACE, "ON_LOAN")])
    SqliteStore(path)

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute(
                "INSERT INTO tasks"
                " (id, workspace_id, loan_id, kind, status, due_at, created_at)"
                " VALUES ('t2', ?, 'loan-a', 'RETURN_DUE', 'PENDING', ?, ?)",
                (WORKSPACE, DUE, CREATED),
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover - only reached if the index is missing
            msg = "a second RETURN_DUE task was accepted for one loan"
            raise AssertionError(msg)
    finally:
        conn.close()


def test_storage_refuses_an_unknown_kind_or_status(tmp_path: Path) -> None:
    """The CHECK constraints keep the enums honest at rest."""
    path = tmp_path / "v1.db"
    _v1_database(path, [("loan-a", WORKSPACE, "ON_LOAN")])
    SqliteStore(path)

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for kind, status in (("SOMETHING_ELSE", "PENDING"), ("PICKUP_DUE", "ACKNOWLEDGED")):
            try:
                conn.execute(
                    "INSERT INTO tasks"
                    " (id, workspace_id, loan_id, kind, status, due_at, created_at)"
                    " VALUES ('t3', ?, 'loan-a', ?, ?, ?, ?)",
                    (WORKSPACE, kind, status, DUE, CREATED),
                )
            except sqlite3.IntegrityError:
                continue
            msg = f"storage accepted kind={kind} status={status}"  # pragma: no cover
            raise AssertionError(msg)  # pragma: no cover
    finally:
        conn.close()
