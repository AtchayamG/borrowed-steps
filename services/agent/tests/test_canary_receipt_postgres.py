"""Actual PostgreSQL receipt proof: races, expiry, recovery and process restart."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from borrowed_steps.infrastructure.canary_receipt import (
    CanaryReceiptError,
    CanaryReceiptStore,
    CanaryState,
    FailureCode,
)
from borrowed_steps.infrastructure.postgres_migrations import (
    _SCHEMA_V1,
    _SCHEMA_V2,
    apply_migrations,
)
from postgres_support import disposable_database, migrated_database
from test_canary_receipt import candidate


@pytest.fixture
def db() -> Iterator[str]:
    with migrated_database() as url:
        yield url


def test_replay_dispatch_terminal_and_reused_authorization(db: str) -> None:
    store = CanaryReceiptStore(db)
    req = candidate()
    row = store.reserve(req)
    assert store.reserve(req) == row
    with pytest.raises(CanaryReceiptError, match="conflict"):
        store.reserve(replace(req, owner_id=str(uuid4())))
    with pytest.raises(CanaryReceiptError):
        store.mark_dispatched(req.receipt_id, str(uuid4()))
    store.mark_dispatched(req.receipt_id, req.owner_id)
    with pytest.raises(CanaryReceiptError, match="consumed"):
        store.mark_dispatched(req.receipt_id, req.owner_id)
    result = store.finish(req.receipt_id, req.owner_id, CanaryState.SUCCEEDED, actual_sends=3)
    assert result["actual_total_tokens"] is None
    assert (result["reserved_sends"], result["reserved_output_tokens"]) == (6, 6144)
    assert (
        store.finish(req.receipt_id, req.owner_id, CanaryState.SUCCEEDED, actual_sends=3) == result
    )
    with pytest.raises(CanaryReceiptError, match="conflict"):
        store.finish(req.receipt_id, req.owner_id, CanaryState.SUCCEEDED, actual_sends=2)
    with pytest.raises(CanaryReceiptError, match="consumed"):
        store.reserve(replace(req, receipt_id=str(uuid4())))
    assert store.reserve(candidate())["state"] == "RESERVED"


def test_real_concurrent_duplicate_and_dispatch(db: str) -> None:
    req = candidate()
    barrier = Barrier(6)

    def reserve() -> str:
        barrier.wait(timeout=10)
        return str(CanaryReceiptStore(db).reserve(req)["receipt_id"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(lambda _: reserve(), range(6))) == [req.receipt_id] * 6

    def dispatch() -> str:
        barrier.wait(timeout=10)
        try:
            CanaryReceiptStore(db).mark_dispatched(req.receipt_id, req.owner_id)
            return "winner"
        except CanaryReceiptError:
            return "refused"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: dispatch(), range(6)))
    assert results.count("winner") == 1
    assert results.count("refused") == 5


def test_real_concurrent_distinct_receipts(db: str) -> None:
    barrier = Barrier(6)

    def reserve() -> str:
        barrier.wait(timeout=10)
        try:
            CanaryReceiptStore(db).reserve(candidate())
            return "winner"
        except CanaryReceiptError:
            return "refused"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: reserve(), range(6)))
    assert results.count("winner") == 1
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM canary_receipts").fetchone() == (1,)


@pytest.mark.parametrize("starting", ["RESERVED", "DISPATCHED", "UNCERTAIN"])
def test_dead_execution_recovery_keeps_unknowns_and_reservations(db: str, starting: str) -> None:
    store = CanaryReceiptStore(db)
    req = candidate()
    store.reserve(req)
    if starting != "RESERVED":
        store.mark_dispatched(req.receipt_id, req.owner_id)
    if starting == "UNCERTAIN":
        row = store.finish(
            req.receipt_id,
            req.owner_id,
            CanaryState.UNCERTAIN,
            failure_code=FailureCode.EXECUTION_UNKNOWN,
        )
        assert row["actual_sends"] is None
        assert row["actual_total_tokens"] is None
    with pytest.raises(CanaryReceiptError):
        store.reserve(candidate())
    operator = str(uuid4())
    with pytest.raises(CanaryReceiptError):
        store.recover_dead_execution(req.receipt_id, req.owner_id, operator_id=operator)
    with pytest.raises(CanaryReceiptError):
        store.recover_dead_execution(
            req.receipt_id, req.owner_id, operator_id="", confirmed_dead=True
        )
    row = store.recover_dead_execution(
        req.receipt_id, req.owner_id, operator_id=operator, confirmed_dead=True
    )
    assert row["state"] == "UNCERTAIN"
    assert not row["concurrency_active"]
    assert row["actual_sends"] is None
    assert row["actual_total_tokens"] is None
    assert row["reserved_sends"] == 6
    assert row["reserved_output_tokens"] == 6144
    assert (
        store.recover_dead_execution(
            req.receipt_id, req.owner_id, operator_id=operator, confirmed_dead=True
        )
        == row
    )
    with pytest.raises(CanaryReceiptError):
        store.mark_dispatched(req.receipt_id, req.owner_id)
    with pytest.raises(CanaryReceiptError):
        store.reserve(replace(req, receipt_id=str(uuid4())))
    assert store.reserve(candidate())["state"] == "RESERVED"


def test_expiry_and_read_only_replay(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    req = candidate()
    store = CanaryReceiptStore(db)
    with pytest.raises(CanaryReceiptError, match="expired"):
        store.reserve(replace(req, authorization_expires_at=datetime.now(UTC) - timedelta(days=1)))
    original = store.reserve(req)
    # Deterministic exact expiry boundary; real SQL storage with injected DB-clock result.
    monkeypatch.setattr(
        CanaryReceiptStore, "_now", staticmethod(lambda _: req.authorization_expires_at)
    )
    with pytest.raises(CanaryReceiptError, match="expired"):
        store.mark_dispatched(req.receipt_id, req.owner_id)
    assert store.reserve(req) == original
    assert store.get_receipt(req.receipt_id)["state"] == "RESERVED"  # type: ignore[index]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"actual_sends": -1},
        {"actual_sends": 7},
        {"actual_sends": True},
        {"actual_total_tokens": -1},
        {"actual_total_tokens": True},
        {"actual_total_tokens": 2147483648},
        {"failure_code": "secret raw response"},
    ],
)
def test_invalid_usage_does_not_settle(db: str, kwargs: dict[str, Any]) -> None:
    req = candidate()
    store = CanaryReceiptStore(db)
    store.reserve(req)
    store.mark_dispatched(req.receipt_id, req.owner_id)
    with pytest.raises(CanaryReceiptError):
        store.finish(req.receipt_id, req.owner_id, CanaryState.FAILED_CONFIRMED, **kwargs)
    row = store.get_receipt(req.receipt_id)
    assert row is not None
    assert row["state"] == "DISPATCHED"


def test_failed_confirmed_unknown_is_not_zero(db: str) -> None:
    req = candidate()
    store = CanaryReceiptStore(db)
    store.reserve(req)
    store.mark_dispatched(req.receipt_id, req.owner_id)
    row = store.finish(
        req.receipt_id,
        req.owner_id,
        CanaryState.FAILED_CONFIRMED,
        failure_code=FailureCode.PROVIDER_FAILURE,
    )
    assert row["actual_total_tokens"] is None
    assert row["actual_sends"] is None
    assert not row["concurrency_active"]
    assert row["reserved_output_tokens"] == 6144


def test_persisted_receipt_read_by_fresh_process(db: str) -> None:
    req = candidate()
    store = CanaryReceiptStore(db)
    store.reserve(req)
    store.mark_dispatched(req.receipt_id, req.owner_id)
    code = """import sys,json
