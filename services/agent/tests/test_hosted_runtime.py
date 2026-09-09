"""Real PostgreSQL HTTP integration tests, lifecycle, idempotency, and concurrency proof."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from borrowed_steps.config import Settings
from borrowed_steps.infrastructure.postgres_migrations import SchemaVersionError
from borrowed_steps.infrastructure.postgres_store import PostgresStore
from borrowed_steps.interfaces.http.app import (
    AGENT_MODE_DISABLED,
    MILESTONE_HOSTED,
    SESSION_COOKIE,
    create_app,
)
from postgres_support import disposable_database, migrated_database

ORIGIN = "https://borrowed-steps.example"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedClock:
        def now(self) -> datetime:
            return datetime(2026, 9, 9, 10, tzinfo=UTC)

    monkeypatch.setattr("borrowed_steps.interfaces.http.app.SystemClock", FixedClock)


def _hosted_settings(db_url: str, **kwargs: object) -> Settings:
    defaults: dict[str, Any] = {
        "db_path": Path("unused.db"),
        "allowed_origins": (ORIGIN,),
        "cookie_secure": True,
        "runtime": "hosted",
        "store": "postgres",
        "database_url": db_url,
        "tasks_enabled": False,
        "assistant_enabled": False,
        "assistant_provider": "groq",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


@pytest.fixture
def pg_url() -> Iterator[str]:
    with migrated_database() as url:
        yield url


def test_hosted_initialization_isolation_and_schema_gates(tmp_path: Path) -> None:
    # 1. Unmigrated database (version 0) refuses startup without creating tables
    with disposable_database() as raw_url:
        settings_raw = _hosted_settings(raw_url)
        with pytest.raises(SchemaVersionError):
            create_app(settings_raw)

        # Verify no tables were created in the database
        with psycopg.connect(raw_url) as conn:
            row = conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            ).fetchone()
            assert row is not None
            assert row[0] == 0

        # 2. Database with newer schema (version 2) refuses startup
        with psycopg.connect(raw_url, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE schema_migrations (version INT PRIMARY KEY, applied_at TIMESTAMPTZ)"
            )
            conn.execute("INSERT INTO schema_migrations VALUES (2, now())")
        with pytest.raises(SchemaVersionError):
            create_app(settings_raw)

    # 3. Migrated database: verify SqliteStore, apply_migrations, TaskRunner never invoked
    with migrated_database() as valid_url:
        dummy_sqlite = tmp_path / "never_created.db"
        settings = _hosted_settings(valid_url, db_path=dummy_sqlite)

        with (
            patch(
                "borrowed_steps.interfaces.http.app.SqliteStore",
                side_effect=AssertionError("SqliteStore must not be called in hosted mode"),
            ),
            patch(
                "borrowed_steps.infrastructure.postgres_migrations.apply_migrations",
                side_effect=AssertionError("apply_migrations must not be called at runtime"),
            ),
            patch(
                "borrowed_steps.interfaces.http.app.TaskRunner",
                side_effect=AssertionError("Hosted mode must not construct TaskRunner"),
            ),
            patch(
                "borrowed_steps.interfaces.http.app._real_interpreter",
                side_effect=AssertionError("Hosted mode must not construct a provider"),
            ),
        ):
            app = create_app(settings)

        assert app.state.tasks is None
        assert not dummy_sqlite.exists()

        # Injected interpreter cannot bypass the hosted disabled gate
        class DummyInterpreter:
            async def interpret(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("Should not run")

        hosted_with_interpreter = create_app(settings, interpreter=DummyInterpreter())  # type: ignore[arg-type]
        with TestClient(hosted_with_interpreter, base_url=ORIGIN) as client:
            with patch.object(
                PostgresStore, "_connect", side_effect=AssertionError("Health must not query DB")
            ):
                resp = client.get("/api/health")
            assert resp.status_code == 200
            assert resp.json()["agent_mode"] == "disabled"
            assert resp.json()["milestone"] == "M3"

            # In hosted mode, the interpreter endpoint refuses even if an interpreter was injected
            ws_res = client.post("/api/workspaces", json={}, headers={"Origin": ORIGIN})
            assert ws_res.status_code == 201
            interpret_res = client.post(
                "/api/intake/interpret",
                json={"text": "I need help"},
                headers={"Origin": ORIGIN},
            )
            assert interpret_res.status_code == 503
            assert interpret_res.json()["error"]["code"] == "ASSISTANT_DISABLED"


def test_hosted_http_full_lifecycle(pg_url: str) -> None:
    settings = _hosted_settings(pg_url)
    app = create_app(settings)

    with TestClient(app, base_url=ORIGIN) as client:
        # Health
        health_resp = client.get("/api/health")
        assert health_resp.status_code == 200
        assert health_resp.json() == {
            "status": "ok",
            "milestone": MILESTONE_HOSTED,
            "agent_mode": AGENT_MODE_DISABLED,
        }

        # Create workspace: verify cookie flags and synthetic state
        ws_resp = client.post(
            "/api/workspaces",
            json={},
            headers={"Origin": ORIGIN},
        )
        assert ws_resp.status_code == 201
        data = ws_resp.json()
        assert "workspace" in data
        assert "snapshot" in data
        assert data["snapshot"]["agent_mode"] == "disabled"
        assert len(data["snapshot"]["equipment"]) == 3

        # Verify Set-Cookie header contains Secure, HttpOnly, SameSite=lax
        cookie_header = ws_resp.headers.get("set-cookie", "")
        assert SESSION_COOKIE in cookie_header
        assert "secure" in cookie_header.lower()
        assert "httponly" in cookie_header.lower()
        assert "samesite=lax" in cookie_header.lower()

        assert SESSION_COOKIE in client.cookies
        auth_headers = {"Origin": ORIGIN, "Idempotency-Key": "req_key_001"}

        # Missing origin rejected
        bad_origin_resp = client.post(
            "/api/requests",
            json={
                "borrower_label": "Alice",
                "equipment_kind": "WHEELCHAIR",
                "pickup_location": "Room 101",
                "due_at": "2026-09-10T12:00:00Z",
            },
            headers={"Origin": "https://malicious.example", "Idempotency-Key": "req_key_001"},
        )
        assert bad_origin_resp.status_code == 403
        assert bad_origin_resp.json()["error"]["code"] == "ORIGIN_FORBIDDEN"

        # Create structured request
        req_resp = client.post(
            "/api/requests",
            json={
                "borrower_label": "Alice",
                "equipment_kind": "WHEELCHAIR",
                "pickup_location": "Room 101",
                "due_at": "2026-09-10T12:00:00Z",
            },
            headers=auth_headers,
        )
        assert req_resp.status_code == 200
        req_id = req_resp.json()["request"]["id"]

        available_items = [e for e in data["snapshot"]["equipment"] if e["state"] == "AVAILABLE"]
        eq_id = available_items[0]["id"]

        # Human approval refusal
        refused_resp = client.post(
            "/api/reservations",
            json={
                "request_id": req_id,
                "equipment_id": eq_id,
                "expected_equipment_version": 1,
                "human_approved": False,
            },
            headers={"Origin": ORIGIN, "Idempotency-Key": "res_key_refused"},
        )
        assert refused_resp.status_code == 422
        assert refused_resp.json()["error"]["code"] == "APPROVAL_REQUIRED"

        # Valid reservation with human_approved=True
        reserve_resp = client.post(
            "/api/reservations",
            json={
                "request_id": req_id,
                "equipment_id": eq_id,
                "expected_equipment_version": 1,
                "human_approved": True,
            },
            headers={"Origin": ORIGIN, "Idempotency-Key": "res_key_001"},
        )
        assert reserve_resp.status_code == 200
        loan_id = reserve_resp.json()["loan"]["id"]

        # Pickup loan
        pickup_resp = client.post(
            f"/api/loans/{loan_id}/pickup",
            json={"expected_equipment_version": 2, "human_approved": True},
            headers={"Origin": ORIGIN, "Idempotency-Key": "pickup_key_001"},
        )
        assert pickup_resp.status_code == 200
        assert pickup_resp.json()["equipment"]["state"] == "ON_LOAN"

        # Return loan
        return_resp = client.post(
            f"/api/loans/{loan_id}/return",
            json={"expected_equipment_version": 3, "human_approved": True},
            headers={"Origin": ORIGIN, "Idempotency-Key": "return_key_001"},
        )
        assert return_resp.status_code == 200
        assert return_resp.json()["equipment"]["state"] == "AWAITING_INSPECTION"

        # Inspect item (transition to QUARANTINED)
        inspect_resp = client.post(
            f"/api/equipment/{eq_id}/inspection",
            json={
                "outcome": "QUARANTINED",
                "expected_equipment_version": 4,
                "human_approved": True,
            },
            headers={"Origin": ORIGIN, "Idempotency-Key": "inspect_key_001"},
        )
        assert inspect_resp.status_code == 200
        assert inspect_resp.json()["equipment"]["state"] == "QUARANTINED"

        # Verify full persisted snapshot
        snap_resp = client.get("/api/snapshot")
        assert snap_resp.status_code == 200
        snap = snap_resp.json()
        assert any(e["id"] == eq_id and e["state"] == "QUARANTINED" for e in snap["equipment"])
        assert len(snap["events"]) == 5

        # Disabled assistant returns 503 without mutation
        interpret_resp = client.post(
            "/api/intake/interpret",
            json={"text": "I need a wheelchair please"},
            headers={"Origin": ORIGIN},
        )
        assert interpret_resp.status_code == 503
        assert interpret_resp.json()["error"]["code"] == "ASSISTANT_DISABLED"
        assert client.get("/api/snapshot").json() == snap


def test_session_isolation_and_foreign_id_protection(pg_url: str) -> None:
    settings = _hosted_settings(pg_url)
    app = create_app(settings)

    with (
        TestClient(app, base_url=ORIGIN) as client_a,
        TestClient(app, base_url=ORIGIN) as client_b,
    ):
        # Create workspace A
        res_a = client_a.post("/api/workspaces", json={}, headers={"Origin": ORIGIN})
        assert res_a.status_code == 201
        eq_a_id = res_a.json()["snapshot"]["equipment"][0]["id"]

        # Create workspace B
        res_b = client_b.post("/api/workspaces", json={}, headers={"Origin": ORIGIN})
        assert res_b.status_code == 201

        # No cookie returns 401
        with TestClient(app, base_url=ORIGIN) as anonymous:
            unauth = anonymous.get("/api/snapshot")
            assert unauth.status_code == 401
            assert unauth.json()["error"]["code"] == "SESSION_REQUIRED"

        # Mutating workspace B using foreign equipment from workspace A fails
        req_b = client_b.post(
            "/api/requests",
            json={
                "borrower_label": "Bob",
                "equipment_kind": "WHEELCHAIR",
                "pickup_location": "Room B",
                "due_at": "2026-09-10T12:00:00Z",
            },
            headers={"Origin": ORIGIN, "Idempotency-Key": "key_b_001"},
        )
        assert req_b.status_code == 200
        req_b_id = req_b.json()["request"]["id"]

        foreign_res = client_b.post(
            "/api/reservations",
            json={
                "request_id": req_b_id,
                "equipment_id": eq_a_id,  # Equipment belongs to workspace A!
                "expected_equipment_version": 1,
                "human_approved": True,
            },
            headers={"Origin": ORIGIN, "Idempotency-Key": "res_foreign_001"},
        )
        assert foreign_res.status_code == 404
        assert foreign_res.json()["error"]["code"] == "NOT_FOUND"


def test_fresh_instance_persistence_and_exact_idempotent_replay(pg_url: str) -> None:
    settings = _hosted_settings(pg_url)
    app1 = create_app(settings)

    session_cookie: str
    original_bytes: bytes

    with TestClient(app1, base_url=ORIGIN) as client1:
        ws_res = client1.post("/api/workspaces", json={}, headers={"Origin": ORIGIN})
        assert ws_res.status_code == 201
        session_cookie = client1.cookies[SESSION_COOKIE]

        req_res = client1.post(
            "/api/requests",
            json={
                "borrower_label": "Carol",
                "equipment_kind": "WALKER",
                "pickup_location": "Desk 4",
                "due_at": "2026-09-11T10:00:00Z",
            },
            headers={"Origin": ORIGIN, "Idempotency-Key": "idemp_test_key_999"},
        )
        assert req_res.status_code == 200
        original_bytes = req_res.content

    # Discard app1 and instantiate app2 on the same PostgreSQL database
    app2 = create_app(settings)
    with TestClient(app2, base_url=ORIGIN, cookies={SESSION_COOKIE: session_cookie}) as client2:
        # 1. Exact replay with identical key and payload: returns byte-identical response
        replay_res = client2.post(
            "/api/requests",
            json={
                "borrower_label": "Carol",
                "equipment_kind": "WALKER",
                "pickup_location": "Desk 4",
                "due_at": "2026-09-11T10:00:00Z",
            },
            headers={"Origin": ORIGIN, "Idempotency-Key": "idemp_test_key_999"},
        )
        assert replay_res.status_code == 200
        assert replay_res.content == original_bytes

        # 2. Conflict replay with same key but different payload
        conflict_res = client2.post(
            "/api/requests",
            json={
                "borrower_label": "Different Person",
                "equipment_kind": "WALKER",
                "pickup_location": "Desk 4",
                "due_at": "2026-09-11T10:00:00Z",
            },
            headers={"Origin": ORIGIN, "Idempotency-Key": "idemp_test_key_999"},
        )
        assert conflict_res.status_code == 409
        assert conflict_res.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_concurrent_competing_allocators(pg_url: str) -> None:
    """Two concurrent HTTP clients attempting to allocate the same item: exactly one wins."""
    settings = _hosted_settings(pg_url)
    app = create_app(settings)

    # Setup: create workspace, 2 requests for wheelchairs, find the available wheelchair
    with TestClient(app, base_url=ORIGIN) as init_client:
        ws_res = init_client.post("/api/workspaces", json={}, headers={"Origin": ORIGIN})
        assert ws_res.status_code == 201
        session_id = init_client.cookies[SESSION_COOKIE]
        ws_id = ws_res.json()["workspace"]["id"]
        equipment = [e for e in ws_res.json()["snapshot"]["equipment"] if e["kind"] == "WHEELCHAIR"]
        target_eq_id = equipment[0]["id"]

        req1_res = init_client.post(
            "/api/requests",
            json={
                "borrower_label": "Rider 1",
                "equipment_kind": "WHEELCHAIR",
                "pickup_location": "Bay A",
                "due_at": "2026-09-12T10:00:00Z",
            },
            headers={"Origin": ORIGIN, "Idempotency-Key": "req_key_r1"},
        )
        assert req1_res.status_code == 200, req1_res.text
        req1 = req1_res.json()["request"]["id"]

        req2_res = init_client.post(
            "/api/requests",
            json={
                "borrower_label": "Rider 2",
                "equipment_kind": "WHEELCHAIR",
                "pickup_location": "Bay B",
                "due_at": "2026-09-12T10:00:00Z",
            },
            headers={"Origin": ORIGIN, "Idempotency-Key": "req_key_r2"},
        )
        assert req2_res.status_code == 200, req2_res.text
        req2 = req2_res.json()["request"]["id"]

    barrier = Barrier(2)

    def allocate(req_id: str, key: str) -> tuple[int, str]:
        with TestClient(app, base_url=ORIGIN, cookies={SESSION_COOKIE: session_id}) as client:
            barrier.wait(timeout=10)
            res = client.post(
                "/api/reservations",
                json={
                    "request_id": req_id,
                    "equipment_id": target_eq_id,
                    "expected_equipment_version": 1,
                    "human_approved": True,
                },
                headers={"Origin": ORIGIN, "Idempotency-Key": key},
            )
            return res.status_code, res.json().get("error", {}).get("code", "SUCCESS")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(allocate, req1, "alloc_key_1")
        f2 = pool.submit(allocate, req2, "alloc_key_2")
        results = [f1.result(timeout=15), f2.result(timeout=15)]

    statuses = sorted(r[0] for r in results)
    assert statuses == [200, 409], f"Expected exactly one 200 and one 409, got: {results}"

    # Verify against PostgreSQL directly: exactly one loan was created
    store = PostgresStore(pg_url)
    with store.transaction(ws_id, write=False) as uow:
        loans = uow.list_loans()
        assert len(loans) == 1
        assert loans[0].equipment_id == target_eq_id


def test_concurrent_identical_idempotency_envelopes(pg_url: str) -> None:
    """Two concurrent HTTP clients sending identical idempotency envelope:

    both 200, byte-identical.
    """
    settings = _hosted_settings(pg_url)
    app = create_app(settings)

    with TestClient(app, base_url=ORIGIN) as init_client:
        ws_res = init_client.post("/api/workspaces", json={}, headers={"Origin": ORIGIN})
        assert ws_res.status_code == 201
        session_id = init_client.cookies[SESSION_COOKIE]
        ws_id = ws_res.json()["workspace"]["id"]

    barrier = Barrier(2)
    shared_key = "concurrent_key_" + uuid4().hex
    shared_body = {
        "borrower_label": "Identical Borrower",
        "equipment_kind": "CRUTCHES",
        "pickup_location": "Front Door",
        "due_at": "2026-09-15T15:00:00Z",
    }

    def send_mutation() -> tuple[int, bytes]:
        with TestClient(app, base_url=ORIGIN, cookies={SESSION_COOKIE: session_id}) as client:
            barrier.wait(timeout=10)
            res = client.post(
                "/api/requests",
                json=shared_body,
                headers={"Origin": ORIGIN, "Idempotency-Key": shared_key},
            )
            return res.status_code, res.content

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(send_mutation)
        f2 = pool.submit(send_mutation)
        status1, body1 = f1.result(timeout=15)
        status2, body2 = f2.result(timeout=15)

    assert status1 == 200
    assert status2 == 200
    assert body1 == body2, (
        "Concurrent identical idempotency requests must produce byte-identical response"
    )

    # Verify database: exactly 1 request created, exactly 1 idempotency record
    store = PostgresStore(pg_url)
    with store.transaction(ws_id, write=False) as uow:
        reqs = [r for r in uow.list_requests() if r.borrower_label == "Identical Borrower"]
        assert len(reqs) == 1
        assert len(uow.list_events()) == 1
        record = uow.get_idempotency(shared_key)
        assert record is not None
        assert record.response_body.encode("utf-8") == body1
