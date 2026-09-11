"""Operator-only one-use canary receipts; not public quota admission.

Trusted caller authenticates the operator and checks source/plan hashes.
Unknown measured usage stays NULL. Reservations are retained without refunds.
No provider calls, credential discovery or implicit migration.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

import psycopg
from psycopg.rows import DictRow, dict_row

from borrowed_steps.infrastructure.postgres_migrations import require_transport_security

ACCEPTED_PLAN_HASH = "38ec48176db21d7f947cfdc1ae1b3b211efdbdb55c976043462aea85a16a3031"
RESERVED_SENDS = 6
RESERVED_OUTPUT_TOKENS = 6144  # Six output caps, NOT an input/total-token bound.
_LOCK = 8_801_103_321_021


class CanaryReceiptError(ValueError):
    """Generic refusal without caller data or SQL detail."""


class CanaryState(StrEnum):
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    SUCCEEDED = "SUCCEEDED"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    UNCERTAIN = "UNCERTAIN"


class FailureCode(StrEnum):
    PROVIDER_FAILURE = "provider_failure"
    INVALID_OUTPUT = "invalid_output"
    CANCELLED = "cancelled"
    EXECUTION_UNKNOWN = "execution_unknown"


@dataclass(frozen=True)
class CanaryReservationRequest:
    receipt_id: str  # Caller creates UUID4 once and retains for replay.
    owner_id: str  # Execution identity, not an authentication secret.
    authorization_id: str  # Operator-issued, one-use UUID4.
    authorization_expires_at: datetime
    live_authorized: bool = False
    plan_hash: str = ACCEPTED_PLAN_HASH
    provider: str = "groq"
    model: str = "openai/gpt-oss-20b"
    reserved_sends: int = RESERVED_SENDS
    reserved_output_tokens: int = RESERVED_OUTPUT_TOKENS


def _uuid(value: str) -> None:
    try:
        parsed = UUID(value)
        valid = parsed.version == 4 and str(parsed) == value
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise CanaryReceiptError("Invalid execution identity")


def request_payload(request: CanaryReservationRequest) -> str:
    """Validate identity without preventing an expired read-only replay."""
    for value in (request.receipt_id, request.owner_id, request.authorization_id):
        _uuid(value)
    expiry = request.authorization_expires_at
    if not isinstance(expiry, datetime) or expiry.tzinfo is None or expiry.utcoffset() is None:
        raise CanaryReceiptError("Explicit authorization expiry required")
    if request.live_authorized is not True:
        raise CanaryReceiptError("Explicit operator authorization required")
    if (request.plan_hash, request.provider, request.model) != (
        ACCEPTED_PLAN_HASH,
        "groq",
        "openai/gpt-oss-20b",
    ):
        raise CanaryReceiptError("Candidate mismatch")
    if (
        type(request.reserved_sends) is not int
        or request.reserved_sends != RESERVED_SENDS
        or type(request.reserved_output_tokens) is not int
        or request.reserved_output_tokens != RESERVED_OUTPUT_TOKENS
    ):
        raise CanaryReceiptError("Reservation mismatch")
    payload = asdict(request)
    payload["authorization_expires_at"] = expiry.astimezone(UTC).isoformat()
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class CanaryReceiptStore:
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
                    # ponytail: serialize operator receipts; split by org only if needed.
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
                else:
                    conn.execute("SET TRANSACTION READ ONLY")
                yield conn
        except psycopg.Error:
            raise CanaryReceiptError(
                "Canary storage unavailable; outcome may be uncertain"
            ) from None

    @staticmethod
    def _owned(conn: psycopg.Connection[DictRow], receipt_id: str, owner_id: str) -> DictRow:
        _uuid(receipt_id)
        _uuid(owner_id)
        row = conn.execute(
            "SELECT * FROM canary_receipts WHERE receipt_id=%s AND owner_id=%s FOR UPDATE",
            (receipt_id, owner_id),
        ).fetchone()
        if row is None:
            raise CanaryReceiptError("Receipt unavailable to this execution")
        return row

    @staticmethod
    def _now(conn: psycopg.Connection[DictRow]) -> datetime:
        row = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        assert row is not None
        result: datetime = row["now"]
        return result

    def reserve(self, request: CanaryReservationRequest) -> DictRow:
        payload = request_payload(request)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM canary_receipts WHERE receipt_id=%s", (request.receipt_id,)
            ).fetchone()
            if row is not None:
                if row["payload"] != payload:
                    raise CanaryReceiptError("Receipt replay conflict")
                return row  # Read only, even after expiry; not permission to send.
            if request.authorization_expires_at <= self._now(conn):
                raise CanaryReceiptError("Authorization expired")
            if conn.execute(
                "SELECT 1 FROM canary_receipts WHERE authorization_id=%s",
                (request.authorization_id,),
            ).fetchone():
                raise CanaryReceiptError("Authorization already consumed")
            if conn.execute("SELECT 1 FROM canary_receipts WHERE concurrency_active").fetchone():
                raise CanaryReceiptError("Another execution is unresolved")
            row = conn.execute(
                """INSERT INTO canary_receipts (
                    receipt_id, owner_id, authorization_id, authorization_expires_at,
                    plan_hash, payload, payload_hash, reserved_sends, reserved_output_tokens
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (
                    request.receipt_id,
                    request.owner_id,
                    request.authorization_id,
                    request.authorization_expires_at,
                    request.plan_hash,
                    payload,
                    digest,
                    request.reserved_sends,
                    request.reserved_output_tokens,
                ),
            ).fetchone()
            assert row is not None
            return row

    def mark_dispatched(self, receipt_id: str, owner_id: str) -> DictRow:
        with self._transaction() as conn:
            row = self._owned(conn, receipt_id, owner_id)
            if row["state"] != CanaryState.RESERVED:
                raise CanaryReceiptError("Dispatch already consumed or receipt terminal")
            if row["authorization_expires_at"] <= self._now(conn):
                raise CanaryReceiptError("Authorization expired")
            updated = conn.execute(
                """UPDATE canary_receipts SET state='DISPATCHED',
                dispatched_at=clock_timestamp() WHERE receipt_id=%s RETURNING *""",
                (receipt_id,),
            ).fetchone()
            assert updated is not None
            return updated  # Commit must succeed before caller receives permission.

    def finish(
        self,
        receipt_id: str,
        owner_id: str,
        state: CanaryState,
        *,
        actual_sends: int | None = None,
        actual_total_tokens: int | None = None,
        failure_code: FailureCode | None = None,
    ) -> DictRow:
        if not isinstance(state, CanaryState) or state not in (
            CanaryState.SUCCEEDED,
            CanaryState.FAILED_CONFIRMED,
            CanaryState.UNCERTAIN,
        ):
            raise CanaryReceiptError("Invalid terminal state")
        if failure_code is not None and not isinstance(failure_code, FailureCode):
            raise CanaryReceiptError("Invalid failure category")
        for value in (actual_sends, actual_total_tokens):
            if value is not None and (type(value) is not int or value < 0 or value > 2147483647):
                raise CanaryReceiptError("Invalid measured usage")
        if actual_sends is not None and actual_sends > RESERVED_SENDS:
            raise CanaryReceiptError("Invalid measured sends")
        if state == CanaryState.SUCCEEDED:
            if actual_sends is None or actual_sends == 0 or failure_code is not None:
                raise CanaryReceiptError("Success requires positive send evidence")
        elif failure_code is None:
            raise CanaryReceiptError("Failure category required")
        with self._transaction() as conn:
            row = self._owned(conn, receipt_id, owner_id)
            evidence = (state, actual_sends, actual_total_tokens, failure_code)
            if row["state"] == state:
                previous = (
                    row["state"],
                    row["actual_sends"],
                    row["actual_total_tokens"],
                    row["failure_code"],
                )
                if previous != evidence:
                    raise CanaryReceiptError("Terminal evidence replay conflict")
                return row
            if row["state"] != CanaryState.DISPATCHED:
                raise CanaryReceiptError("Illegal receipt transition")
            updated = conn.execute(
                """UPDATE canary_receipts SET state=%s, actual_sends=%s,
                actual_total_tokens=%s, failure_code=%s, concurrency_active=%s,
                completed_at=clock_timestamp() WHERE receipt_id=%s RETURNING *""",
                (*evidence, state == CanaryState.UNCERTAIN, receipt_id),
            ).fetchone()
            assert updated is not None
            return updated

    def recover_dead_execution(
        self,
        receipt_id: str,
        owner_id: str,
        *,
        operator_id: str,
        confirmed_dead: bool = False,
    ) -> DictRow:
        _uuid(operator_id)
        if confirmed_dead is not True:
            raise CanaryReceiptError("Explicit confirmation of terminated execution required")
        with self._transaction() as conn:
            row = self._owned(conn, receipt_id, owner_id)
            if row["recovered_at"] is not None:
                if row["recovered_by"] != operator_id:
                    raise CanaryReceiptError("Recovery replay conflict")
                return row
            if row["state"] not in (
                CanaryState.RESERVED,
                CanaryState.DISPATCHED,
                CanaryState.UNCERTAIN,
            ):
                raise CanaryReceiptError("Receipt is already settled")
            # Covers crashes before/after dispatch. Never expiry-based recovery.
            updated = conn.execute(
                """UPDATE canary_receipts SET state='UNCERTAIN', concurrency_active=FALSE,
                recovered_at=clock_timestamp(), recovered_by=%s
                WHERE receipt_id=%s RETURNING *""",
                (operator_id, receipt_id),
            ).fetchone()
            assert updated is not None
            return updated

    def get_receipt(self, receipt_id: str) -> DictRow | None:
        _uuid(receipt_id)
        with self._transaction(write=False) as conn:
            return conn.execute(
                "SELECT * FROM canary_receipts WHERE receipt_id=%s", (receipt_id,)
            ).fetchone()
