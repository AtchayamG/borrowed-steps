"""Application request control across instances in PostgreSQL.

Enforces single active operation, fixed reservation accounting, rolling rate
ceilings, and a durable 15-minute provider 429 cooldown.
No provider calls, credential discovery or implicit migrations.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

import psycopg
from psycopg.rows import DictRow, dict_row

from borrowed_steps.infrastructure.postgres_migrations import require_transport_security

RESERVED_SENDS = 6
GLOBAL_LIMIT_60S = 6
GLOBAL_LIMIT_24H = 120
WORKSPACE_LIMIT_24H = 24
STORED_DEADLINE_SECONDS = 120
PROVIDER_429_COOLDOWN_SECONDS = 900
_ADMISSION_LOCK = 8_801_103_331_031


class InferenceAdmissionError(ValueError):
    """Base exception for inference admission errors."""


class AdmissionRefusedError(InferenceAdmissionError):
    """Admission or transition refused due to policy, quota, timeout, or state."""


class AdmissionReplayConflictError(InferenceAdmissionError):
    """Replay conflict due to changed parameters or evidence."""


class AdmissionUnavailableError(InferenceAdmissionError):
    """Database unavailable or connection failed."""


class AdmissionState(StrEnum):
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    SUCCEEDED = "SUCCEEDED"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    UNCERTAIN = "UNCERTAIN"
    RECOVERED = "RECOVERED"


class AdmissionFailureCode(StrEnum):
    PROVIDER_429 = "provider_429"
    PROVIDER_FAILURE = "provider_failure"
    INVALID_OUTPUT = "invalid_output"
    CANCELLED = "cancelled"
    DEADLINE_EXPIRED = "deadline_expired"
    EXECUTION_UNKNOWN = "execution_unknown"


class RecoveryReason(StrEnum):
    PROCESS_DEAD = "process_dead"
    POD_EVICTED = "pod_evicted"
    OPERATOR_RESET = "operator_reset"
    MANUAL_INTERVENTION = "manual_intervention"
    TIMEOUT_TERMINATED = "timeout_terminated"


@dataclass(frozen=True)
class AdmissionReservationRequest:
    reservation_id: str
    workspace_id: str
    owner_id: str
    request_key_hash: str
    payload_hash: str
    reserved_sends: int = RESERVED_SENDS


def _uuid(value: str) -> None:
    if type(value) is not str:
        raise InferenceAdmissionError("Invalid execution identity")
    try:
        parsed = UUID(value)
        valid = parsed.version == 4 and str(parsed) == value
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise InferenceAdmissionError("Invalid execution identity")


def _is_hex64(value: str) -> bool:
    if type(value) is not str or len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)


def validate_workspace_id(value: str) -> None:
    """Accept real application IDs and existing UUID4 workspace fixtures.

    The database foreign key still requires an actual workspace. Execution and
    operator IDs remain UUID4; workspace IDs must not be rewritten to match them.
    """
    if type(value) is str and len(value) == 24 and all(c in "0123456789abcdef" for c in value):
        return
    _uuid(value)


class InferenceAdmissionStore:
    """Short serialized SQL transactions; no transaction spans inference."""

    def __init__(self, database_url: str) -> None:
        require_transport_security(database_url)
        self._url = database_url

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with psycopg.connect(
                self._url,
                row_factory=dict_row,
                connect_timeout=5,
                prepare_threshold=None,
                options="-c statement_timeout=5000 -c lock_timeout=2000",
            ) as conn:
                if write:
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMISSION_LOCK,))
                else:
                    conn.execute("SET TRANSACTION READ ONLY")
                yield conn
        except psycopg.Error:
            raise AdmissionUnavailableError(
                "Admission storage unavailable; outcome may be uncertain"
            ) from None

    @staticmethod
    def _now(conn: psycopg.Connection[DictRow]) -> datetime:
        row = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        assert row is not None
        result: datetime = row["now"]
        return result

    def reserve(self, request: AdmissionReservationRequest) -> DictRow:
        _uuid(request.reservation_id)
        validate_workspace_id(request.workspace_id)
        _uuid(request.owner_id)
        if not _is_hex64(request.request_key_hash):
            raise InferenceAdmissionError("Invalid request key hash")
        if not _is_hex64(request.payload_hash):
            raise InferenceAdmissionError("Invalid payload hash")
        if type(request.reserved_sends) is not int or request.reserved_sends != RESERVED_SENDS:
            raise InferenceAdmissionError("Reservation mismatch")

        with self._transaction() as conn:
            # Check for existing reservation by ID
            row_by_id = conn.execute(
                "SELECT * FROM inference_admissions WHERE reservation_id = %s FOR UPDATE",
                (request.reservation_id,),
            ).fetchone()

            # Check for existing reservation by (workspace_id, request_key_hash)
            row_by_key = conn.execute(
                """SELECT * FROM inference_admissions
                WHERE workspace_id = %s AND request_key_hash = %s FOR UPDATE""",
                (request.workspace_id, request.request_key_hash),
            ).fetchone()

            if row_by_id is not None:
                matches = (
                    row_by_id["workspace_id"] == request.workspace_id
                    and row_by_id["owner_id"] == request.owner_id
                    and row_by_id["request_key_hash"] == request.request_key_hash
                    and row_by_id["payload_hash"] == request.payload_hash
                )
                if matches:
                    return row_by_id  # Exact replay: read only, does not grant dispatch permission.
                raise AdmissionReplayConflictError("Reservation identity replay conflict")

            if row_by_key is not None:
                raise AdmissionReplayConflictError(
                    "Request key already exists with different reservation ID"
                )

            # Check provider 429 cooldown (15 minutes durable cooldown across database)
            cooldown_row = conn.execute(
                """SELECT 1 FROM inference_admissions
                WHERE failure_code = %s
                  AND completed_at > clock_timestamp() - (%s * INTERVAL '1 second')
                LIMIT 1""",
                (AdmissionFailureCode.PROVIDER_429.value, PROVIDER_429_COOLDOWN_SECONDS),
            ).fetchone()
            if cooldown_row is not None:
                raise AdmissionRefusedError("Provider 429 cooldown active")

            # Check single active operation across the database
            active_row = conn.execute(
                "SELECT 1 FROM inference_admissions WHERE is_active LIMIT 1"
            ).fetchone()
            if active_row is not None:
                raise AdmissionRefusedError("Another operation is active")

            # Check global rolling 60-second limit (cap 6 sends)
            g60_row = conn.execute(
                """SELECT COALESCE(SUM(reserved_sends), 0) AS total FROM inference_admissions
                WHERE is_active OR released_at > clock_timestamp() - (%s * INTERVAL '1 second')""",
                (60,),
            ).fetchone()
            assert g60_row is not None
            if g60_row["total"] + request.reserved_sends > GLOBAL_LIMIT_60S:
                raise AdmissionRefusedError("Global 60-second reservation limit exceeded")

            # Check global rolling 24-hour limit (cap 120 sends)
            g24h_row = conn.execute(
                """SELECT COALESCE(SUM(reserved_sends), 0) AS total FROM inference_admissions
                WHERE is_active OR released_at > clock_timestamp() - INTERVAL '24 hours'"""
            ).fetchone()
            assert g24h_row is not None
            if g24h_row["total"] + request.reserved_sends > GLOBAL_LIMIT_24H:
                raise AdmissionRefusedError("Global 24-hour reservation limit exceeded")

            # Check per-workspace rolling 24-hour limit (cap 24 sends)
            w24h_row = conn.execute(
                """SELECT COALESCE(SUM(reserved_sends), 0) AS total FROM inference_admissions
                WHERE workspace_id = %s
                  AND (is_active OR released_at > clock_timestamp() - INTERVAL '24 hours')""",
                (request.workspace_id,),
            ).fetchone()
            assert w24h_row is not None
            if w24h_row["total"] + request.reserved_sends > WORKSPACE_LIMIT_24H:
                raise AdmissionRefusedError("Workspace 24-hour reservation limit exceeded")

            # Insert new reservation
            inserted = conn.execute(
                """INSERT INTO inference_admissions (
                    reservation_id,
                    workspace_id,
                    owner_id,
                    request_key_hash,
                    payload_hash,
                    reserved_sends,
                    deadline_at,
                    state,
                    is_active
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    clock_timestamp() + (%s * INTERVAL '1 second'),
                    'RESERVED',
                    TRUE
                ) RETURNING *""",
                (
                    request.reservation_id,
                    request.workspace_id,
                    request.owner_id,
                    request.request_key_hash,
                    request.payload_hash,
                    request.reserved_sends,
                    STORED_DEADLINE_SECONDS,
                ),
            ).fetchone()
            assert inserted is not None
            return inserted

    def mark_dispatched(self, reservation_id: str, owner_id: str) -> DictRow:
        _uuid(reservation_id)
        _uuid(owner_id)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM inference_admissions WHERE reservation_id = %s FOR UPDATE",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise AdmissionRefusedError("Reservation unavailable to this execution")
            if row["owner_id"] != owner_id:
                raise AdmissionRefusedError("Reservation unavailable to this execution")
            if row["state"] != AdmissionState.RESERVED.value:
                raise AdmissionRefusedError("Dispatch already consumed or reservation terminal")
            if row["deadline_at"] <= self._now(conn):
                raise AdmissionRefusedError("Reservation deadline expired")

            updated = conn.execute(
                """UPDATE inference_admissions SET
                    state = 'DISPATCHED',
                    dispatched_at = clock_timestamp()
                WHERE reservation_id = %s
                RETURNING *""",
                (reservation_id,),
            ).fetchone()
            assert updated is not None
            return updated

    def finish(
        self,
        reservation_id: str,
        owner_id: str,
        state: AdmissionState,
        *,
        cleanup_completed: bool,
        actual_sends: int | None = None,
        actual_total_tokens: int | None = None,
        failure_code: AdmissionFailureCode | None = None,
    ) -> DictRow:
        _uuid(reservation_id)
        _uuid(owner_id)
        if type(cleanup_completed) is not bool:
            raise AdmissionRefusedError("Invalid cleanup_completed flag")

        valid_terminal_states = (
            AdmissionState.SUCCEEDED,
            AdmissionState.FAILED_CONFIRMED,
            AdmissionState.UNCERTAIN,
        )
        if state not in valid_terminal_states:
            raise AdmissionRefusedError("Invalid terminal state")

        if (
            state in (AdmissionState.SUCCEEDED, AdmissionState.FAILED_CONFIRMED)
            and cleanup_completed is not True
        ):
            raise AdmissionRefusedError("Confirmed terminal states require cleanup_completed=True")

        if actual_sends is not None and (
            type(actual_sends) is not int or actual_sends < 0 or actual_sends > RESERVED_SENDS
        ):
            raise AdmissionRefusedError("Invalid measured sends")

        if actual_total_tokens is not None and (
            type(actual_total_tokens) is not int
            or actual_total_tokens < 0
            or actual_total_tokens > 9_223_372_036_854_775_807
        ):
            raise AdmissionRefusedError("Invalid measured total tokens")

        failure_code_str: str | None = None
        if state == AdmissionState.SUCCEEDED:
            if failure_code is not None:
                raise AdmissionRefusedError("Success cannot have failure_code")
            if actual_sends is None or actual_sends == 0:
                raise AdmissionRefusedError("Success requires positive send evidence")
        else:
            if failure_code is None:
                raise AdmissionRefusedError("Failure code required for failed or uncertain state")
            try:
                code_enum = AdmissionFailureCode(failure_code)
            except ValueError:
                raise AdmissionRefusedError("Invalid failure code") from None
            failure_code_str = code_enum.value

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM inference_admissions WHERE reservation_id = %s FOR UPDATE",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise AdmissionRefusedError("Reservation unavailable to this execution")
            if row["owner_id"] != owner_id:
                raise AdmissionRefusedError("Reservation unavailable to this execution")

            state_str = state.value if isinstance(state, AdmissionState) else str(state)

            if row["state"] == state_str:
                same_sends = row["actual_sends"] == actual_sends
                same_tokens = row["actual_total_tokens"] == actual_total_tokens
                same_cleanup = row["cleanup_completed"] == cleanup_completed
                same_failure = row["failure_code"] == failure_code_str
                if same_sends and same_tokens and same_cleanup and same_failure:
                    return row  # Exact replay
                raise AdmissionReplayConflictError("Terminal evidence replay conflict")

            if row["state"] == AdmissionState.UNCERTAIN.value:
                raise AdmissionRefusedError("Transition from UNCERTAIN refuses")

            if row["state"] in (
                AdmissionState.SUCCEEDED.value,
                AdmissionState.FAILED_CONFIRMED.value,
                AdmissionState.RECOVERED.value,
            ):
                raise AdmissionRefusedError("Reservation is already terminal")

            if row["state"] != AdmissionState.DISPATCHED.value:
                raise AdmissionRefusedError("Illegal reservation transition")

            is_terminal = state != AdmissionState.UNCERTAIN
            is_active = not is_terminal

            updated = conn.execute(
                """UPDATE inference_admissions SET
                    state = %s,
                    is_active = %s,
                    actual_sends = %s,
                    actual_total_tokens = %s,
                    cleanup_completed = %s,
                    failure_code = %s,
                    completed_at = clock_timestamp(),
                    released_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END
                WHERE reservation_id = %s
                RETURNING *""",
                (
                    state_str,
                    is_active,
                    actual_sends,
                    actual_total_tokens,
                    cleanup_completed,
                    failure_code_str,
                    is_terminal,
                    reservation_id,
                ),
            ).fetchone()
            assert updated is not None
            return updated

    def recover_dead(
        self,
        reservation_id: str,
        owner_id: str | None = None,
        *,
        operator_id: str,
        reason: RecoveryReason,
        confirmed_dead: bool = False,
    ) -> DictRow:
        _uuid(reservation_id)
        if owner_id is not None:
            _uuid(owner_id)
        _uuid(operator_id)
        if type(confirmed_dead) is not bool or confirmed_dead is not True:
            raise AdmissionRefusedError("Explicit confirmation of terminated execution required")
        try:
            reason_enum = RecoveryReason(reason)
        except ValueError:
            raise AdmissionRefusedError("Invalid recovery reason") from None
        reason_str = reason_enum.value

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM inference_admissions WHERE reservation_id = %s FOR UPDATE",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise AdmissionRefusedError("Reservation not found")
            if owner_id is not None and row["owner_id"] != owner_id:
                raise AdmissionRefusedError("Owner mismatch")

            if row["state"] == AdmissionState.RECOVERED.value:
                if (
                    row["recovery_operator_id"] == operator_id
                    and row["recovery_reason"] == reason_str
                ):
                    return row  # Exact replay
                raise AdmissionReplayConflictError("Recovery replay conflict")

            if row["state"] in (
                AdmissionState.SUCCEEDED.value,
                AdmissionState.FAILED_CONFIRMED.value,
            ):
                raise AdmissionRefusedError("Reservation is already settled")

            if row["state"] not in (
                AdmissionState.RESERVED.value,
                AdmissionState.DISPATCHED.value,
                AdmissionState.UNCERTAIN.value,
            ):
                raise AdmissionRefusedError("Illegal recovery transition")

            updated = conn.execute(
                """UPDATE inference_admissions SET
                    state = 'RECOVERED',
                    is_active = FALSE,
                    released_at = clock_timestamp(),
                    recovered_at = clock_timestamp(),
                    recovery_operator_id = %s,
                    recovery_reason = %s,
                    completed_at = COALESCE(completed_at, clock_timestamp())
                WHERE reservation_id = %s
                RETURNING *""",
                (operator_id, reason_str, reservation_id),
            ).fetchone()
            assert updated is not None
            return updated

    def get(self, reservation_id: str) -> DictRow | None:
        _uuid(reservation_id)
        with self._transaction(write=False) as conn:
            return conn.execute(
                "SELECT * FROM inference_admissions WHERE reservation_id = %s",
                (reservation_id,),
            ).fetchone()

    def get_active(self) -> DictRow | None:
        with self._transaction(write=False) as conn:
            return conn.execute(
                "SELECT * FROM inference_admissions WHERE is_active LIMIT 1"
            ).fetchone()

    def get_by_request_key(self, workspace_id: str, request_key_hash: str) -> DictRow | None:
        validate_workspace_id(workspace_id)
        if not _is_hex64(request_key_hash):
            raise InferenceAdmissionError("Invalid request key hash")
        with self._transaction(write=False) as conn:
            return conn.execute(
                """SELECT * FROM inference_admissions
                WHERE workspace_id = %s AND request_key_hash = %s""",
                (workspace_id, request_key_hash),
            ).fetchone()
