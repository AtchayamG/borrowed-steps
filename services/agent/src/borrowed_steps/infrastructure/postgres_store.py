"""PostgreSQL implementation of the application storage ports.

The same guarantees the SQLite adapter makes, made a different way, because
PostgreSQL has real concurrency instead of one writer at a time:

* **A workspace is the unit of serialisation.** Every write transaction takes
  ``SELECT ... FOR UPDATE`` on its ``workspaces`` row *before* reading any
  lifecycle or idempotency state. Two callers touching one workspace queue up;
  two callers in different workspaces do not see each other at all. Under READ
  COMMITTED that row lock is what makes a read-then-write sequence safe, and it
  is why an idempotency check cannot be overtaken between the lookup and the
  insert.
* **A read is a consistent snapshot.** Read-only transactions run REPEATABLE
  READ, so a dashboard that lists equipment, loans, events and tasks cannot
  catch a mutation half-applied across those five queries.
* **The constraints are the backstop, not the plan.** A partial unique index
  still refuses a second open loan on one item, and composite foreign keys
  still refuse a loan, task or request that belongs to another workspace. If
  the locking discipline were ever bypassed, the database would still say no.
* **Nothing is held open.** Each unit of work gets its own connection, closed
  on success, on error and on contention alike. There is no in-process pool
  keeping backends alive, and no transaction ever spans an inference or HTTP
  call, because none is made from in here.

Migrations live in :mod:`borrowed_steps.infrastructure.postgres_migrations`
and are never run by this class. The constructor connects to nothing;
:meth:`PostgresStore.check_schema` is the explicit, read-only way to ask
whether the database is ready.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import DictRow, dict_row

from borrowed_steps.application.errors import NotFoundError, StorageBusyError
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
from borrowed_steps.infrastructure.postgres_migrations import (
    EXPECTED_SCHEMA_VERSION,
    SchemaVersionError,
    read_schema_version,
    require_transport_security,
)
from borrowed_steps.isotime import parse_iso, to_iso

__all__ = ["PostgresStore", "PostgresUnitOfWork"]

_CONNECT_TIMEOUT_S = 15
_STATEMENT_TIMEOUT_MS = 15_000
_LOCK_TIMEOUT_MS = 10_000


def _ts(value: datetime) -> datetime:
    """Normalise to the one instant shape the whole service agrees on.

    Round-tripping through the canonical text form is deliberate rather than
    wasteful: it is exactly what the SQLite adapter stores, so a whole-second
    UTC value goes in and the same whole-second UTC value comes back, and the
    two adapters cannot drift apart on sub-second precision or on a naive
    datetime slipping in from a caller.
    """
    return parse_iso(to_iso(value))


def _out(value: datetime) -> datetime:
    """A TIMESTAMPTZ read back as UTC, whatever the server session says."""
    return value.astimezone(UTC)


def _is_contention(error: psycopg.Error) -> bool:
    """True only for "try again", never for "something is actually wrong".

    Lock timeouts, statement timeouts, deadlocks and serialisation failures are
    the four ways PostgreSQL says another transaction got in the way. A
    violated constraint, a missing table or a broken connection are none of
    those, and quietly relabelling them ``StorageBusyError`` would turn a real
    defect into a retry loop that hides it.
    """
    return isinstance(
        error,
        pg_errors.LockNotAvailable
        | pg_errors.QueryCanceled
        | pg_errors.DeadlockDetected
        | pg_errors.SerializationFailure,
    )


def _to_equipment(row: DictRow) -> Equipment:
    return Equipment(
        id=str(row["id"]),
        label=str(row["label"]),
        kind=EquipmentKind(row["kind"]),
        state=EquipmentState(row["state"]),
        version=int(row["version"]),
    )


def _to_request(row: DictRow) -> Request:
    return Request(
        id=str(row["id"]),
        borrower_label=str(row["borrower_label"]),
        equipment_kind=EquipmentKind(row["equipment_kind"]),
        pickup_location=str(row["pickup_location"]),
        due_at=_out(row["due_at"]),
        status=RequestStatus(row["status"]),
        created_at=_out(row["created_at"]),
    )


def _to_loan(row: DictRow) -> Loan:
    return Loan(
        id=str(row["id"]),
        request_id=str(row["request_id"]),
        equipment_id=str(row["equipment_id"]),
        status=LoanStatus(row["status"]),
        due_at=_out(row["due_at"]),
        created_at=_out(row["created_at"]),
    )


def _to_task(row: DictRow) -> CoordinationTask:
    return CoordinationTask(
        id=str(row["id"]),
        loan_id=str(row["loan_id"]),
        kind=TaskKind(row["kind"]),
        status=TaskStatus(row["status"]),
        due_at=_out(row["due_at"]),
        created_at=_out(row["created_at"]),
    )


def _to_event(row: DictRow) -> Event:
    return Event(
        id=str(row["id"]),
        entity_type=EntityType(row["entity_type"]),
        entity_id=str(row["entity_id"]),
        action=EventAction(row["action"]),
        at=_out(row["at"]),
    )


class PostgresUnitOfWork:
    """Reads and writes inside one open transaction, scoped to one workspace.

    Every statement carries ``workspace_id`` in its WHERE clause, so an
    identifier from another workspace does not resolve - the same rule the
    SQLite adapter follows, for the same reason: isolation should not depend on
    a caller remembering to filter.
    """

    __slots__ = ("_conn", "_workspace_id")

    def __init__(self, conn: psycopg.Connection[DictRow], workspace_id: str) -> None:
        self._conn = conn
        self._workspace_id = workspace_id

    def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[DictRow]:
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def _fetchone(self, sql: str, params: tuple[Any, ...]) -> DictRow | None:
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()

    def _execute(self, sql: str, params: tuple[Any, ...]) -> int:
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount

    def list_equipment(self) -> list[Equipment]:
        rows = self._fetchall(
            "SELECT * FROM equipment WHERE workspace_id = %s ORDER BY seq",
            (self._workspace_id,),
        )
        return [_to_equipment(row) for row in rows]

    def list_requests(self) -> list[Request]:
        rows = self._fetchall(
            "SELECT * FROM requests WHERE workspace_id = %s ORDER BY created_at, id",
            (self._workspace_id,),
        )
        return [_to_request(row) for row in rows]

    def list_loans(self) -> list[Loan]:
        rows = self._fetchall(
            "SELECT * FROM loans WHERE workspace_id = %s ORDER BY created_at, id",
            (self._workspace_id,),
        )
        return [_to_loan(row) for row in rows]

    def list_events(self) -> list[Event]:
        rows = self._fetchall(
            "SELECT * FROM events WHERE workspace_id = %s ORDER BY seq DESC",
            (self._workspace_id,),
        )
        return [_to_event(row) for row in rows]

    def get_equipment(self, equipment_id: str) -> Equipment | None:
        row = self._fetchone(
            "SELECT * FROM equipment WHERE workspace_id = %s AND id = %s",
            (self._workspace_id, equipment_id),
        )
        return None if row is None else _to_equipment(row)

    def get_request(self, request_id: str) -> Request | None:
        row = self._fetchone(
            "SELECT * FROM requests WHERE workspace_id = %s AND id = %s",
            (self._workspace_id, request_id),
        )
        return None if row is None else _to_request(row)

    def get_loan(self, loan_id: str) -> Loan | None:
        row = self._fetchone(
            "SELECT * FROM loans WHERE workspace_id = %s AND id = %s",
            (self._workspace_id, loan_id),
        )
        return None if row is None else _to_loan(row)

    def find_returned_loan(self, equipment_id: str) -> Loan | None:
        """The most recently created returned loan for this item.

        ``seq`` rather than ``created_at`` because two loans can share a
        whole-second timestamp, and "most recent" then has to mean insertion
        order - which is what the SQLite adapter's ``rowid DESC`` meant.
        """
        row = self._fetchone(
            "SELECT * FROM loans"
            " WHERE workspace_id = %s AND equipment_id = %s AND status = %s"
            " ORDER BY seq DESC LIMIT 1",
            (self._workspace_id, equipment_id, LoanStatus.RETURNED.value),
        )
        return None if row is None else _to_loan(row)

    def add_request(self, request: Request) -> None:
        self._execute(
            "INSERT INTO requests"
            " (id, workspace_id, borrower_label, equipment_kind, pickup_location,"
            "  due_at, status, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                request.id,
                self._workspace_id,
                request.borrower_label,
                request.equipment_kind.value,
                request.pickup_location,
                _ts(request.due_at),
                request.status.value,
                _ts(request.created_at),
            ),
        )

    def add_loan(self, loan: Loan) -> None:
        self._execute(
            "INSERT INTO loans"
            " (id, workspace_id, request_id, equipment_id, status, due_at, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                loan.id,
                self._workspace_id,
                loan.request_id,
                loan.equipment_id,
                loan.status.value,
                _ts(loan.due_at),
                _ts(loan.created_at),
            ),
        )

    def add_event(self, event: Event) -> None:
        self._execute(
            "INSERT INTO events (id, workspace_id, entity_type, entity_id, action, at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                event.id,
                self._workspace_id,
                event.entity_type.value,
                event.entity_id,
                event.action.value,
                _ts(event.at),
            ),
        )

    def save_request(self, request: Request) -> None:
        self._execute(
            "UPDATE requests SET status = %s WHERE workspace_id = %s AND id = %s",
            (request.status.value, self._workspace_id, request.id),
        )

    def save_loan(self, loan: Loan) -> None:
        self._execute(
            "UPDATE loans SET status = %s WHERE workspace_id = %s AND id = %s",
            (loan.status.value, self._workspace_id, loan.id),
        )

    def save_equipment(self, equipment: Equipment) -> None:
        self._execute(
            "UPDATE equipment SET state = %s, version = %s WHERE workspace_id = %s AND id = %s",
            (equipment.state.value, equipment.version, self._workspace_id, equipment.id),
        )

    def get_idempotency(self, key: str) -> IdempotencyRecord | None:
        row = self._fetchone(
            "SELECT * FROM idempotency WHERE workspace_id = %s AND key = %s",
            (self._workspace_id, key),
        )
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
        """Store the completed outcome, in the same transaction as its effects.

        No ``ON CONFLICT`` clause, and that is the point: the workspace row is
        already locked, so a duplicate key here would mean the serialisation
        discipline had failed. Swallowing it would hide that; letting it raise
        rolls the whole attempt back.

        ``response_body`` goes in as the exact string it arrived as, into a
        TEXT column, so a replay returns byte-identical content.
        """
        self._execute(
            "INSERT INTO idempotency"
            " (workspace_id, key, route, request_hash, status_code, response_body, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                self._workspace_id,
                record.key,
                record.route,
                record.request_hash,
                record.status_code,
                record.response_body,
                _ts(datetime.now(UTC)),
            ),
        )

    # --- coordination tasks ---------------------------------------------

    def list_tasks(self) -> list[CoordinationTask]:
        rows = self._fetchall(
            "SELECT * FROM tasks WHERE workspace_id = %s ORDER BY created_at, id",
            (self._workspace_id,),
        )
        return [_to_task(row) for row in rows]

    def get_task(self, task_id: str) -> CoordinationTask | None:
        row = self._fetchone(
            "SELECT * FROM tasks WHERE workspace_id = %s AND id = %s",
            (self._workspace_id, task_id),
        )
        return None if row is None else _to_task(row)

    def find_task(self, loan_id: str, kind: TaskKind) -> CoordinationTask | None:
        row = self._fetchone(
            "SELECT * FROM tasks WHERE workspace_id = %s AND loan_id = %s AND kind = %s",
            (self._workspace_id, loan_id, kind.value),
        )
        return None if row is None else _to_task(row)

    def open_tasks_for_loan(self, loan_id: str) -> list[CoordinationTask]:
        rows = self._fetchall(
            "SELECT * FROM tasks"
            " WHERE workspace_id = %s AND loan_id = %s AND status <> %s"
            " ORDER BY created_at, id",
            (self._workspace_id, loan_id, TaskStatus.RESOLVED.value),
        )
        return [_to_task(row) for row in rows]

    def add_task(self, task: CoordinationTask) -> None:
        """Insert one task.

        The composite foreign key on ``(workspace_id, loan_id)`` means
        PostgreSQL itself rejects a loan belonging to another workspace; there
        is no application-side check to forget.
        """
        self._execute(
            "INSERT INTO tasks"
            " (id, workspace_id, loan_id, kind, status, due_at, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                task.id,
                self._workspace_id,
                task.loan_id,
                task.kind.value,
                task.status.value,
                _ts(task.due_at),
                _ts(task.created_at),
            ),
        )

    def resolve_task(self, task_id: str) -> bool:
        rowcount = self._execute(
            "UPDATE tasks SET status = %s WHERE workspace_id = %s AND id = %s AND status <> %s",
            (
                TaskStatus.RESOLVED.value,
                self._workspace_id,
                task_id,
                TaskStatus.RESOLVED.value,
            ),
        )
        return rowcount == 1

    def mark_task_due(self, task_id: str) -> bool:
        """Claim one PENDING task, reporting whether this call really claimed it.

        The PENDING condition lives in the statement, not in a prior read, so
        the answer cannot be stale. Two processors racing on one task are
        already serialised by the workspace row lock; the loser then finds the
        row no longer PENDING, updates nothing, and is told so - which is how
        one due task yields exactly one event however many runners tick.
        """
        rowcount = self._execute(
            "UPDATE tasks SET status = %s WHERE workspace_id = %s AND id = %s AND status = %s",
            (
                TaskStatus.DUE.value,
                self._workspace_id,
                task_id,
                TaskStatus.PENDING.value,
            ),
        )
        return rowcount == 1


class PostgresStore:
    """Durable storage for every workspace, on one PostgreSQL database."""

    __slots__ = (
        "_connect_timeout_s",
        "_database_url",
        "_lock_timeout_ms",
        "_statement_timeout_ms",
    )

    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_s: int | None = None,
        statement_timeout_ms: int | None = None,
        lock_timeout_ms: int | None = None,
    ) -> None:
        """Validate the URL and keep it. Nothing connects, nothing migrates.

        A constructor that quietly created a schema would make "is the database
        ready?" unanswerable and would let a mistyped URL bring a new schema
        into existence somewhere it did not belong. Readiness is
        :meth:`check_schema`; changing the schema is ``apply_migrations``.
        """
        require_transport_security(database_url)
        self._database_url = database_url
        self._connect_timeout_s = connect_timeout_s
        self._statement_timeout_ms = statement_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms

    def _connect(self, *, connect_timeout_s: int | None = None) -> psycopg.Connection[DictRow]:
        """One short-lived connection, bounded from the moment it opens.

        Not pooled and not kept alive: a unit of work opens a connection, does
        its work and closes it. ``prepare_threshold=None`` keeps the client
        from creating prepared statements without assuming pooler support.
        """
        fallback_timeout = (
            self._connect_timeout_s if self._connect_timeout_s is not None else _CONNECT_TIMEOUT_S
        )
        timeout = connect_timeout_s if connect_timeout_s is not None else fallback_timeout
        return psycopg.connect(
            self._database_url,
            autocommit=False,
            connect_timeout=timeout,
            prepare_threshold=None,
            row_factory=dict_row,
            options=(
                f"-c statement_timeout={self._statement_timeout_ms}"
                if self._statement_timeout_ms is not None
                else ""
            ),
        )

    def check_schema(self) -> None:
        """Read-only readiness check. Raises on anything but the exact version.

        Explicit and separate so a caller decides when to ask, and so asking
        never becomes a write.
        """
        version = read_schema_version(self._database_url)
        if version != EXPECTED_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"PostgreSQL schema version {version}, expected {EXPECTED_SCHEMA_VERSION}."
            )

    @contextmanager
    def _connection(
        self,
        *,
        read_only: bool = False,
        connect_timeout_s: int | None = None,
        statement_timeout_ms: int | None = None,
        lock_timeout_ms: int | None = None,
    ) -> Iterator[psycopg.Connection[DictRow]]:
        stmt_timeout = (
            statement_timeout_ms
            if statement_timeout_ms is not None
            else (
                self._statement_timeout_ms
                if self._statement_timeout_ms is not None
                else _STATEMENT_TIMEOUT_MS
            )
        )
        lock_timeout = (
            lock_timeout_ms
            if lock_timeout_ms is not None
            else (self._lock_timeout_ms if self._lock_timeout_ms is not None else _LOCK_TIMEOUT_MS)
        )
        try:
            conn_cm = (
                self._connect(connect_timeout_s=connect_timeout_s)
                if connect_timeout_s is not None
                else self._connect()
            )
            with conn_cm as conn:
                if read_only:
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                conn.execute(f"SET LOCAL lock_timeout = '{lock_timeout}ms'")
                conn.execute(f"SET LOCAL statement_timeout = '{stmt_timeout}ms'")
                yield conn
        except psycopg.Error as exc:
            if _is_contention(exc):
                raise StorageBusyError("PostgreSQL operation contended or timed out") from None
            raise RuntimeError("PostgreSQL storage operation failed") from None

    @contextmanager
    def transaction(
        self,
        workspace_id: str,
        *,
        write: bool = True,
        connect_timeout_s: int | None = None,
        statement_timeout_ms: int | None = None,
        lock_timeout_ms: int | None = None,
    ) -> Iterator[PostgresUnitOfWork]:
        with self._connection(
            read_only=not write,
            connect_timeout_s=connect_timeout_s,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        ) as conn:
            if write:
                row = conn.execute(
                    "SELECT id FROM workspaces WHERE id = %s FOR UPDATE", (workspace_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("Workspace not found")
            yield PostgresUnitOfWork(conn, workspace_id)

    def create_workspace(self, workspace_id: str, equipment: Sequence[Equipment]) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO workspaces (id, created_at) VALUES (%s, %s)",
                (workspace_id, _ts(datetime.now(UTC))),
            )
            for item in equipment:
                conn.execute(
                    "INSERT INTO equipment (id, workspace_id, label, kind, state, version)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        item.id,
                        workspace_id,
                        item.label,
                        item.kind.value,
                        item.state.value,
                        item.version,
                    ),
                )

    def create_session(self, session: Session) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO sessions (id, workspace_id, created_at, expires_at)"
                " VALUES (%s, %s, %s, %s)",
                (
                    session.id,
                    session.workspace_id,
                    _ts(session.created_at),
                    _ts(session.expires_at),
                ),
            )

    def get_session(self, session_id: str) -> Session | None:
        with self._connection(read_only=True) as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = %s", (session_id,)).fetchone()
            if row is None:
                return None
            return Session(
                str(row["id"]),
                str(row["workspace_id"]),
                _out(row["created_at"]),
                _out(row["expires_at"]),
            )

    def due_task_candidates(self, now: datetime, limit: int) -> list[DueTaskRef]:
        if not 0 <= limit <= 100:
            raise ValueError("Candidate limit must be between 0 and 100")
        with self._connection(read_only=True) as conn:
            rows = conn.execute(
                "SELECT workspace_id, id FROM tasks WHERE status = 'PENDING' AND due_at <= %s"
                " ORDER BY due_at, id LIMIT %s",
                (_ts(now), limit),
            ).fetchall()
            return [DueTaskRef(str(row["workspace_id"]), str(row["id"])) for row in rows]
