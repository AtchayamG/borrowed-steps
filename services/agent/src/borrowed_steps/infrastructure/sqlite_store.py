"""File-backed SQLite implementation of the application storage ports.

Durability and isolation rules that the rest of the service relies on:

* every connection enables foreign keys and a busy timeout;
* the database file uses WAL, so a snapshot read never blocks a writer;
* a write transaction is ``BEGIN IMMEDIATE``, so two callers racing for the same
  item are serialised and the loser re-reads a changed version;
* a partial unique index makes a second open loan on one item impossible even if
  a future caller bypasses the rules;
* every statement is scoped by ``workspace_id``, so an identifier belonging to
  another workspace simply does not resolve.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from borrowed_steps.application.errors import StorageBusyError
from borrowed_steps.application.ports import DueTaskRef, IdempotencyRecord, Session
from borrowed_steps.domain.models import (
    CoordinationTask,
    EntityType,
    Equipment,
    EquipmentKind,
    EquipmentState,
    Event,
    EventAction,
    Loan,
    LoanStatus,
    Request,
    RequestStatus,
    TaskKind,
    TaskStatus,
)
from borrowed_steps.isotime import parse_iso, to_iso

__all__ = ["SqliteStore", "SqliteUnitOfWork"]

_BUSY_TIMEOUT_MS = 5000

_SCHEMA_V1: tuple[str, ...] = (
    """
    CREATE TABLE workspaces (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE equipment (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        label TEXT NOT NULL,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1)
    )
    """,
    "CREATE INDEX ix_equipment_workspace ON equipment(workspace_id)",
    """
    CREATE TABLE requests (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        borrower_label TEXT NOT NULL,
        equipment_kind TEXT NOT NULL,
        pickup_location TEXT NOT NULL,
        due_at TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_requests_workspace ON requests(workspace_id)",
    """
    CREATE TABLE loans (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        request_id TEXT NOT NULL REFERENCES requests(id),
        equipment_id TEXT NOT NULL REFERENCES equipment(id),
        status TEXT NOT NULL,
        due_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_loans_workspace ON loans(workspace_id)",
    """
    CREATE UNIQUE INDEX ux_loans_active_equipment
        ON loans(equipment_id) WHERE status IN ('RESERVED', 'ON_LOAN')
    """,
    """
    CREATE TABLE events (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        at TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_events_workspace ON events(workspace_id)",
    """
    CREATE TABLE idempotency (
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        key TEXT NOT NULL,
        route TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        status_code INTEGER NOT NULL,
        response_body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (workspace_id, key)
    )
    """,
)

# Version 2 adds persisted coordination tasks. It only ever adds: no statement
# in _SCHEMA_V1 is edited, and an existing database keeps every row it had.
#
# The composite foreign key is the point of ux_loans_workspace_id: it makes the
# storage layer itself refuse a task whose loan belongs to another workspace,
# rather than leaving that to a caller remembering to check.
#
# Backfill runs exactly once, gated by schema_migrations, and only for loans
# that are still open. A RETURNED or CLOSED loan gets nothing: there is no
# coordination left to do, and inventing a due event for a past deadline would
# be fabricating history. created_at is the migration instant - honest about
# when the record appeared - while due_at is the real deadline the loan already
# had. `strftime` here produces exactly the canonical whole-second UTC format
# in isotime.to_iso, and randomblob(12) matches the 24-hex-character shape of
# SecretsIdGenerator.new_id(); task ids are opaque handles inside a
# workspace-scoped read, not a security boundary.
_SCHEMA_V2: tuple[str, ...] = (
    "CREATE UNIQUE INDEX ux_loans_workspace_id ON loans(workspace_id, id)",
    """
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        loan_id TEXT NOT NULL REFERENCES loans(id),
        kind TEXT NOT NULL CHECK (kind IN ('PICKUP_DUE', 'RETURN_DUE')),
        status TEXT NOT NULL CHECK (status IN ('PENDING', 'DUE', 'RESOLVED')),
        due_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id, loan_id) REFERENCES loans(workspace_id, id)
    )
    """,
    "CREATE UNIQUE INDEX ux_tasks_loan_kind ON tasks(loan_id, kind)",
    "CREATE INDEX ix_tasks_pending_due ON tasks(status, due_at, id)",
    "CREATE INDEX ix_tasks_workspace ON tasks(workspace_id, created_at, id)",
    """
    INSERT INTO tasks (id, workspace_id, loan_id, kind, status, due_at, created_at)
    SELECT lower(hex(randomblob(12))), loans.workspace_id, loans.id,
           'PICKUP_DUE', 'PENDING', loans.created_at,
           strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    FROM loans WHERE loans.status = 'RESERVED'
    """,
    """
    INSERT INTO tasks (id, workspace_id, loan_id, kind, status, due_at, created_at)
    SELECT lower(hex(randomblob(12))), loans.workspace_id, loans.id,
           'RETURN_DUE', 'PENDING', loans.due_at,
           strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    FROM loans WHERE loans.status = 'ON_LOAN'
    """,
)

_MIGRATIONS: tuple[tuple[str, ...], ...] = (_SCHEMA_V1, _SCHEMA_V2)


def _to_equipment(row: sqlite3.Row) -> Equipment:
    return Equipment(
        id=str(row["id"]),
        label=str(row["label"]),
        kind=EquipmentKind(row["kind"]),
        state=EquipmentState(row["state"]),
        version=int(row["version"]),
    )


def _to_request(row: sqlite3.Row) -> Request:
    return Request(
        id=str(row["id"]),
        borrower_label=str(row["borrower_label"]),
        equipment_kind=EquipmentKind(row["equipment_kind"]),
        pickup_location=str(row["pickup_location"]),
        due_at=parse_iso(str(row["due_at"])),
        status=RequestStatus(row["status"]),
        created_at=parse_iso(str(row["created_at"])),
    )


def _to_loan(row: sqlite3.Row) -> Loan:
    return Loan(
        id=str(row["id"]),
        request_id=str(row["request_id"]),
        equipment_id=str(row["equipment_id"]),
        status=LoanStatus(row["status"]),
        due_at=parse_iso(str(row["due_at"])),
        created_at=parse_iso(str(row["created_at"])),
    )


def _to_task(row: sqlite3.Row) -> CoordinationTask:
    return CoordinationTask(
        id=str(row["id"]),
        loan_id=str(row["loan_id"]),
        kind=TaskKind(row["kind"]),
        status=TaskStatus(row["status"]),
        due_at=parse_iso(str(row["due_at"])),
        created_at=parse_iso(str(row["created_at"])),
    )


def _to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=str(row["id"]),
        entity_type=EntityType(row["entity_type"]),
        entity_id=str(row["entity_id"]),
        action=EventAction(row["action"]),
        at=parse_iso(str(row["at"])),
    )


