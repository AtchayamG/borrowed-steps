"""Focused, comprehensive real PostgreSQL tests for the inference admission boundary.

Verifies:
- Single active operation invariant across workspaces
- Additive migration V4 and business data preservation
- Identity validation and exact vs conflicting replay
- Exactly one RESERVED -> DISPATCHED transition
- Expired deadline refuses dispatch and never auto-releases
- Terminal state evidence validation and replay checks
- UNCERTAIN stays active and blocks subsequent reservations
- Dead execution recovery lifecycle and fresh full-window debit retention
- Rolling rate caps: 60-second global, 24-hour global, 24-hour workspace
- Full debit retained despite fewer actual sends
- Provider 429 15-minute global cooldown
- PostgreSQL engine-level constraints (partial unique index, checks)
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from borrowed_steps.infrastructure.inference_admission import (
    RESERVED_SENDS,
    AdmissionFailureCode,
    AdmissionRefusedError,
    AdmissionReplayConflictError,
    AdmissionReservationRequest,
    AdmissionState,
    InferenceAdmissionError,
    InferenceAdmissionStore,
    RecoveryReason,
)
from borrowed_steps.infrastructure.postgres_migrations import (
    _SCHEMA_V1,
    _SCHEMA_V2,
    _SCHEMA_V3,
    apply_migrations,
)
from postgres_support import migrated_database


@pytest.fixture
def db() -> Iterator[str]:
    with migrated_database() as url:
        yield url


def _create_workspace(url: str, workspace_id: str | None = None) -> str:
    wid = workspace_id or str(uuid4())
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO workspaces (id, created_at) VALUES (%s, clock_timestamp())",
            (wid,),
        )
    return wid


def _sample_request(
    workspace_id: str,
    reservation_id: str | None = None,
    owner_id: str | None = None,
    seed: str = "sample",
) -> AdmissionReservationRequest:
    rid = reservation_id or str(uuid4())
    oid = owner_id or str(uuid4())
    req_hash = hashlib.sha256(f"req_{seed}_{rid}".encode()).hexdigest()
    pay_hash = hashlib.sha256(f"pay_{seed}_{rid}".encode()).hexdigest()
    return AdmissionReservationRequest(
        reservation_id=rid,
        workspace_id=workspace_id,
        owner_id=oid,
        request_key_hash=req_hash,
        payload_hash=pay_hash,
    )


def test_v4_migration_additive_and_preserves_business_data(db: str) -> None:
    """Additive migration lands on version 4, preserves all existing tables and data."""
    # Create an unmigrated database up to v3 only
    from postgres_support import disposable_database

    with disposable_database() as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE schema_migrations"
                " (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ)"
            )
            for statement in (*_SCHEMA_V1, *_SCHEMA_V2, *_SCHEMA_V3):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES (1, now()), (2, now()), (3, now())")
            # Seed workspace, request, loan, and canary receipt
            conn.execute("INSERT INTO workspaces (id, created_at) VALUES ('ws_prev', now())")
            conn.execute(
                """INSERT INTO requests (
                    id, workspace_id, borrower_label, equipment_kind,
                    pickup_location, due_at, status, created_at
                ) VALUES (
                    'req_prev', 'ws_prev', 'Borrower', 'WHEELCHAIR',
                    'Room 1', now() + INTERVAL '1 day', 'OPEN', now()
                )"""
            )
            conn.execute(
                """INSERT INTO canary_receipts (
                    receipt_id, owner_id, plan_hash, authorization_id,
                    authorization_expires_at, payload, reserved_sends,
                    reserved_output_tokens, payload_hash, state, concurrency_active
                ) VALUES (
                    'rcpt_prev', 'owner_prev', 'planhash', 'auth_prev',
                    now() + INTERVAL '1 hour', '{}', 6, 6144, 'payhash',
                    'SUCCEEDED', FALSE
                )"""
            )

        # Apply migrations: should advance from 3 to 4
        assert apply_migrations(url) == 4
        # Re-running is idempotent and repeatable
        assert apply_migrations(url) == 4

        with psycopg.connect(url) as conn:
            # Verify existing data preserved
            assert conn.execute("SELECT id FROM workspaces WHERE id = 'ws_prev'").fetchone() == (
                "ws_prev",
            )
            assert conn.execute("SELECT id FROM requests WHERE id = 'req_prev'").fetchone() == (
                "req_prev",
            )
            assert conn.execute(
                "SELECT receipt_id FROM canary_receipts WHERE receipt_id = 'rcpt_prev'"
            ).fetchone() == ("rcpt_prev",)
            # Verify inference_admissions table exists and is empty
            assert conn.execute(
                "SELECT to_regclass('public.inference_admissions') IS NOT NULL"
            ).fetchone() == (True,)
            assert conn.execute("SELECT count(*) FROM inference_admissions").fetchone() == (0,)


def test_reserve_valid_and_exact_replay(db: str) -> None:
    wid = _create_workspace(db)
    store = InferenceAdmissionStore(db)
    req = _sample_request(wid)

    row = store.reserve(req)
    assert row["reservation_id"] == req.reservation_id
    assert row["workspace_id"] == wid
    assert row["owner_id"] == req.owner_id
    assert row["request_key_hash"] == req.request_key_hash
    assert row["payload_hash"] == req.payload_hash
    assert row["reserved_sends"] == RESERVED_SENDS
    assert row["state"] == AdmissionState.RESERVED.value
    assert row["is_active"] is True
    assert row["released_at"] is None
    assert row["dispatched_at"] is None
    assert row["completed_at"] is None

    # Exact replay reads and returns identical record
    replay_row = store.reserve(req)
    assert replay_row["reservation_id"] == row["reservation_id"]
    assert replay_row["created_at"] == row["created_at"]

    # Verify getter methods
    assert store.get(req.reservation_id) == row
    assert store.get_active() == row
    assert store.get_by_request_key(wid, req.request_key_hash) == row


def test_reserve_identity_replay_conflicts(db: str) -> None:
    w1 = _create_workspace(db)
    w2 = _create_workspace(db)
    store = InferenceAdmissionStore(db)
    req = _sample_request(w1)
    store.reserve(req)

    # Changed workspace on same reservation_id
    with pytest.raises(AdmissionReplayConflictError, match="conflict"):
        store.reserve(replace(req, workspace_id=w2))

    # Changed owner on same reservation_id
    with pytest.raises(AdmissionReplayConflictError, match="conflict"):
        store.reserve(replace(req, owner_id=str(uuid4())))

    # Changed request_key_hash on same reservation_id
    other_key_hash = hashlib.sha256(b"other_key").hexdigest()
    with pytest.raises(AdmissionReplayConflictError, match="conflict"):
        store.reserve(replace(req, request_key_hash=other_key_hash))

    # Changed payload_hash on same reservation_id
    other_pay_hash = hashlib.sha256(b"other_payload").hexdigest()
    with pytest.raises(AdmissionReplayConflictError, match="conflict"):
        store.reserve(replace(req, payload_hash=other_pay_hash))

    # Different reservation_id for same (workspace_id, request_key_hash)
    with pytest.raises(AdmissionReplayConflictError, match="Request key already exists"):
        store.reserve(replace(req, reservation_id=str(uuid4())))


def test_invalid_inputs_rejected_fail_closed(db: str) -> None:
    wid = _create_workspace(db)
    store = InferenceAdmissionStore(db)

    # Bad reservation UUID
    with pytest.raises(InferenceAdmissionError, match="Invalid execution identity"):
        store.reserve(
            AdmissionReservationRequest(
                reservation_id="not-a-uuid",
                workspace_id=wid,
                owner_id=str(uuid4()),
                request_key_hash="a" * 64,
                payload_hash="b" * 64,
            )
        )

    # Uppercase hex in hash
    with pytest.raises(InferenceAdmissionError, match="Invalid request key hash"):
        store.reserve(
            AdmissionReservationRequest(
                reservation_id=str(uuid4()),
                workspace_id=wid,
                owner_id=str(uuid4()),
                request_key_hash="A" * 64,
                payload_hash="b" * 64,
            )
        )

    # Hash length mismatch
    with pytest.raises(InferenceAdmissionError, match="Invalid payload hash"):
        store.reserve(
            AdmissionReservationRequest(
                reservation_id=str(uuid4()),
                workspace_id=wid,
                owner_id=str(uuid4()),
                request_key_hash="a" * 64,
                payload_hash="b" * 63,
            )
        )

    # Reserved sends mismatch
    with pytest.raises(InferenceAdmissionError, match="Reservation mismatch"):
        store.reserve(
            AdmissionReservationRequest(
                reservation_id=str(uuid4()),
                workspace_id=wid,
                owner_id=str(uuid4()),
                request_key_hash="a" * 64,
                payload_hash="b" * 64,
                reserved_sends=5,
            )
        )

    # Verify zero rows in database
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM inference_admissions").fetchone() == (0,)


def test_single_active_operation_across_workspaces(db: str) -> None:
    w1 = _create_workspace(db)
    w2 = _create_workspace(db)
    store = InferenceAdmissionStore(db)

    req1 = _sample_request(w1)
    store.reserve(req1)

    req2 = _sample_request(w2)
    with pytest.raises(AdmissionRefusedError, match="Another operation is active"):
        store.reserve(req2)


def test_concurrent_reservations_race_single_winner(db: str) -> None:
    w1 = _create_workspace(db)
    w2 = _create_workspace(db)
    store = InferenceAdmissionStore(db)
    barrier = Barrier(2)

    req1 = _sample_request(w1)
    req2 = _sample_request(w2)

    def run_reserve(req: AdmissionReservationRequest) -> str:
        barrier.wait(timeout=5)
        try:
            store.reserve(req)
            return "SUCCESS"
        except AdmissionRefusedError:
            return "REFUSED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(run_reserve, req1)
        f2 = executor.submit(run_reserve, req2)
        results = sorted([f1.result(), f2.result()])

    assert results == ["REFUSED", "SUCCESS"]
    with psycopg.connect(db) as conn:
        count = conn.execute("SELECT count(*) FROM inference_admissions WHERE is_active").fetchone()
        assert count == (1,)


def test_dispatch_lifecycle_and_expired_deadline_never_releases(db: str) -> None:
    wid = _create_workspace(db)
    store = InferenceAdmissionStore(db)
    req = _sample_request(wid)
    store.reserve(req)

    # Wrong owner dispatch refused
    with pytest.raises(AdmissionRefusedError, match="Reservation unavailable"):
        store.mark_dispatched(req.reservation_id, str(uuid4()))

    # Correct owner dispatches successfully
    dispatched = store.mark_dispatched(req.reservation_id, req.owner_id)
    assert dispatched["state"] == AdmissionState.DISPATCHED.value
    assert dispatched["dispatched_at"] is not None

    # Repeat dispatch refused
    with pytest.raises(AdmissionRefusedError, match="Dispatch already consumed"):
        store.mark_dispatched(req.reservation_id, req.owner_id)

    # Finish first reservation and age released_at past 60s window
    store.finish(
        req.reservation_id,
        req.owner_id,
        AdmissionState.SUCCEEDED,
        cleanup_completed=True,
        actual_sends=1,
    )
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(
            "UPDATE inference_admissions"
            " SET released_at = clock_timestamp() - INTERVAL '65 seconds'"
            " WHERE reservation_id = %s",
            (req.reservation_id,),
        )

    # Test expired deadline: update deadline into the past
    req_expired = _sample_request(wid)
    store.reserve(req_expired)
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(
            "UPDATE inference_admissions"
            " SET deadline_at = clock_timestamp() - INTERVAL '10 seconds'"
            " WHERE reservation_id = %s",
            (req_expired.reservation_id,),
        )

    # Dispatch refused due to expired deadline
    with pytest.raises(AdmissionRefusedError, match="Reservation deadline expired"):
        store.mark_dispatched(req_expired.reservation_id, req_expired.owner_id)

    # Crucial invariant: Expired deadline NEVER auto-releases
    expired_row = store.get(req_expired.reservation_id)
    assert expired_row is not None
    assert expired_row["is_active"] is True
    assert expired_row["released_at"] is None


def test_finish_success_flow_and_replays(db: str) -> None:
    wid = _create_workspace(db)
    store = InferenceAdmissionStore(db)
    req = _sample_request(wid)
    store.reserve(req)
    store.mark_dispatched(req.reservation_id, req.owner_id)

    # Cleanup not completed refuses terminal state
    with pytest.raises(AdmissionRefusedError, match="cleanup_completed=True"):
        store.finish(
            req.reservation_id,
            req.owner_id,
            AdmissionState.SUCCEEDED,
            cleanup_completed=False,
            actual_sends=3,
        )

    # Success requires positive actual_sends
    with pytest.raises(AdmissionRefusedError, match="Success requires positive send evidence"):
        store.finish(
            req.reservation_id,
            req.owner_id,
            AdmissionState.SUCCEEDED,
            cleanup_completed=True,
            actual_sends=None,
        )

    with pytest.raises(AdmissionRefusedError, match="Success requires positive send evidence"):
        store.finish(
            req.reservation_id,
            req.owner_id,
            AdmissionState.SUCCEEDED,
            cleanup_completed=True,
            actual_sends=0,
        )

    # Success cannot have failure code
    with pytest.raises(AdmissionRefusedError, match="Success cannot have failure_code"):
        store.finish(
            req.reservation_id,
            req.owner_id,
            AdmissionState.SUCCEEDED,
            cleanup_completed=True,
            actual_sends=3,
            failure_code=AdmissionFailureCode.PROVIDER_FAILURE,
        )

    # Successful finish
    finished = store.finish(
        req.reservation_id,
        req.owner_id,
        AdmissionState.SUCCEEDED,
        cleanup_completed=True,
        actual_sends=3,
        actual_total_tokens=1500,
    )
    assert finished["state"] == AdmissionState.SUCCEEDED.value
    assert finished["is_active"] is False
    assert finished["actual_sends"] == 3
    assert finished["actual_total_tokens"] == 1500
    assert finished["cleanup_completed"] is True
    assert finished["completed_at"] is not None
    assert finished["released_at"] is not None

    # Exact replay returns same row
    replay = store.finish(
        req.reservation_id,
        req.owner_id,
        AdmissionState.SUCCEEDED,
        cleanup_completed=True,
        actual_sends=3,
        actual_total_tokens=1500,
    )
    assert replay["reservation_id"] == finished["reservation_id"]

    # Changed evidence replay raises conflict
    with pytest.raises(AdmissionReplayConflictError, match="Terminal evidence replay conflict"):
        store.finish(
            req.reservation_id,
            req.owner_id,
            AdmissionState.SUCCEEDED,
            cleanup_completed=True,
            actual_sends=4,
            actual_total_tokens=1500,
        )


def test_finish_failed_confirmed_flow_and_replays(db: str) -> None:
    wid = _create_workspace(db)
    store = InferenceAdmissionStore(db)
    req = _sample_request(wid)
    store.reserve(req)
    store.mark_dispatched(req.reservation_id, req.owner_id)

    # Failure code required
    with pytest.raises(AdmissionRefusedError, match="Failure code required"):
        store.finish(
            req.reservation_id,
            req.owner_id,
            AdmissionState.FAILED_CONFIRMED,
            cleanup_completed=True,
            failure_code=None,
        )

    failed = store.finish(
        req.reservation_id,
        req.owner_id,
        AdmissionState.FAILED_CONFIRMED,
        cleanup_completed=True,
        actual_sends=1,
        failure_code=AdmissionFailureCode.PROVIDER_FAILURE,
    )
    assert failed["state"] == AdmissionState.FAILED_CONFIRMED.value
    assert failed["is_active"] is False
    assert failed["failure_code"] == AdmissionFailureCode.PROVIDER_FAILURE.value
    assert failed["released_at"] is not None

    # Exact replay
    assert (
        store.finish(
            req.reservation_id,
            req.owner_id,
            AdmissionState.FAILED_CONFIRMED,
            cleanup_completed=True,
            actual_sends=1,
            failure_code=AdmissionFailureCode.PROVIDER_FAILURE,
        )
        == failed
    )


def test_finish_uncertain_stays_active_and_blocks_reservations(db: str) -> None:
    wid = _create_workspace(db)
    store = InferenceAdmissionStore(db)
    req = _sample_request(wid)
    store.reserve(req)
    store.mark_dispatched(req.reservation_id, req.owner_id)

    # Finish as UNCERTAIN
    uncertain = store.finish(
        req.reservation_id,
        req.owner_id,
        AdmissionState.UNCERTAIN,
        cleanup_completed=False,  # UNCERTAIN does not require cleanup_completed
        failure_code=AdmissionFailureCode.EXECUTION_UNKNOWN,
    )
    assert uncertain["state"] == AdmissionState.UNCERTAIN.value
    assert uncertain["is_active"] is True
    assert uncertain["released_at"] is None

    # Cleanup evidence is part of the durable receipt, including UNCERTAIN rows.
    with pytest.raises(AdmissionReplayConflictError, match="Terminal evidence replay conflict"):
        store.finish(
            req.reservation_id,
            req.owner_id,
            AdmissionState.UNCERTAIN,
            cleanup_completed=True,
            failure_code=AdmissionFailureCode.EXECUTION_UNKNOWN,
        )

    # Transition from UNCERTAIN to SUCCEEDED or FAILED_CONFIRMED is refused
    with pytest.raises(AdmissionRefusedError, match="Transition from UNCERTAIN refuses"):
        store.finish(
            req.reservation_id,
            req.owner_id,
            AdmissionState.SUCCEEDED,
            cleanup_completed=True,
            actual_sends=1,
        )

    # Subsequent reservation is refused because UNCERTAIN row remains active
    req2 = _sample_request(wid)
    with pytest.raises(AdmissionRefusedError, match="Another operation is active"):
        store.reserve(req2)


def test_recover_dead_execution_lifecycle_and_replays(db: str) -> None:
    wid = _create_workspace(db)
    store = InferenceAdmissionStore(db)
    req = _sample_request(wid)
    store.reserve(req)
    store.mark_dispatched(req.reservation_id, req.owner_id)
    store.finish(
        req.reservation_id,
        req.owner_id,
        AdmissionState.UNCERTAIN,
        cleanup_completed=False,
        failure_code=AdmissionFailureCode.EXECUTION_UNKNOWN,
    )

    op_id = str(uuid4())

    # Explicit confirmed_dead=True required
    with pytest.raises(
        AdmissionRefusedError, match="confirmation of terminated execution required"
    ):
        store.recover_dead(
            req.reservation_id,
            req.owner_id,
            operator_id=op_id,
            reason=RecoveryReason.PROCESS_DEAD,
            confirmed_dead=False,
        )

    # Successful recovery from UNCERTAIN
    recovered = store.recover_dead(
        req.reservation_id,
        req.owner_id,
        operator_id=op_id,
        reason=RecoveryReason.PROCESS_DEAD,
        confirmed_dead=True,
    )
    assert recovered["state"] == AdmissionState.RECOVERED.value
    assert recovered["is_active"] is False
    assert recovered["released_at"] is not None
    assert recovered["recovered_at"] is not None
    assert recovered["recovery_operator_id"] == op_id
    assert recovered["recovery_reason"] == RecoveryReason.PROCESS_DEAD.value

    # Exact replay returns same row
    replay = store.recover_dead(
        req.reservation_id,
        req.owner_id,
        operator_id=op_id,
        reason=RecoveryReason.PROCESS_DEAD,
        confirmed_dead=True,
    )
    assert replay["reservation_id"] == recovered["reservation_id"]

    # Conflicting recovery replay raises conflict
    with pytest.raises(AdmissionReplayConflictError, match="Recovery replay conflict"):
        store.recover_dead(
            req.reservation_id,
            req.owner_id,
            operator_id=str(uuid4()),
            reason=RecoveryReason.PROCESS_DEAD,
            confirmed_dead=True,
        )

    # Succeeded reservation cannot be recovered
    req_succ = _sample_request(wid)
    # Wait out 60s window or artificially advance released_at
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(
            "UPDATE inference_admissions"
            " SET released_at = clock_timestamp() - INTERVAL '65 seconds'"
        )
    store.reserve(req_succ)
    store.mark_dispatched(req_succ.reservation_id, req_succ.owner_id)
    store.finish(
        req_succ.reservation_id,
        req_succ.owner_id,
        AdmissionState.SUCCEEDED,
        cleanup_completed=True,
        actual_sends=2,
    )
    with pytest.raises(AdmissionRefusedError, match="Reservation is already settled"):
        store.recover_dead(
            req_succ.reservation_id,
            operator_id=op_id,
            reason=RecoveryReason.PROCESS_DEAD,
            confirmed_dead=True,
        )


def test_recovery_of_old_uncertainty_retains_fresh_full_window_debit(db: str) -> None:
    wid = _create_workspace(db)
    store = InferenceAdmissionStore(db)
    req = _sample_request(wid)

    # Insert an old uncertain reservation from 10 minutes ago
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO inference_admissions (
                reservation_id, workspace_id, owner_id, request_key_hash, payload_hash,
                reserved_sends, deadline_at, state, is_active, created_at, dispatched_at,
                failure_code, completed_at
            ) VALUES (
                %s, %s, %s, %s, %s, 6,
                clock_timestamp() - INTERVAL '8 minutes',
                'UNCERTAIN', TRUE,
                clock_timestamp() - INTERVAL '10 minutes',
                clock_timestamp() - INTERVAL '9 minutes',
                'execution_unknown',
                clock_timestamp() - INTERVAL '8 minutes'
            )""",
            (req.reservation_id, wid, req.owner_id, req.request_key_hash, req.payload_hash),
        )

    op_id = str(uuid4())
    recovered = store.recover_dead(
        req.reservation_id,
        operator_id=op_id,
        reason=RecoveryReason.TIMEOUT_TERMINATED,
        confirmed_dead=True,
    )
    assert recovered["state"] == AdmissionState.RECOVERED.value
    # released_at must be fresh (current clock timestamp, not 10 or 8 minutes ago)
    with psycopg.connect(db) as conn:
        diff_s = conn.execute(
            "SELECT EXTRACT(EPOCH FROM (clock_timestamp() - released_at)) AS diff"
            " FROM inference_admissions WHERE reservation_id = %s",
            (req.reservation_id,),
        ).fetchone()
        assert diff_s is not None
        assert diff_s[0] < 5.0

    # Fresh debit blocks new reservation under global 60-second cap
    req2 = _sample_request(wid)
    with pytest.raises(AdmissionRefusedError, match="Global 60-second reservation limit exceeded"):
        store.reserve(req2)


