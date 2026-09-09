"""Independent transaction failure and discovery/action interleaving checks."""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from borrowed_steps.application.coordination import process_due_tasks
from borrowed_steps.application.ports import DueTaskRef
from borrowed_steps.domain.models import Event
from borrowed_steps.infrastructure.sqlite_store import SqliteStore, SqliteUnitOfWork
from borrowed_steps.infrastructure.system import SecretsIdGenerator
from conftest import FakeClock
from support import snapshot, transition
from test_coordination_processing import _reserve


def test_due_event_failure_rolls_back_task(
    client: TestClient, clock: FakeClock, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reserve(client)
    before = snapshot(client)

    def fail(self: SqliteUnitOfWork, event: Event) -> None:
        raise RuntimeError("injected event write failure")

    with monkeypatch.context() as patch:
        patch.setattr(SqliteUnitOfWork, "add_event", fail)
        with pytest.raises(RuntimeError, match="injected"):
            process_due_tasks(SqliteStore(db_path), clock, SecretsIdGenerator(), limit=100)
    assert snapshot(client) == before


def test_pickup_between_discovery_and_claim_wins(
    client: TestClient, clock: FakeClock, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reserved = _reserve(client)
    store = SqliteStore(db_path)
    discover = store.due_task_candidates

    def interleave(self: SqliteStore, now: datetime, limit: int) -> list[DueTaskRef]:
        found = discover(now, limit)
        response = transition(
            client,
            loan_id=reserved["loan"]["id"],
            action="pickup",
            expected_version=reserved["equipment"]["version"],
        )
        assert response.status_code == 200
        return found

    monkeypatch.setattr(SqliteStore, "due_task_candidates", interleave)
    report = process_due_tasks(store, clock, SecretsIdGenerator(), limit=100)
    assert report.marked_due == 0
    result = snapshot(client)
    assert result["loans"][0]["status"] == "ON_LOAN"
    assert "PICKUP_DUE" not in [e["action"] for e in result["events"]]
    assert next(t for t in result["tasks"] if t["kind"] == "PICKUP_DUE")["status"] == "RESOLVED"