class SqliteUnitOfWork:
    """Reads and writes inside one open transaction, scoped to one workspace."""

    __slots__ = ("_conn", "_workspace_id")

    def __init__(self, conn: sqlite3.Connection, workspace_id: str) -> None:
        self._conn = conn
        self._workspace_id = workspace_id

    def list_equipment(self) -> list[Equipment]:
        rows = self._conn.execute(
            "SELECT * FROM equipment WHERE workspace_id = ? ORDER BY rowid",
            (self._workspace_id,),
        ).fetchall()
        return [_to_equipment(row) for row in rows]

    def list_requests(self) -> list[Request]:
        rows = self._conn.execute(
            "SELECT * FROM requests WHERE workspace_id = ? ORDER BY created_at, id",
            (self._workspace_id,),
        ).fetchall()
        return [_to_request(row) for row in rows]

    def list_loans(self) -> list[Loan]:
        rows = self._conn.execute(
            "SELECT * FROM loans WHERE workspace_id = ? ORDER BY created_at, id",
            (self._workspace_id,),
        ).fetchall()
        return [_to_loan(row) for row in rows]

    def list_events(self) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE workspace_id = ? ORDER BY rowid DESC",
            (self._workspace_id,),
        ).fetchall()
        return [_to_event(row) for row in rows]

    def get_equipment(self, equipment_id: str) -> Equipment | None:
        row = self._conn.execute(
            "SELECT * FROM equipment WHERE workspace_id = ? AND id = ?",
            (self._workspace_id, equipment_id),
        ).fetchone()
        return None if row is None else _to_equipment(row)

    def get_request(self, request_id: str) -> Request | None:
        row = self._conn.execute(
            "SELECT * FROM requests WHERE workspace_id = ? AND id = ?",
            (self._workspace_id, request_id),
        ).fetchone()
        return None if row is None else _to_request(row)

    def get_loan(self, loan_id: str) -> Loan | None:
        row = self._conn.execute(
            "SELECT * FROM loans WHERE workspace_id = ? AND id = ?",
            (self._workspace_id, loan_id),
        ).fetchone()
        return None if row is None else _to_loan(row)

    def find_returned_loan(self, equipment_id: str) -> Loan | None:
        row = self._conn.execute(
            "SELECT * FROM loans"
            " WHERE workspace_id = ? AND equipment_id = ? AND status = ?"
            " ORDER BY rowid DESC LIMIT 1",
            (self._workspace_id, equipment_id, LoanStatus.RETURNED.value),
        ).fetchone()
        return None if row is None else _to_loan(row)

    def add_request(self, request: Request) -> None:
        self._conn.execute(
            "INSERT INTO requests"
            " (id, workspace_id, borrower_label, equipment_kind, pickup_location,"
            "  due_at, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.id,
                self._workspace_id,
                request.borrower_label,
                request.equipment_kind.value,
                request.pickup_location,
                to_iso(request.due_at),
                request.status.value,
                to_iso(request.created_at),
            ),
        )

    def add_loan(self, loan: Loan) -> None:
        self._conn.execute(
            "INSERT INTO loans"
            " (id, workspace_id, request_id, equipment_id, status, due_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                loan.id,
                self._workspace_id,
                loan.request_id,
                loan.equipment_id,
                loan.status.value,
                to_iso(loan.due_at),
                to_iso(loan.created_at),
            ),
        )

    def add_event(self, event: Event) -> None:
        self._conn.execute(
            "INSERT INTO events (id, workspace_id, entity_type, entity_id, action, at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.id,
                self._workspace_id,
                event.entity_type.value,
                event.entity_id,
                event.action.value,
                to_iso(event.at),
            ),
        )

    def save_request(self, request: Request) -> None:
        self._conn.execute(
            "UPDATE requests SET status = ? WHERE workspace_id = ? AND id = ?",
            (request.status.value, self._workspace_id, request.id),
        )

    def save_loan(self, loan: Loan) -> None:
        self._conn.execute(
            "UPDATE loans SET status = ? WHERE workspace_id = ? AND id = ?",
            (loan.status.value, self._workspace_id, loan.id),
        )

    def save_equipment(self, equipment: Equipment) -> None:
        self._conn.execute(
            "UPDATE equipment SET state = ?, version = ? WHERE workspace_id = ? AND id = ?",
            (equipment.state.value, equipment.version, self._workspace_id, equipment.id),
        )

    def get_idempotency(self, key: str) -> IdempotencyRecord | None:
        row = self._conn.execute(
            "SELECT * FROM idempotency WHERE workspace_id = ? AND key = ?",
            (self._workspace_id, key),
        ).fetchone()
        if row is None:
            return None
        return IdempotencyRecord(
            key=str(row["key"]),
            route=str(row["route"]),
            request_hash=str(row["request_hash"]),
            status_code=int(row["status_code"]),
            response_body=str(row["response_body"]),
        )

    def save_idempotency(self, record: IdempotencyRecord) -> None:
        self._conn.execute(
            "INSERT INTO idempotency"
            " (workspace_id, key, route, request_hash, status_code, response_body, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._workspace_id,
                record.key,
                record.route,
                record.request_hash,
                record.status_code,
                record.response_body,
                to_iso(datetime.now(UTC)),
            ),
        )

    # --- coordination tasks ---------------------------------------------
    #
    # Every statement below is scoped by workspace_id like the rest of this
    # class, so a task id from another workspace simply does not resolve.

    def list_tasks(self) -> list[CoordinationTask]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE workspace_id = ? ORDER BY created_at, id",
            (self._workspace_id,),
        ).fetchall()
        return [_to_task(row) for row in rows]

    def get_task(self, task_id: str) -> CoordinationTask | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE workspace_id = ? AND id = ?",
            (self._workspace_id, task_id),
        ).fetchone()
        return None if row is None else _to_task(row)

    def find_task(self, loan_id: str, kind: TaskKind) -> CoordinationTask | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE workspace_id = ? AND loan_id = ? AND kind = ?",
            (self._workspace_id, loan_id, kind.value),
        ).fetchone()
        return None if row is None else _to_task(row)

    def open_tasks_for_loan(self, loan_id: str) -> list[CoordinationTask]:
        rows = self._conn.execute(
            "SELECT * FROM tasks"
            " WHERE workspace_id = ? AND loan_id = ? AND status <> ?"
            " ORDER BY created_at, id",
            (self._workspace_id, loan_id, TaskStatus.RESOLVED.value),
        ).fetchall()
        return [_to_task(row) for row in rows]

    def add_task(self, task: CoordinationTask) -> None:
        """Insert one task.

        The composite foreign key on (workspace_id, loan_id) means SQLite
        itself rejects a loan belonging to another workspace; there is no
        application-side check to forget.
        """
        self._conn.execute(
            "INSERT INTO tasks"
            " (id, workspace_id, loan_id, kind, status, due_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task.id,
                self._workspace_id,
                task.loan_id,
                task.kind.value,
                task.status.value,
                to_iso(task.due_at),
                to_iso(task.created_at),
            ),
        )

    def resolve_task(self, task_id: str) -> bool:
        cursor = self._conn.execute(
            "UPDATE tasks SET status = ? WHERE workspace_id = ? AND id = ? AND status <> ?",
            (
                TaskStatus.RESOLVED.value,
                self._workspace_id,
                task_id,
                TaskStatus.RESOLVED.value,
            ),
        )
        return cursor.rowcount == 1

    def mark_task_due(self, task_id: str) -> bool:
        """Claim one PENDING task, reporting whether this call really claimed it.

        The PENDING condition lives in the statement rather than in a prior
        read, so the answer cannot be stale: two runners inside two write
        transactions cannot both see rowcount 1, and a later tick over an
        already-DUE task claims nothing and writes no second event.
        """
        cursor = self._conn.execute(
            "UPDATE tasks SET status = ? WHERE workspace_id = ? AND id = ? AND status = ?",
            (
                TaskStatus.DUE.value,
                self._workspace_id,
                task_id,
                TaskStatus.PENDING.value,
            ),
        )
        return cursor.rowcount == 1


