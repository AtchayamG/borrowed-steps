"""Tests for HostedSchedulerService and schema V2 scheduler control.

Verifies bounded execution, concurrency control, lease expiration/superseding,
outcome recording, status reporting, and V1->V2 migration preservation against
real PostgreSQL.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from borrowed_steps.application import use_cases as uc
from borrowed_steps.application.ports import DueTaskRef, IdempotencyRecord, Session
from borrowed_steps.domain.models import (
    Equipment,
    EquipmentKind,
    EventAction,
    TaskStatus,
)
from borrowed_steps.infrastructure.hosted_scheduler import HostedSchedulerService
from borrowed_steps.infrastructure.postgres_migrations import (
    EXPECTED_SCHEMA_VERSION,
    SchemaVersionError,
    apply_migrations,
    read_schema_version,
)
from borrowed_steps.infrastructure.postgres_store import PostgresStore, PostgresUnitOfWork
from borrowed_steps.infrastructure.system import SecretsIdGenerator, SystemClock
from postgres_support import (
    disposable_database,
    get_test_postgres_url,
    migrated_database,
    v1_database,
)


@pytest.mark.parametrize("budget", [float("nan"), float("inf"), -1.0, 21.0])
def test_invalid_deadline_cannot_disable_bound(budget: float) -> None:
    with pytest.raises(ValueError, match="Processing budget"):
        HostedSchedulerService("postgresql://localhost/test").tick(processing_budget_seconds=budget)


def test_event_failure_is_sanitized_and_counts_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_database() as url:
        store = PostgresStore(url)
        svc, session, eq = _seed_workspace(store)
        _create_loan_with_due_task(svc, session, eq)

        def fail(self: PostgresUnitOfWork, event: object) -> None:
            raise RuntimeError("synthetic-private-db-detail")

        monkeypatch.setattr(PostgresUnitOfWork, "add_event", fail)
        sched = HostedSchedulerService(url)
        result = sched()
        assert result.outcome == "failed"
        assert "synthetic-private-db-detail" not in repr(result)
        status = sched.read_status().to_dict()
        assert status["counts_complete"] is False
        assert status["counts"] is None
        with store.transaction(session.workspace_id, write=False) as uow:
            assert uow.list_tasks()[0].status is TaskStatus.PENDING
            assert not any(e.action is EventAction.PICKUP_DUE for e in uow.list_events())


def test_multiple_slow_sql_statements_do_not_get_transaction_sized_timeouts() -> None:
    with migrated_database() as url:
        sched = HostedSchedulerService(url)
        with sched._store._connection() as conn:
            start = time.monotonic()
            # Each completes below the500ms ceiling, proving waits accumulate.
            for _ in range(3):
                conn.execute("SELECT pg_sleep(0.1)")
            elapsed = time.monotonic() - start
            assert elapsed >= 0.3
            with pytest.raises(psycopg.errors.QueryCanceled):
                conn.execute("SELECT pg_sleep(5)")
            conn.rollback()
            assert time.monotonic() - start < 2


def test_connection_failure_does_not_leak_driver_details(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise psycopg.OperationalError("synthetic-private-db-detail")

    monkeypatch.setattr(psycopg, "connect", fail)
    with pytest.raises(RuntimeError, match="storage operation failed") as exc:
        PostgresStore("postgresql://localhost/test").get_session("synthetic")
    assert exc.value.__suppress_context__
    assert "synthetic-private-db-detail" not in str(exc.value)


def _seed_workspace(store: PostgresStore) -> tuple[uc.Services, Session, Equipment]:
    """Create a standard workspace with one equipment item and session."""
    clock = SystemClock()
    ids = SecretsIdGenerator()
    svc = uc.Services(store, clock, ids)
    session = uc.start_workspace(svc)
    with store.transaction(session.workspace_id, write=False) as uow:
        equipment = uow.list_equipment()[0]
    return svc, session, equipment


def _request(svc: uc.Services, wid: str) -> str:
    with svc.store.transaction(wid) as uow:
        return uc.create_request(
            svc,
            uow,
            borrower_label="Synthetic",
            equipment_kind=EquipmentKind.WHEELCHAIR,
            pickup_location="Test room",
            due_at=svc.clock.now() + timedelta(hours=1),
        ).id


def _reserve(svc: uc.Services, wid: str, rid: str, eid: str) -> str:
    with svc.store.transaction(wid) as uow:
        return uc.reserve_equipment(
            svc,
            uow,
            request_id=rid,
            equipment_id=eid,
            expected_equipment_version=1,
            human_approved=True,
        )[2].id


def _create_loan_with_due_task(
    svc: uc.Services,
    session: Session,
    equipment: Equipment,
) -> tuple[str, str, str]:
    """Create request and loan; reserve_equipment automatically creates a PENDING task."""
    rid = _request(svc, session.workspace_id)
    loan_id = _reserve(svc, session.workspace_id, rid, equipment.id)
    with svc.store.transaction(session.workspace_id, write=False) as uow:
        tasks = [t for t in uow.list_tasks() if t.loan_id == loan_id]
        task_id = tasks[0].id
    return rid, loan_id, task_id


def test_v1_to_v2_migration_preserves_business_records_and_exact_strings() -> None:
    """Verify that migrating from schema V1 to V2 preserves all business records byte-for-byte."""
    get_test_postgres_url()

    with v1_database() as url:
        assert read_schema_version(url) == 1

        # Seed rich business records in V1
        store_v1 = PostgresStore(url)
        svc, session, eq = _seed_workspace(store_v1)
        wid = session.workspace_id

        rid = _request(svc, wid)
        _reserve(svc, wid, rid, eq.id)
        with store_v1.transaction(wid) as uow:
            idemp = IdempotencyRecord(
                key="idemp-key-1",
                route="reserve",
                request_hash="sha256-test-hash",
                status_code=200,
                response_body='{"status":"ok","exact_string":"alpha-beta-123"}',
            )
            uow.save_idempotency(idemp)

        # Snapshot records in V1
        sess_before = store_v1.get_session(session.id)
        assert sess_before is not None
        with store_v1.transaction(wid, write=False) as uow:
            reqs_before = uow.list_requests()
            loans_before = uow.list_loans()
            events_before = uow.list_events()
            idemp_before = uow.get_idempotency("idemp-key-1")

        # Apply migration to V2
        new_version = apply_migrations(url)
        assert new_version == EXPECTED_SCHEMA_VERSION == 2

        # Verify schema_migrations has version 1 and 2
        with psycopg.connect(url) as conn:
            versions = [
                r[0]
                for r in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
            assert versions == [1, 2]

            # Verify scheduler_control table exists and has row id=1
            ctrl_row = conn.execute(
                "SELECT id, active_run_id, last_outcome FROM scheduler_control WHERE id = 1"
            ).fetchone()
            assert ctrl_row is not None
            assert ctrl_row[0] == 1
            assert ctrl_row[1] is None
            assert ctrl_row[2] == "never_run"

        # Verify business records in V2 are identical
        store_v2 = PostgresStore(url)
        store_v2.check_schema()

        sess_after = store_v2.get_session(session.id)
        assert sess_after == sess_before

        with store_v2.transaction(wid, write=False) as uow:
            assert uow.list_requests() == reqs_before
            assert uow.list_loans() == loans_before
            assert uow.list_events() == events_before
            idemp_after = uow.get_idempotency("idemp-key-1")
            assert idemp_after == idemp_before
            assert idemp_after is not None
            assert idemp_after.response_body == '{"status":"ok","exact_string":"alpha-beta-123"}'

        # Readiness checks succeed for both store and scheduler
        sched = HostedSchedulerService(url)
        sched.check_schema()


def test_read_status_initial_never_run_and_read_only() -> None:
    """Reading status before any runs returns never_run and makes no database mutations."""
    get_test_postgres_url()

    with migrated_database() as url:
        sched = HostedSchedulerService(url)
        status = sched.read_status()

        assert status.status == "never_run"
        assert status.last_outcome == "never_run"
        assert status.last_run_id is None
        assert status.last_completed_at is None
        assert status.last_success_at is None
        assert status.last_success_recent is False
        assert status.stale_warning is True
        assert status.active_run_id is None
        assert status.last_considered == 0
        assert status.last_marked_due == 0
        assert status.last_resolved_stale == 0
        assert status.last_unchanged == 0
        assert status.last_contended == 0
        assert status.last_stopped_early is False

        # Verify to_dict format
        d = status.to_dict()
        assert d["status"] == "never_run"
        assert d["last_outcome"] == "never_run"
        assert d["last_success_recent"] is False
        assert d["stale_warning"] is True
        assert d["counts"] == {
            "considered": 0,
            "marked_due": 0,
            "resolved_stale": 0,
            "unchanged": 0,
            "contended": 0,
            "stopped_early": False,
        }

        # Verify table was not modified
        with psycopg.connect(url) as conn:
            row = conn.execute("SELECT * FROM scheduler_control WHERE id = 1").fetchone()
            assert row is not None
            assert row[1] is None  # active_run_id


def test_read_status_carries_no_secrets_or_entity_ids() -> None:
    """Status dictionary contains only counts/timestamps and no secrets or domain entity IDs."""
    get_test_postgres_url()

    with migrated_database() as url:
        sched = HostedSchedulerService(url)
        d = sched.read_status().to_dict()

        forbidden_substrings = [
            "workspace",
            "borrower",
            "session",
            "password",
            "secret",
            "token",
            "postgres",
            "localhost",
            "54340",
            "equipment",
            "intake",
            "notes",
        ]
        dict_str = str(d).lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in dict_str, f"Found forbidden substring '{forbidden}' in status"


def test_single_tick_success_and_status_update() -> None:
    """One tick processes due tasks, produces events, updates status, and yields success."""
    get_test_postgres_url()

    with migrated_database() as url:
        store = PostgresStore(url)
        svc, session, eq = _seed_workspace(store)
        _create_loan_with_due_task(svc, session, eq)

        sched = HostedSchedulerService(url)
        res = sched()

        assert res.outcome == "success"
        assert res.run_id is not None
        assert res.capacity_limited is False
        assert res.error is None
        assert res.report is not None
        assert res.report.considered == 1
        assert res.report.marked_due == 1
        assert res.report.resolved_stale == 0
        assert res.report.unchanged == 0
        assert res.report.contended == 0
        assert res.report.stopped_early is False

        # Check status after run
        st = sched.read_status()
        assert st.status == "success"
        assert st.last_outcome == "success"
        assert st.last_run_id == res.run_id
        assert st.last_completed_at is not None
        assert st.last_success_at == st.last_completed_at
        assert st.last_success_recent is True
        assert st.stale_warning is False
        assert st.active_run_id is None
        assert st.last_considered == 1
        assert st.last_marked_due == 1
        assert st.last_stopped_early is False

        # A new process must recover committed aggregate evidence from PostgreSQL.
        recovered = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,sys; "
                "from borrowed_steps.infrastructure.hosted_scheduler "
                "import HostedSchedulerService; "
                "print(json.dumps(HostedSchedulerService(sys.argv[1]).read_status().to_dict()))",
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        persisted = json.loads(recovered.stdout)
        assert persisted["status"] == "success"
        assert persisted["counts"] == st.to_dict()["counts"]
        assert persisted["last_completed_at"] == st.to_dict()["last_completed_at"]

        # Verify due notice event was created in workspace
        with store.transaction(session.workspace_id, write=False) as uow:
            events = uow.list_events()
            due_events = [e for e in events if e.action == EventAction.PICKUP_DUE]
            assert len(due_events) == 1
            # Task status is now DUE
            assert uow.list_tasks()[0].status == TaskStatus.DUE


def test_rerun_tick_makes_no_duplicate_events() -> None:
    """Re-running a tick when no further tasks are due processes 0 tasks and adds no events."""
    get_test_postgres_url()

    with migrated_database() as url:
        store = PostgresStore(url)
        svc, session, eq = _seed_workspace(store)
        _create_loan_with_due_task(svc, session, eq)

        sched = HostedSchedulerService(url)
        res1 = sched()
        assert res1.outcome == "success"

        with store.transaction(session.workspace_id, write=False) as uow:
            events_count1 = len(uow.list_events())

        # Second tick
        res2 = sched()
        assert res2.outcome == "success"
        assert res2.report is not None
        assert res2.report.considered == 0

        with store.transaction(session.workspace_id, write=False) as uow:
            events_count2 = len(uow.list_events())
            assert events_count2 == events_count1


def test_concurrent_competing_instances_admit_once_second_returns_busy() -> None:
    """When an instance has an active lease, a concurrent tick returns busy."""
    get_test_postgres_url()

    with migrated_database() as url:
        sched = HostedSchedulerService(url)

        # Manually occupy active lease
        with psycopg.connect(url) as conn:
            conn.execute(
                "UPDATE scheduler_control SET "
                "active_run_id = 'active-run-abc', "
                "active_run_started_at = now(), "
                "active_run_expires_at = now() + interval '60 seconds' "
                "WHERE id = 1"
            )

        # Attempt concurrent tick
        res = sched()
        assert res.outcome == "busy"
        assert res.run_id is None
        assert res.report is None

        # Status shows 'running'
        st = sched.read_status()
        assert st.status == "running"
        assert st.active_run_id == "active-run-abc"


def test_monotonic_deadline_stops_processing_yields_partial() -> None:
    """Monotonic deadline stops beginning candidates and produces partial outcome."""
    get_test_postgres_url()

    with migrated_database() as url:
        store = PostgresStore(url)
        svc, session, eq = _seed_workspace(store)
        _create_loan_with_due_task(svc, session, eq)

        # First run to establish a last_success_at
        sched = HostedSchedulerService(url)
        res1 = sched()
        assert res1.outcome == "success"
        success_ts = sched.read_status().last_success_at
        assert success_ts is not None

        # Add another workspace with due task
        svc2, session2, eq2 = _seed_workspace(store)
        _create_loan_with_due_task(svc2, session2, eq2)

        # Tick with immediate deadline (negative processing budget)
        res2 = sched(processing_budget_seconds=0.0)
        assert res2.outcome == "partial"
        assert res2.report is not None
        assert res2.report.stopped_early is True

        # Status must reflect partial and preserve prior success timestamp
        st = sched.read_status()
        assert st.status == "partial"
        assert st.last_outcome == "partial"
        assert st.last_stopped_early is True
        assert st.last_success_at == success_ts


def test_candidate_limit_capacity_limited_yields_partial() -> None:
    """Reaching candidate limit yields partial (capacity-limited) outcome."""
    get_test_postgres_url()

    with migrated_database() as url:
        store = PostgresStore(url)
        # Create 2 workspaces, each with 1 due task
        svc1, session1, eq1 = _seed_workspace(store)
        _create_loan_with_due_task(svc1, session1, eq1)

        svc2, session2, eq2 = _seed_workspace(store)
        _create_loan_with_due_task(svc2, session2, eq2)

        sched = HostedSchedulerService(url)
        res = sched(limit=1)
        assert res.outcome == "partial"
        assert res.capacity_limited is True
        assert res.report is not None
        assert res.report.considered == 1

        st = sched.read_status()
        assert st.status == "partial"
        assert st.last_outcome == "partial"


def test_workspace_lock_contention_yields_partial() -> None:
    """Contention on workspace row lock results in contended count and partial outcome."""
    get_test_postgres_url()

    with migrated_database() as url:
        store = PostgresStore(url)
        svc, session, eq = _seed_workspace(store)
        _create_loan_with_due_task(svc, session, eq)

        # Hold a workspace lock on a separate connection
        lock_conn = psycopg.connect(url)
        lock_conn.execute(
            "SELECT id FROM workspaces WHERE id = %s FOR UPDATE",
            (session.workspace_id,),
        )

        try:
            # Scheduler with very short lock timeout (100ms)
            sched = HostedSchedulerService(url, lock_timeout_ms=100)
            res = sched()
            assert res.outcome == "partial"
            assert res.report is not None
            assert res.report.considered == 1
            assert res.report.contended == 1

            st = sched.read_status()
            assert st.status == "partial"
            assert st.last_contended == 1
        finally:
            lock_conn.rollback()
            lock_conn.close()


def test_expired_lease_superseded_and_stale_finalizer_cannot_overwrite() -> None:
    """An expired lease is superseded by a new run, and the stale run cannot overwrite."""
    get_test_postgres_url()

    with migrated_database() as url:
        sched = HostedSchedulerService(url)

        # 1. Simulate a crashed instance past 60s lease
        with psycopg.connect(url) as conn:
            conn.execute(
                "UPDATE scheduler_control SET "
                "active_run_id = 'crashed-run-1', "
                "active_run_started_at = now() - interval '70 seconds', "
                "active_run_expires_at = now() - interval '10 seconds' "
                "WHERE id = 1"
            )

        # 2. Reading status shows 'expired'
        st = sched.read_status()
        assert st.status == "expired"
        assert st.active_run_id == "crashed-run-1"

        # 3. New instance can supersede
        res = sched()
        assert res.outcome == "success"
        assert res.run_id != "crashed-run-1"

        # 4. Attempt by stale run to finalize fails (returns False, does not overwrite)
        with psycopg.connect(url, row_factory=dict_row) as conn:
            stale_finalized = sched._finalize(conn, "crashed-run-1", "failed", None)
            assert stale_finalized is False

        # Status remains new run's outcome
        st_after = sched.read_status()
        assert st_after.status == "success"
        assert st_after.last_run_id == res.run_id


def test_status_freshness_warning_after_30_minutes() -> None:
    """Status shows stale_warning=True when last_success_at is older than 30 minutes."""
    get_test_postgres_url()

    with migrated_database() as url:
        sched = HostedSchedulerService(url)

        # Set last_success_at to 35 minutes ago
        with psycopg.connect(url) as conn:
            conn.execute(
                "UPDATE scheduler_control SET "
                "last_outcome = 'success', "
                "last_completed_at = now() - interval '35 minutes', "
                "last_success_at = now() - interval '35 minutes' "
                "WHERE id = 1"
            )

        st = sched.read_status()
        assert st.status == "success"
        assert st.last_outcome == "success"
        assert st.last_success_recent is False
        assert st.stale_warning is True

        # Now set last_success_at to 20 minutes ago
        with psycopg.connect(url) as conn:
            conn.execute(
                "UPDATE scheduler_control SET "
                "last_success_at = now() - interval '20 minutes' "
                "WHERE id = 1"
            )

        st2 = sched.read_status()
        assert st2.last_success_recent is True
        assert st2.stale_warning is False


def test_evidence_write_failure_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If persisting evidence fails, tick raises RuntimeError and does not report success."""
    get_test_postgres_url()

    with migrated_database() as url:
        sched = HostedSchedulerService(url)

        # Break finalization
        def bad_finalize(
            conn: object,
            run_id: str,
            outcome: str,
            report: object,
        ) -> bool:
            raise psycopg.OperationalError("Simulated DB connection dropped")

        monkeypatch.setattr(sched, "_finalize", bad_finalize)

        with pytest.raises(RuntimeError, match="Failed to persist scheduler execution outcome"):
            sched()