from borrowed_steps.infrastructure.canary_receipt import CanaryReceiptStore
r=CanaryReceiptStore(sys.argv[1]).get_receipt(sys.argv[2])
print(json.dumps([r['state'],r['reserved_sends'],r['reserved_output_tokens']]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, db, req.receipt_id],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
        env=os.environ.copy(),
    )
    assert json.loads(result.stdout) == ["DISPATCHED", 6, 6144]
    with pytest.raises(CanaryReceiptError):
        CanaryReceiptStore(db).mark_dispatched(req.receipt_id, req.owner_id)


def test_v2_upgrade_preserves_business_rows_and_is_repeatable() -> None:
    with disposable_database() as url:
        with psycopg.connect(url) as conn:
            conn.execute(
                "CREATE TABLE schema_migrations"
                "(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ)"
            )
            for statement in (*_SCHEMA_V1, *_SCHEMA_V2):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES(1,now()),(2,now())")
            conn.execute("INSERT INTO workspaces(id,created_at) VALUES('preserved',now())")
        assert apply_migrations(url) == 3
        assert apply_migrations(url) == 3
        with psycopg.connect(url) as conn:
            assert conn.execute("SELECT id FROM workspaces").fetchall() == [("preserved",)]
            assert conn.execute("SELECT count(*) FROM canary_receipts").fetchone() == (0,)


def test_database_constraints_and_connection_closure(db: str) -> None:
    store = CanaryReceiptStore(db)
    req = candidate()
    store.reserve(req)
    with psycopg.connect(db) as conn:
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE canary_receipts SET reserved_sends=1")
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE canary_receipts SET concurrency_active=FALSE")
        assert conn.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname=current_database() AND pid<>pg_backend_pid()"
        ).fetchone() == (0,)
