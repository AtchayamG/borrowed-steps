"""Real PostgreSQL acceptance; BS_POSTGRES_TEST_URL must name a disposable server."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import TypeVar
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import DictRow

from borrowed_steps.application import use_cases as uc
from borrowed_steps.application.coordination import process_due_tasks
from borrowed_steps.application.errors import (
    IdempotencyConflictError,
    SessionRequiredError,
    StorageBusyError,
)
from borrowed_steps.application.ports import DueTaskRef, IdempotencyRecord, Session, Store
from borrowed_steps.domain.errors import StateConflictError
from borrowed_steps.domain.models import (
    EquipmentKind,
    EquipmentState,
    EventAction,
    LoanStatus,
    TaskKind,
    TaskStatus,
)
from borrowed_steps.infrastructure import postgres_migrations as migrations
from borrowed_steps.infrastructure import postgres_store as pg
from borrowed_steps.infrastructure.postgres_migrations import (
    SchemaVersionError,
    apply_migrations,
    read_schema_version,
)
from borrowed_steps.infrastructure.postgres_store import PostgresStore, PostgresUnitOfWork
from borrowed_steps.infrastructure.system import SecretsIdGenerator


@dataclass
class Clock:
    instant: datetime = datetime(2026, 9, 9, 10, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


@pytest.fixture
def database_url() -> Iterator[str]:
    base = os.environ.get("BS_POSTGRES_TEST_URL")
    if not base:
        pytest.skip("BS_POSTGRES_TEST_URL not configured; real PostgreSQL proof not run")
    # An explicit disposable database is created for each case; never drop caller's DB.
    name = "bs011_test_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(base)
    url = urlunsplit(parts._replace(path="/" + name))
    try:
        yield url
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def store(database_url: str) -> PostgresStore:
    assert apply_migrations(database_url) == 4
    result = PostgresStore(database_url)
    result.check_schema()
    return result


def workspace(store: Store) -> tuple[uc.Services, Session, str]:
    svc = uc.Services(store, Clock(), SecretsIdGenerator())
    session = uc.start_workspace(svc)
    with store.transaction(session.workspace_id, write=False) as uow:
        equipment_id = uow.list_equipment()[0].id
    return svc, session, equipment_id


def request(svc: uc.Services, wid: str) -> str:
    with svc.store.transaction(wid) as uow:
        return uc.create_request(
            svc,
            uow,
            borrower_label="Synthetic",
            equipment_kind=EquipmentKind.WHEELCHAIR,
            pickup_location="Test room",
            due_at=svc.clock.now() + timedelta(hours=1),
        ).id


def reserve(svc: uc.Services, wid: str, rid: str, eid: str) -> str:
    with svc.store.transaction(wid) as uow:
        return uc.reserve_equipment(
            svc,
            uow,
            request_id=rid,
            equipment_id=eid,
            expected_equipment_version=1,
            human_approved=True,
        )[2].id


_T = TypeVar("_T")


def parallel(action: Callable[[], _T]) -> list[_T]:
    barrier = Barrier(2)

    def run() -> _T:
        barrier.wait(timeout=5)
        return action()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        return [future.result(timeout=20) for future in futures]


def test_migrations_concurrent_repeat_and_future(database_url: str) -> None:
    assert read_schema_version(database_url) == 0
    with pytest.raises(SchemaVersionError):
        PostgresStore(database_url).check_schema()
    assert parallel(lambda: apply_migrations(database_url)) == [4, 4]
    store = PostgresStore(database_url)
    _, session, _ = workspace(store)
    assert apply_migrations(database_url) == 4
    assert store.get_session(session.id) == session
    with psycopg.connect(database_url) as conn:
        conn.execute("INSERT INTO schema_migrations VALUES (5, now())")
    with pytest.raises(SchemaVersionError):
        apply_migrations(database_url)


def test_lifecycle_sessions_and_persistence(store: PostgresStore, database_url: str) -> None:
    svc, session, eid = workspace(store)
    wid = session.workspace_id
    assert uc.resolve_session(svc, session.id) == session
    for value in [None, "unknown"]:
        with pytest.raises(SessionRequiredError):
            uc.resolve_session(svc, value)
    expired = replace(session, id=uuid4().hex, expires_at=svc.clock.now())
    store.create_session(expired)
    with pytest.raises(SessionRequiredError):
        uc.resolve_session(svc, expired.id)
    rid = request(svc, wid)
    lid = reserve(svc, wid, rid, eid)
    with store.transaction(wid) as uow:
        assert uow.get_task("missing") is None
        assert uow.find_task(lid, TaskKind.PICKUP_DUE) is not None
        uc.pick_up_loan(svc, uow, loan_id=lid, expected_equipment_version=2, human_approved=True)
    with store.transaction(wid) as uow:
        uc.return_loan(svc, uow, loan_id=lid, expected_equipment_version=3, human_approved=True)
        assert uow.find_returned_loan(eid) is not None
    with store.transaction(wid) as uow:
        uc.inspect_item(
            svc,
            uow,
            equipment_id=eid,
            outcome=EquipmentState.QUARANTINED,
            expected_equipment_version=4,
            human_approved=True,
        )
    with PostgresStore(database_url).transaction(wid, write=False) as uow:
        snapshot = uc.read_snapshot(uow)
        assert snapshot.equipment[0].state is EquipmentState.QUARANTINED
        assert snapshot.loans[0].status is LoanStatus.CLOSED
        assert len(snapshot.events) == 5
        assert all(t.status is TaskStatus.RESOLVED for t in snapshot.tasks)
        assert uow.open_tasks_for_loan(lid) == []
        assert snapshot.requests[0].due_at == svc.clock.now() + timedelta(hours=1)
    code = (
        "import os; "
        "from borrowed_steps.infrastructure.postgres_store import PostgresStore; "
        "s=PostgresStore(os.environ['BS_CHILD_URL']); s.check_schema(); "
        "assert s.get_session(os.environ['BS_CHILD_SESSION']) is not None; "
        "print('restart persistence PASS')"
    )
    env = {
        **os.environ,
        "BS_CHILD_URL": database_url,
        "BS_CHILD_SESSION": session.id,
        "PYTHONPATH": "src",
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0
    assert "restart persistence PASS" in result.stdout


def test_two_allocators_and_workspace_isolation(store: PostgresStore) -> None:
    svc, session, eid = workspace(store)
    other_svc, other, other_eid = workspace(store)
    rid = request(svc, session.workspace_id)

    def allocate() -> str:
        try:
            reserve(svc, session.workspace_id, rid, eid)
            return "won"
        except StateConflictError:
            return "conflict"

    assert sorted(parallel(allocate)) == ["conflict", "won"]
    with store.transaction(other.workspace_id) as uow:
        assert uow.get_request(rid) is None
        assert uow.get_equipment(eid) is None
        assert uow.list_loans() == []
        assert uow.get_idempotency("anything") is None
    # A writer in another workspace succeeds while the first workspace is locked.
    with store.transaction(session.workspace_id), ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(request, other_svc, other.workspace_id).result(timeout=5)
    assert other_eid != eid


def test_idempotency_serializes_exact_bytes_and_conflicts(store: PostgresStore) -> None:
    svc, session, _ = workspace(store)
    payload = '{ "z": 1, "a": "synthetic" }\n'

    def mutate(fingerprint: str = "hash") -> str:
        with store.transaction(session.workspace_id) as uow:
            previous = uow.get_idempotency("key")
            if previous:
                if previous.request_hash != fingerprint:
                    raise IdempotencyConflictError("Conflicting request")
                return previous.response_body
            uc.create_request(
                svc,
                uow,
                borrower_label="Synthetic",
                equipment_kind=EquipmentKind.WHEELCHAIR,
                pickup_location="Test",
                due_at=svc.clock.now() + timedelta(hours=1),
            )
            uow.save_idempotency(IdempotencyRecord("key", "create", fingerprint, 201, payload))
            return payload

    assert parallel(mutate) == [payload, payload]
    with pytest.raises(IdempotencyConflictError):
        mutate("different")
    with store.transaction(session.workspace_id, write=False) as uow:
        assert len(uow.list_requests()) == len(uow.list_events()) == 1


def test_event_failure_rolls_back_mutation_and_idempotency(
    store: PostgresStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, session, _ = workspace(store)

    def fail(self: PostgresUnitOfWork, event: object) -> None:
        raise RuntimeError("synthetic event failure")

    monkeypatch.setattr(PostgresUnitOfWork, "add_event", fail)

    def mutate() -> None:
        with store.transaction(session.workspace_id) as uow:
            uow.save_idempotency(IdempotencyRecord("key", "create", "hash", 201, "exact"))
            uc.create_request(
                svc,
                uow,
                borrower_label="Synthetic",
                equipment_kind=EquipmentKind.WHEELCHAIR,
                pickup_location="Test",
                due_at=svc.clock.now() + timedelta(hours=1),
            )

    with pytest.raises(RuntimeError, match="synthetic event failure"):
        mutate()
    with store.transaction(session.workspace_id, write=False) as uow:
        assert not uow.list_requests()
        assert not uow.list_events()
        assert uow.get_idempotency("key") is None


def test_due_processors_claim_once_and_event_failure_atomic(
    store: PostgresStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, session, eid = workspace(store)
    reserve(svc, session.workspace_id, request(svc, session.workspace_id), eid)
    original = PostgresUnitOfWork.add_event

    def fail(self: PostgresUnitOfWork, event: object) -> None:
        raise RuntimeError("synthetic due event failure")

    monkeypatch.setattr(PostgresUnitOfWork, "add_event", fail)
    with pytest.raises(RuntimeError, match="synthetic due event failure"):
        process_due_tasks(store, svc.clock, svc.ids, limit=100)
    assert len(store.due_task_candidates(svc.clock.now(), 100)) == 1
    monkeypatch.setattr(PostgresUnitOfWork, "add_event", original)
    parallel(lambda: process_due_tasks(store, svc.clock, svc.ids, limit=100))
    with store.transaction(session.workspace_id, write=False) as uow:
        assert sum(e.action is EventAction.PICKUP_DUE for e in uow.list_events()) == 1
        assert uow.list_tasks()[0].status is TaskStatus.DUE


@pytest.mark.parametrize("returning", [False, True])
def test_human_action_after_discovery_prevents_stale_notice(
    store: PostgresStore, returning: bool
) -> None:
    svc, session, eid = workspace(store)
    lid = reserve(svc, session.workspace_id, request(svc, session.workspace_id), eid)
    if returning:
        with store.transaction(session.workspace_id) as uow:
            uc.pick_up_loan(
                svc, uow, loan_id=lid, expected_equipment_version=2, human_approved=True
            )
    clock = Clock(svc.clock.now() + timedelta(hours=2))
    captured = store.due_task_candidates(clock.now(), 100)
    with store.transaction(session.workspace_id) as uow:
        if returning:
            uc.return_loan(svc, uow, loan_id=lid, expected_equipment_version=3, human_approved=True)
        else:
            uc.pick_up_loan(
                svc, uow, loan_id=lid, expected_equipment_version=2, human_approved=True
            )

    class DiscoveredStore(PostgresStore):
        def due_task_candidates(self, now: datetime, limit: int) -> list[DueTaskRef]:
            return captured

    process_due_tasks(DiscoveredStore(store._database_url), clock, svc.ids, limit=100)
    with store.transaction(session.workspace_id, write=False) as uow:
        action = EventAction.RETURN_DUE if returning else EventAction.PICKUP_DUE
        assert not any(e.action is action for e in uow.list_events())


def test_consistent_snapshot_and_lock_timeout(
    store: PostgresStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    connections: list[psycopg.Connection[DictRow]] = []
    original = PostgresStore._connect

    def track(self: PostgresStore) -> psycopg.Connection[DictRow]:
        conn = original(self)
        connections.append(conn)
        return conn

    monkeypatch.setattr(PostgresStore, "_connect", track)
    svc, session, _ = workspace(store)
    with store.transaction(session.workspace_id, write=False) as view:
        assert view.list_requests() == []
        request(svc, session.workspace_id)
        assert view.list_requests() == []
        assert view.list_events() == []
    monkeypatch.setattr(pg, "_LOCK_TIMEOUT_MS", 100)
    with store.transaction(session.workspace_id), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(request, svc, session.workspace_id)
        with pytest.raises(StorageBusyError):
            future.result(timeout=5)
    request(svc, session.workspace_id)
    with (
        pytest.raises(RuntimeError, match="storage operation failed"),
        store.transaction(session.workspace_id, write=False) as view,
    ):
        view.save_idempotency(IdempotencyRecord("forbidden", "create", "hash", 201, "text"))
    assert connections
    assert all(conn.closed for conn in connections)


def test_database_constraints_backstop_bypassed_workflow(
    store: PostgresStore, database_url: str
) -> None:
    svc, session, eid = workspace(store)
    _, other, other_eid = workspace(store)
    rid = request(svc, session.workspace_id)
    lid = reserve(svc, session.workspace_id, rid, eid)
    loan_sql = (
        "INSERT INTO loans (id, workspace_id, request_id, equipment_id, status, due_at, created_at)"
        " VALUES (%s, %s, %s, %s, 'RESERVED', now(), now())"
    )
    with pytest.raises(psycopg.errors.UniqueViolation), psycopg.connect(database_url) as conn:
        conn.execute(loan_sql, (uuid4().hex, session.workspace_id, rid, eid))
    with pytest.raises(psycopg.errors.ForeignKeyViolation), psycopg.connect(database_url) as conn:
        conn.execute(loan_sql, (uuid4().hex, other.workspace_id, rid, other_eid))
    with pytest.raises(psycopg.errors.ForeignKeyViolation), psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO tasks (id, workspace_id, loan_id, kind, status, due_at, created_at)"
            " VALUES (%s, %s, %s, 'RETURN_DUE', 'PENDING', now(), now())",
            (uuid4().hex, other.workspace_id, lid),
        )
    with store.transaction(session.workspace_id, write=False) as uow:
        task = uow.list_tasks()[0]
        assert len(uow.list_loans()) == 1
    with store.transaction(other.workspace_id) as uow:
        assert uow.get_loan(lid) is None
        assert uow.get_task(task.id) is None
        assert not uow.mark_task_due(task.id)
        assert not uow.resolve_task(task.id)
    with store.transaction(session.workspace_id) as uow:
        assert uow.mark_task_due(task.id)
        assert not uow.mark_task_due(task.id)
        assert uow.resolve_task(task.id)
        assert not uow.resolve_task(task.id)


def test_due_candidates_order_and_bound(store: PostgresStore, database_url: str) -> None:
    for _ in range(3):
        svc, session, eid = workspace(store)
        reserve(svc, session.workspace_id, request(svc, session.workspace_id), eid)
    with psycopg.connect(database_url) as conn:
        expected = conn.execute(
            "SELECT workspace_id, id FROM tasks ORDER BY due_at, id LIMIT 2"
        ).fetchall()
    actual = store.due_task_candidates(Clock().now(), 2)
    assert [(ref.workspace_id, ref.task_id) for ref in actual] == expected
    assert store.due_task_candidates(Clock().now() - timedelta(seconds=1), 100) == []
    assert store.due_task_candidates(Clock().now(), 0) == []
    for limit in [-1, 101]:
        with pytest.raises(ValueError, match="limit"):
            store.due_task_candidates(Clock().now(), limit)


def test_failed_migration_rolls_back_schema(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = migrations._MIGRATIONS
    monkeypatch.setattr(migrations, "_MIGRATIONS", (("CREATE TABLE partial (id INT)", "BAD SQL"),))
    with pytest.raises(RuntimeError, match="PostgreSQL migration failed"):
        apply_migrations(database_url)
    assert read_schema_version(database_url) == 0
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT to_regclass('partial')").fetchone() == (None,)
    monkeypatch.setattr(migrations, "_MIGRATIONS", original)
    assert apply_migrations(database_url) == 4


def test_migration_cli_explicit_configuration_and_sanitized_output(database_url: str) -> None:
    env = {**os.environ, "PYTHONPATH": "src"}
    env.pop("BS_POSTGRES_ADMIN_URL", None)

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/postgres_migrate.py"],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    result = run()
    assert result.returncode == 2
    env["BS_POSTGRES_ADMIN_URL"] = "postgresql://user:synthetic-secret@remote/db?sslmode=disable"
    result = run()
    assert result.returncode == 1
    assert "synthetic-secret" not in result.stdout + result.stderr
    env["BS_POSTGRES_ADMIN_URL"] = database_url
    for _ in range(2):
        result = run()
        assert result.returncode == 0
        assert (
            result.stdout.strip()
            == f"PostgreSQL schema version: {migrations.EXPECTED_SCHEMA_VERSION}"
        )
        assert result.stderr == ""


@pytest.mark.parametrize(
    "url",
    [
        "host=remote dbname=x",
        "postgresql:///db",
        "postgresql://remote/db?sslmode=require",
        "postgresql://localhost/db?host=remote",
        "postgresql://localhost/db?hostaddr=8.8.8.8",
    ],
)
def test_insecure_or_ambient_routing_refused(url: str) -> None:
    with pytest.raises(ValueError, match="explicit host URI"):
        PostgresStore(url)
    with pytest.raises(ValueError, match="explicit host URI"):
        apply_migrations(url)