def test_rolling_60s_limit_and_full_debit_despite_actual_sends_1(db: str) -> None:
    wid = _create_workspace(db)
    store = InferenceAdmissionStore(db)
    req = _sample_request(wid)
    store.reserve(req)
    store.mark_dispatched(req.reservation_id, req.owner_id)

    # Only 1 actual send occurred
    store.finish(
        req.reservation_id,
        req.owner_id,
        AdmissionState.SUCCEEDED,
        cleanup_completed=True,
        actual_sends=1,
    )

    # New reservation immediately requested: full 6 sends debited, so 6 + 6 > 6 limit
    req2 = _sample_request(wid)
    with pytest.raises(AdmissionRefusedError, match="Global 60-second reservation limit exceeded"):
        store.reserve(req2)


def test_rolling_24h_global_limit(db: str) -> None:
    wid = _create_workspace(db)
    store = InferenceAdmissionStore(db)

    # Insert 20 released operations over the last 12 hours (outside 60s, inside 24h)
    # 20 * 6 = 120 sends (exact global 24h cap)
    with psycopg.connect(db, autocommit=True) as conn:
        for i in range(20):
            rid = str(uuid4())
            kh = hashlib.sha256(f"hist_{i}".encode()).hexdigest()
            conn.execute(
                """INSERT INTO inference_admissions (
                    reservation_id, workspace_id, owner_id, request_key_hash, payload_hash,
                    reserved_sends, deadline_at, state, is_active, created_at, released_at,
                    completed_at, actual_sends, cleanup_completed
                ) VALUES (
                    %s, %s, %s, %s, %s, 6,
                    clock_timestamp() - INTERVAL '10 minutes',
                    'SUCCEEDED', FALSE,
                    clock_timestamp() - INTERVAL '10 minutes',
                    clock_timestamp() - ((%s + 2) * INTERVAL '1 minute'),
                    clock_timestamp() - ((%s + 2) * INTERVAL '1 minute'),
                    6, TRUE
                )""",
                (rid, wid, str(uuid4()), kh, kh, i, i),
            )

    # 21st operation exceeds 120 sends in 24h
    req = _sample_request(wid)
    with pytest.raises(AdmissionRefusedError, match="Global 24-hour reservation limit exceeded"):
        store.reserve(req)