class SqliteStore:
    """Durable storage on one SQLite file."""

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = path
        parent = path.parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        return conn

    def _initialise(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations"
                    " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
                applied = int(row["version"])
                now = to_iso(datetime.now(UTC))
                for version, statements in enumerate(_MIGRATIONS, start=1):
                    if version <= applied:
                        continue
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (version, now),
                    )
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self, workspace_id: str, *, write: bool = True) -> Iterator[SqliteUnitOfWork]:
        """Open a transaction scoped to ``workspace_id``."""
        conn = self._connect()
        try:
            try:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN DEFERRED")
            except sqlite3.OperationalError as error:
                # Another writer still held the lock when the busy timeout ran
                # out. Nothing was begun, so nothing needs undoing. Translated
                # here so callers that can retry do not have to know what
                # database this is.
                if _is_contention(error):
                    raise StorageBusyError(str(error)) from error
                raise
            try:
                yield SqliteUnitOfWork(conn, workspace_id)
            except BaseException:
                _rollback(conn)
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def create_workspace(self, workspace_id: str, equipment: Sequence[Equipment]) -> None:
        """Create a workspace and its seeded equipment in one transaction."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO workspaces (id, created_at) VALUES (?, ?)",
                    (workspace_id, to_iso(datetime.now(UTC))),
                )
                for item in equipment:
                    conn.execute(
                        "INSERT INTO equipment"
                        " (id, workspace_id, label, kind, state, version)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            item.id,
                            workspace_id,
                            item.label,
                            item.kind.value,
                            item.state.value,
                            item.version,
                        ),
                    )
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        finally:
            conn.close()

    def create_session(self, session: Session) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO sessions (id, workspace_id, created_at, expires_at)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        session.id,
                        session.workspace_id,
                        to_iso(session.created_at),
                        to_iso(session.expires_at),
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        finally:
            conn.close()

    def get_session(self, session_id: str) -> Session | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return Session(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            created_at=parse_iso(str(row["created_at"])),
            expires_at=parse_iso(str(row["expires_at"])),
        )

    def due_task_candidates(self, now: datetime, limit: int) -> list[DueTaskRef]:
        """Identifiers only, across workspaces, for the scheduler alone.

        A read on its own connection, outside any write transaction: discovery
        must not hold the single writer while the runner works through
        candidates one workspace at a time.

        ``due_at <= ?`` makes equality count as due, and comparing the stored
        text is exact because ``to_iso`` writes one fixed-width whole-second UTC
        form for every row.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT workspace_id, id FROM tasks"
                " WHERE status = ? AND due_at <= ?"
                " ORDER BY due_at, id LIMIT ?",
                (TaskStatus.PENDING.value, to_iso(now), limit),
            ).fetchall()
        finally:
            conn.close()
        return [DueTaskRef(workspace_id=str(r["workspace_id"]), task_id=str(r["id"])) for r in rows]


def _is_contention(error: sqlite3.OperationalError) -> bool:
    """True for the two lock messages SQLite reports when a writer is busy."""
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _rollback(conn: sqlite3.Connection) -> None:
    # No transaction is open when BEGIN itself failed; nothing to undo.
    with suppress(sqlite3.Error):
        conn.execute("ROLLBACK")