def test_human_action_after_discovery_prevents_stale_notice() -> None:
    """Human pickup or return racing with discovered task is safely handled."""
    get_test_postgres_url()

    with migrated_database() as url:
        store = PostgresStore(url)
        svc, session, eq = _seed_workspace(store)
        _rid, loan_id, task_id = _create_loan_with_due_task(svc, session, eq)

        # Candidates discovered at this point
        candidates = store.due_task_candidates(svc.clock.now(), 100)
        assert len(candidates) == 1
        assert candidates[0].task_id == task_id

        # Human borrower acts (picks up loan) before scheduler processes the discovered candidate
        with store.transaction(session.workspace_id) as uow:
            uc.pick_up_loan(
                svc,
                uow,
                loan_id=loan_id,
                expected_equipment_version=2,
                human_approved=True,
            )

        # Now scheduler processes the already-discovered candidate
        class DiscoveredStore(PostgresStore):
            def due_task_candidates(self, now: datetime, limit: int) -> list[DueTaskRef]:
                return candidates

        sched = HostedSchedulerService(
            url,
            store=DiscoveredStore(
                url, connect_timeout_s=2, statement_timeout_ms=500, lock_timeout_ms=250
            ),
        )
        res = sched()

        assert res.outcome == "success"
        assert res.report is not None
        assert res.report.considered == 1
        # The pickup action answered the notice, so mark_task_due returned false or task resolved
        assert res.report.marked_due == 0
        assert res.report.unchanged == 1 or res.report.resolved_stale == 1

        # Verify no PICKUP_DUE notice event was created
        with store.transaction(session.workspace_id, write=False) as uow:
            assert not any(e.action == EventAction.PICKUP_DUE for e in uow.list_events())