def test_rolling_24h_per_workspace_limit(db: str) -> None:
    w1 = _create_workspace(db)
    w2 = _create_workspace(db)
    store = InferenceAdmissionStore(db)

    # Insert 4 released operations in w1 (4 * 6 = 24 sends = w1 limit)
    with psycopg.connect(db, autocommit=True) as conn:
        for i in range(4):
            rid = str(uuid4())
            kh = hashlib.sha256(f"w1_hist_{i}".encode()).hexdigest()
            conn.execute(
                """INSERT INTO inference_admissions (
                    reservation_id, workspace_id, owner_id, request_key_hash, payload_hash,
                    reserved_sends, deadline_at, state, is_active, created_at, released_at,
                    completed_at, actual_sends, cleanup_completed
                ) VALUES (
                    %s, %s, %s, %s, %s, 6,
                    clock_timestamp() - INTERVAL '10 minutes',
                    'SUCCEEDED', FALSE,
                    clock_timestamp() - INTERVAL '10 minutes',
                    clock_timestamp() - ((%s + 2) * INTERVAL '1 minute'),
                    clock_timestamp() - ((%s + 2) * INTERVAL '1 minute'),
                    6, TRUE
                )""",
                (rid, w1, str(uuid4()), kh, kh, i, i),
            )

    # 5th operation in w1 refused
    req_w1 = _sample_request(w1)
    with pytest.raises(AdmissionRefusedError, match="Workspace 24-hour reservation limit exceeded"):
        store.reserve(req_w1)

    # Operation in w2 succeeds (global sum is 24, w2 sum is 0)
    req_w2 = _sample_request(w2)
    row_w2 = store.reserve(req_w2)
    assert row_w2["workspace_id"] == w2


