"""Hosted task scheduler service and execution evidence for PostgreSQL.

Operates as a callable service taking an explicit database URL and standard
clock/id seams. Reuses application.process_due_tasks with a bounded limit
(at most 100) and StopSignal, governed by a 20-second monotonic processing
budget.

State is kept in the singleton `scheduler_control` table (schema V2).
Admission and finalization are short atomic transactions; no transaction spans
the entire tick. Expired leases (60s) can be superseded; stale tokens cannot
overwrite newer evidence.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row

from borrowed_steps.application.coordination import StopSignal, TickReport, process_due_tasks
from borrowed_steps.application.ports import Clock, IdGenerator
from borrowed_steps.infrastructure.postgres_migrations import (
    EXPECTED_SCHEMA_VERSION,
    SchemaVersionError,
    require_transport_security,
)
from borrowed_steps.infrastructure.postgres_store import PostgresStore
from borrowed_steps.infrastructure.system import SecretsIdGenerator, SystemClock
from borrowed_steps.isotime import to_iso

__all__ = [
    "DEFAULT_CANDIDATE_LIMIT",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_PROCESSING_BUDGET_SECONDS",
    "STALE_WARNING_THRESHOLD",
    "HostedSchedulerService",
    "MonotonicDeadlineStopSignal",
    "SchedulerStatus",
    "TickResult",
]

_LOGGER = logging.getLogger("borrowed_steps.scheduler")

DEFAULT_PROCESSING_BUDGET_SECONDS: float = 20.0
DEFAULT_LEASE_SECONDS: float = 60.0
DEFAULT_CANDIDATE_LIMIT: int = 100
STALE_WARNING_THRESHOLD: timedelta = timedelta(minutes=30)

_CONNECT_TIMEOUT_S = 2
_STATEMENT_TIMEOUT_MS = 500
_LOCK_TIMEOUT_MS = 250


class MonotonicDeadlineStopSignal:
    """Combines a monotonic clock deadline with an optional external StopSignal."""

    __slots__ = ("_deadline", "_external")

    def __init__(self, deadline: float, external: StopSignal | None = None) -> None:
        self._deadline = deadline
        self._external = external

    def is_set(self) -> bool:
        if time.monotonic() >= self._deadline:
            return True
        return self._external.is_set() if self._external is not None else False


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    """Read-only persisted counts and timestamps. Carries no secrets or entity data."""

    status: str
    """One of: 'never_run', 'running', 'success', 'partial', 'failed', 'expired'."""
    last_outcome: str
    """Last recorded completed outcome: 'never_run', 'success', 'partial', 'failed'."""
    last_run_id: str | None
    last_completed_at: datetime | None
    last_success_at: datetime | None
    last_success_recent: bool
    """True if last_success_at is within 30 minutes of current DB time."""
    stale_warning: bool
    """True if last_success_recent is False."""
    active_run_id: str | None
    active_run_started_at: datetime | None
    active_run_expires_at: datetime | None
    last_considered: int
    last_marked_due: int
    last_resolved_stale: int
    last_unchanged: int
    last_contended: int
    last_stopped_early: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "last_outcome": self.last_outcome,
            "last_completed_at": to_iso(self.last_completed_at) if self.last_completed_at else None,
            "last_success_at": to_iso(self.last_success_at) if self.last_success_at else None,
            "last_success_recent": self.last_success_recent,
            "stale_warning": self.stale_warning,
            "active_run_started_at": to_iso(self.active_run_started_at)
            if self.active_run_started_at
            else None,
            "active_run_expires_at": to_iso(self.active_run_expires_at)
            if self.active_run_expires_at
            else None,
            "counts_complete": self.last_outcome != "failed",
            "counts": {
                "considered": self.last_considered,
                "marked_due": self.last_marked_due,
                "resolved_stale": self.last_resolved_stale,
                "unchanged": self.last_unchanged,
                "contended": self.last_contended,
                "stopped_early": self.last_stopped_early,
            }
            if self.last_outcome != "failed"
            else None,
        }


@dataclass(frozen=True, slots=True)
class TickResult:
    """Outcome of one tick execution attempt."""

    outcome: str
    """'success', 'partial', 'busy', 'failed', 'stale_lease'."""
    run_id: str | None
    report: TickReport | None
    capacity_limited: bool
    error: str | None = None


class HostedSchedulerService:
    """Callable hosted task scheduler service.

    No network/database I/O on construction. Readiness is explicit via check_schema().
    """

    def __init__(
        self,
        database_url: str,
        *,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        store: PostgresStore | None = None,
        connect_timeout_s: int = _CONNECT_TIMEOUT_S,
        statement_timeout_ms: int = _STATEMENT_TIMEOUT_MS,
        lock_timeout_ms: int = _LOCK_TIMEOUT_MS,
    ) -> None:
        require_transport_security(database_url)
        for value, ceiling in (
            (connect_timeout_s, _CONNECT_TIMEOUT_S),
            (statement_timeout_ms, _STATEMENT_TIMEOUT_MS),
            (lock_timeout_ms, _LOCK_TIMEOUT_MS),
        ):
            if type(value) is not int or not 0 < value <= ceiling:
                raise ValueError("Scheduler timeout must be positive and within its fixed ceiling")
        self._database_url = database_url
        self._clock = clock if clock is not None else SystemClock()
        self._ids = ids if ids is not None else SecretsIdGenerator()
        self._connect_timeout_s = connect_timeout_s
        self._statement_timeout_ms = statement_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms
        if store is not None and (
            store._database_url != database_url
            or store._connect_timeout_s != connect_timeout_s
            or store._statement_timeout_ms != statement_timeout_ms
            or store._lock_timeout_ms != lock_timeout_ms
        ):
            raise ValueError(
                "Injected scheduler store must use the same database and bounded timeouts"
            )
        self._store = (
            store
            if store is not None
            else PostgresStore(
                database_url,
                connect_timeout_s=connect_timeout_s,
                statement_timeout_ms=statement_timeout_ms,
                lock_timeout_ms=lock_timeout_ms,
            )
        )

    def _connect(self) -> psycopg.Connection[DictRow]:
        try:
            return self._open_connection()
        except psycopg.Error:
            raise RuntimeError("Scheduler database connection failed") from None

    def _open_connection(self) -> psycopg.Connection[DictRow]:
        return psycopg.connect(
            self._database_url,
            autocommit=False,
            connect_timeout=self._connect_timeout_s,
            prepare_threshold=None,
            row_factory=dict_row,
            options=(
                f"-c statement_timeout={self._statement_timeout_ms} "
                f"-c lock_timeout={self._lock_timeout_ms}"
            ),
        )

    def _apply_timeouts(
        self,
        conn: psycopg.Connection[DictRow],
        *,
        read_only: bool = False,
    ) -> None:
        if read_only:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute(f"SET LOCAL lock_timeout = '{self._lock_timeout_ms}ms'")
        conn.execute(f"SET LOCAL statement_timeout = '{self._statement_timeout_ms}ms'")

    def check_schema(self) -> None:
        """Read-only schema readiness gate. Requires schema version 2."""
        try:
            with self._connect() as conn:
                self._apply_timeouts(conn, read_only=True)
                present = conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()
                version = 0
                if present is not None and present["to_regclass"] is not None:
                    row = conn.execute(
                        "SELECT MAX(version) AS version FROM schema_migrations"
                    ).fetchone()
                    version = int(row["version"] or 0) if row else 0
        except psycopg.Error:
            raise RuntimeError("Scheduler schema readiness failed") from None
        if version != EXPECTED_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"PostgreSQL schema version {version}, expected {EXPECTED_SCHEMA_VERSION}."
            )

    def _admit(
        self,
        conn: psycopg.Connection[DictRow],
        run_id: str,
        lease_seconds: float,
    ) -> bool:
        """Attempt to admit a run. Returns True if admitted, False if busy.

        Runs inside its own short transaction with lock_timeout and statement_timeout.
        """
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, active_run_id, active_run_expires_at, clock_timestamp() as db_now "
            "FROM scheduler_control WHERE id = 1 FOR UPDATE"
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Scheduler control row is missing")

        active_id = row["active_run_id"]
        expires_at = row["active_run_expires_at"]
        db_now = row["db_now"]

        if active_id is not None and expires_at is not None and db_now < expires_at:
            return False

        cursor.execute(
            "UPDATE scheduler_control SET "
            "active_run_id = %s, "
            "active_run_started_at = %s, "
            "active_run_expires_at = %s + make_interval(secs => %s) "
            "WHERE id = 1",
            (run_id, db_now, db_now, lease_seconds),
        )
        return True

    def _finalize(
        self,
        conn: psycopg.Connection[DictRow],
        run_id: str,
        outcome: str,
        report: TickReport | None,
    ) -> bool:
        """Conditionally finalize the run if active_run_id still matches run_id.

        Returns True if successfully finalized, False if stale (superseded).
        """
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, active_run_id, clock_timestamp() as db_now "
            "FROM scheduler_control WHERE id = 1 FOR UPDATE"
        )
        row = cursor.fetchone()
        if row is None or row["active_run_id"] != run_id:
            return False

        db_now = row["db_now"]
        considered = report.considered if report is not None else 0
        marked_due = report.marked_due if report is not None else 0
        resolved_stale = report.resolved_stale if report is not None else 0
        unchanged = report.unchanged if report is not None else 0
        contended = report.contended if report is not None else 0
        stopped_early = report.stopped_early if report is not None else False

        if outcome == "success":
            cursor.execute(
                "UPDATE scheduler_control SET "
                "active_run_id = NULL, "
                "active_run_started_at = NULL, "
                "active_run_expires_at = NULL, "
                "last_run_id = %s, "
                "last_outcome = %s, "
                "last_completed_at = %s, "
                "last_success_at = %s, "
                "last_considered = %s, "
                "last_marked_due = %s, "
                "last_resolved_stale = %s, "
                "last_unchanged = %s, "
                "last_contended = %s, "
                "last_stopped_early = %s "
                "WHERE id = 1 AND active_run_id = %s",
                (
                    run_id,
                    outcome,
                    db_now,
                    db_now,
                    considered,
                    marked_due,
                    resolved_stale,
                    unchanged,
                    contended,
                    stopped_early,
                    run_id,
                ),
            )
        else:
            # Preserve prior last_success_at
            cursor.execute(
                "UPDATE scheduler_control SET "
                "active_run_id = NULL, "
                "active_run_started_at = NULL, "
                "active_run_expires_at = NULL, "
                "last_run_id = %s, "
                "last_outcome = %s, "
                "last_completed_at = %s, "
                "last_considered = %s, "
                "last_marked_due = %s, "
                "last_resolved_stale = %s, "
                "last_unchanged = %s, "
                "last_contended = %s, "
                "last_stopped_early = %s "
                "WHERE id = 1 AND active_run_id = %s",
                (
                    run_id,
                    outcome,
                    db_now,
                    considered,
                    marked_due,
                    resolved_stale,
                    unchanged,
                    contended,
                    stopped_early,
                    run_id,
                ),
            )
        return cursor.rowcount > 0

    def tick(
        self,
        *,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
        processing_budget_seconds: float = DEFAULT_PROCESSING_BUDGET_SECONDS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        stop: StopSignal | None = None,
    ) -> TickResult:
        """Execute one bounded scheduler tick.

        Adopts a unique run token, runs process_due_tasks outside any lock on
        scheduler_control, and conditionally finalizes the outcome.
        """
        if not 0 <= limit <= 100:
            msg = "Candidate limit must be between 0 and 100"
            raise ValueError(msg)
        if not math.isfinite(processing_budget_seconds) or not 0 <= processing_budget_seconds <= 20:
            raise ValueError("Processing budget must be finite and between zero and20 seconds")
        if lease_seconds != DEFAULT_LEASE_SECONDS:
            raise ValueError("Scheduler lease must remain60 seconds")

        self.check_schema()
        run_id = self._ids.new_id()

        # Step 1: Admission in short atomic transaction
        try:
            with self._connect() as conn:
                self._apply_timeouts(conn)
                admitted = self._admit(conn, run_id, lease_seconds)
        except psycopg.Error as exc:
            contended = isinstance(
                exc, (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled)
            )
            _LOGGER.warning("Scheduler admission failed")
            return TickResult(
                outcome="busy" if contended else "failed",
                run_id=None,
                report=None,
                capacity_limited=False,
                error="Scheduler admission unavailable",
            )

        if not admitted:
            return TickResult(
                outcome="busy",
                run_id=None,
                report=None,
                capacity_limited=False,
            )

        # Step 2: Task processing outside any control row transaction
        deadline = time.monotonic() + processing_budget_seconds
        stop_signal = MonotonicDeadlineStopSignal(deadline, stop)
        report: TickReport | None = None
        error: str | None = None

        try:
            report = process_due_tasks(
                self._store,
                self._clock,
                self._ids,
                limit=limit,
                stop=stop_signal,
            )
        except Exception:
            _LOGGER.error("Task processing failed; partial counts unavailable")
            error = "Task processing failed; partial counts unavailable"
        if report is not None and stop_signal.is_set():
            report = replace(report, stopped_early=True)

        # Determine outcome
        capacity_limited = bool(report is not None and report.considered >= limit)
        if error is not None:
            outcome = "failed"
        elif report is not None and (
            report.contended > 0 or report.stopped_early or capacity_limited
        ):
            outcome = "partial"
        else:
            outcome = "success"

        # Step 3: Finalization in short atomic transaction
        try:
            with self._connect() as conn:
                self._apply_timeouts(conn)
                finalized = self._finalize(conn, run_id, outcome, report)
        except Exception:
            _LOGGER.error("Failed to record scheduler outcome")
            msg = "Failed to persist scheduler execution outcome"
            raise RuntimeError(msg) from None

        if not finalized:
            return TickResult(
                outcome="stale_lease",
                run_id=run_id,
                report=report,
                capacity_limited=capacity_limited,
                error="Lease was superseded before finalization",
            )

        return TickResult(
            outcome=outcome,
            run_id=run_id,
            report=report,
            capacity_limited=capacity_limited,
            error=error,
        )

    def __call__(
        self,
        *,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
        processing_budget_seconds: float = DEFAULT_PROCESSING_BUDGET_SECONDS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        stop: StopSignal | None = None,
    ) -> TickResult:
        """Call instance directly as a tick service."""
        return self.tick(
            limit=limit,
            processing_budget_seconds=processing_budget_seconds,
            lease_seconds=lease_seconds,
            stop=stop,
        )

    def read_status(self) -> SchedulerStatus:
        """Read the persisted counts and timestamps. Never runs tasks."""
        try:
            return self._read_status()
        except psycopg.Error:
            raise RuntimeError("Scheduler status read failed") from None

    def _read_status(self) -> SchedulerStatus:
        self.check_schema()
        with self._connect() as conn:
            self._apply_timeouts(conn, read_only=True)
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT "
                    "id, active_run_id, active_run_started_at, active_run_expires_at, "
                    "last_run_id, last_outcome, last_completed_at, last_success_at, "
                    "last_considered, last_marked_due, last_resolved_stale, last_unchanged, "
                    "last_contended, last_stopped_early, clock_timestamp() as db_now "
                    "FROM scheduler_control WHERE id = 1"
                )
                row = cursor.fetchone()
                if row is None:
                    return SchedulerStatus(
                        status="never_run",
                        last_outcome="never_run",
                        last_run_id=None,
                        last_completed_at=None,
                        last_success_at=None,
                        last_success_recent=False,
                        stale_warning=True,
                        active_run_id=None,
                        active_run_started_at=None,
                        active_run_expires_at=None,
                        last_considered=0,
                        last_marked_due=0,
                        last_resolved_stale=0,
                        last_unchanged=0,
                        last_contended=0,
                        last_stopped_early=False,
                    )

                active_id = row["active_run_id"]
                expires_at = row["active_run_expires_at"]
                db_now = row["db_now"]
                last_outcome = str(row["last_outcome"])
                last_success = row["last_success_at"]

                # Determine status
                if active_id is not None and expires_at is not None:
                    status = "running" if db_now < expires_at else "expired"
                else:
                    status = last_outcome

                is_recent = bool(
                    last_success is not None
                    and timedelta(0) <= (db_now - last_success) <= STALE_WARNING_THRESHOLD
                )
                stale_warning = not is_recent

                return SchedulerStatus(
                    status=status,
                    last_outcome=last_outcome,
                    last_run_id=row["last_run_id"],
                    last_completed_at=row["last_completed_at"],
                    last_success_at=last_success,
                    last_success_recent=is_recent,
                    stale_warning=stale_warning,
                    active_run_id=active_id,
                    active_run_started_at=row["active_run_started_at"],
                    active_run_expires_at=expires_at,
                    last_considered=int(row["last_considered"]),
                    last_marked_due=int(row["last_marked_due"]),
                    last_resolved_stale=int(row["last_resolved_stale"]),
                    last_unchanged=int(row["last_unchanged"]),
                    last_contended=int(row["last_contended"]),
                    last_stopped_early=bool(row["last_stopped_early"]),
                )