def test_controlled_blocked_query_finite_bound_demonstration() -> None:
    """Demonstrates that lock and statement timeouts enforce a finite execution bound."""
    get_test_postgres_url()

    with migrated_database() as url:
        store = PostgresStore(url)
        svc, session, eq = _seed_workspace(store)
        _create_loan_with_due_task(svc, session, eq)

        # Hold a blocking lock
        lock_conn = psycopg.connect(url)
        lock_conn.execute(
            "SELECT id FROM workspaces WHERE id = %s FOR UPDATE",
            (session.workspace_id,),
        )

        try:
            # Set a 200ms lock timeout
            sched = HostedSchedulerService(
                url,
                lock_timeout_ms=200,
                statement_timeout_ms=500,
                connect_timeout_s=2,
            )
            t0 = time.monotonic()
            res = sched()
            duration = time.monotonic() - t0

            # Verified bound: must complete well within a fraction of a second, not hang
            assert duration < 2.0, f"Expected bounded execution < 2.0s, took {duration}s"
            assert res.outcome == "partial"
            assert res.report is not None
            assert res.report.contended == 1
        finally:
            lock_conn.rollback()
            lock_conn.close()


def test_schema_version_gates() -> None:
    """HostedSchedulerService strictly enforces EXPECTED_SCHEMA_VERSION == 2."""
    get_test_postgres_url()

    with disposable_database() as url:
        sched = HostedSchedulerService(url)

        # Version 0 refuses
        with pytest.raises(SchemaVersionError):
            sched.check_schema()
        with pytest.raises(SchemaVersionError):
            sched.tick()
        with pytest.raises(SchemaVersionError):
            sched.read_status()

        # Version 1 refuses
        with psycopg.connect(url) as conn:
            conn.execute(
                "CREATE TABLE schema_migrations (version INT PRIMARY KEY, applied_at TIMESTAMPTZ)"
            )
            conn.execute("INSERT INTO schema_migrations VALUES (1, now())")

        with pytest.raises(SchemaVersionError):
            sched.check_schema()

        # Future version 3 refuses
        with psycopg.connect(url) as conn:
            conn.execute("INSERT INTO schema_migrations VALUES (3, now())")

        with pytest.raises(SchemaVersionError):
            sched.check_schema()