def test_provider_429_cooldown_durable(db: str) -> None:
    wid = _create_workspace(db)
    store = InferenceAdmissionStore(db)
    req = _sample_request(wid)
    store.reserve(req)
    store.mark_dispatched(req.reservation_id, req.owner_id)

    # Finish with 429
    store.finish(
        req.reservation_id,
        req.owner_id,
        AdmissionState.FAILED_CONFIRMED,
        cleanup_completed=True,
        failure_code=AdmissionFailureCode.PROVIDER_429,
    )

    # Artificially age released_at so 60s limit is not the reason
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(
            "UPDATE inference_admissions"
            " SET released_at = clock_timestamp() - INTERVAL '70 seconds'"
        )

    # New store instance (simulating restart) still enforces 15-minute cooldown
    store2 = InferenceAdmissionStore(db)
    req2 = _sample_request(wid)
    with pytest.raises(AdmissionRefusedError, match="Provider 429 cooldown active"):
        store2.reserve(req2)


def test_database_engine_level_constraints(db: str) -> None:
    wid = _create_workspace(db)

    with psycopg.connect(db) as conn:
        # Constraint: Single active index
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(
                """INSERT INTO inference_admissions (
                    reservation_id, workspace_id, owner_id, request_key_hash, payload_hash,
                    reserved_sends, deadline_at, state, is_active
                ) VALUES
                (%s, %s, %s, %s, %s, 6, now() + INTERVAL '120s', 'RESERVED', TRUE),
                (%s, %s, %s, %s, %s, 6, now() + INTERVAL '120s', 'RESERVED', TRUE)""",
                (
                    str(uuid4()),
                    wid,
                    str(uuid4()),
                    "1" * 64,
                    "1" * 64,
                    str(uuid4()),
                    wid,
                    str(uuid4()),
                    "2" * 64,
                    "2" * 64,
                ),
            )

        # Constraint: is_active must match state
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                """INSERT INTO inference_admissions (
                    reservation_id, workspace_id, owner_id, request_key_hash, payload_hash,
                    reserved_sends, deadline_at, state, is_active
                ) VALUES (%s, %s, %s, %s, %s, 6, now() + INTERVAL '120s', 'RESERVED', FALSE)""",
                (str(uuid4()), wid, str(uuid4()), "3" * 64, "3" * 64),
            )

        # Constraint: released_at required when inactive
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                """INSERT INTO inference_admissions (
                    reservation_id, workspace_id, owner_id, request_key_hash, payload_hash,
                    reserved_sends, deadline_at, state, is_active, released_at, actual_sends
                ) VALUES (
                    %s, %s, %s, %s, %s, 6, now() + INTERVAL '120s', 'SUCCEEDED', FALSE, NULL, 1
                )""",
                (str(uuid4()), wid, str(uuid4()), "4" * 64, "4" * 64),
            )

        # Constraint: Success requires actual_sends between 1 and 6
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                """INSERT INTO inference_admissions (
                    reservation_id, workspace_id, owner_id, request_key_hash, payload_hash,
                    reserved_sends, deadline_at, state, is_active, released_at, actual_sends
                ) VALUES (
                    %s, %s, %s, %s, %s, 6, now() + INTERVAL '120s', 'SUCCEEDED', FALSE, now(), 0
                )""",
                (str(uuid4()), wid, str(uuid4()), "5" * 64, "5" * 64),
            )

        # Constraint: Success forbids failure_code
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                """INSERT INTO inference_admissions (
                    reservation_id, workspace_id, owner_id, request_key_hash, payload_hash,
                    reserved_sends, deadline_at, state, is_active, released_at,
                    actual_sends, failure_code
                ) VALUES (
                    %s, %s, %s, %s, %s, 6, now() + INTERVAL '120s', 'SUCCEEDED', FALSE, now(),
                    1, 'provider_failure'
                )""",
                (str(uuid4()), wid, str(uuid4()), "6" * 64, "6" * 64),
            )
