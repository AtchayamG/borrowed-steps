"""Comprehensive tests for hosted scheduler HTTP packaging and authentication.

Conforms to docs/M3_SCHEDULER_HTTP_CONTRACT.md:
- Cold-start zero-connection authentication verification for tick and status
- Bearer token strictness: duplicate headers, cookies, query/body substitutes, malformed tokens
- Real PostgreSQL execution: authorized tick, fresh-app status, busy/duplicate, partial, failure
- Zero body/query influence on scheduler options
- Absence from OpenAPI, exclusion from local mode, and Cache-Control: no-store headers
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from borrowed_steps.config import Settings
from borrowed_steps.infrastructure.hosted_scheduler import HostedSchedulerService
from borrowed_steps.infrastructure.postgres_store import PostgresStore, PostgresUnitOfWork
from borrowed_steps.interfaces.http.app import (
    SESSION_COOKIE,
    create_app,
)
from postgres_support import disposable_database, migrated_database
from test_hosted_scheduler import _create_loan_with_due_task, _seed_workspace

ORIGIN = "https://borrowed-steps.example"
VALID_TICK_TOKEN = "a" * 32 + "-secret-task-tick-token-12345"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedClock:
        def now(self) -> datetime:
            return datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)

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
        "task_tick_token": VALID_TICK_TOKEN,
    }
    defaults.update(kwargs)
    return Settings(**defaults)


@pytest.fixture
def pg_url() -> Iterator[str]:
    with migrated_database() as url:
        yield url


# ==============================================================================
# 1. Cold-Start Zero-Connection Authentication Proofs
# ==============================================================================


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/internal/tasks/tick", "post"),
        ("/api/internal/tasks/status", "get"),
    ],
)
def test_unauthenticated_cold_start_causes_zero_database_connections(
    pg_url: str, path: str, method: str
) -> None:
    """Instrument psycopg.connect before app construction through unauthorized requests.

    Proves that missing config, missing auth, bad token, duplicate headers,
    cookie-only auth, or body/query tokens cause ZERO DB connections.
    """
    real_connect = psycopg.connect
    connect_calls = 0

    def instrumented_connect(*args: object, **kwargs: object) -> object:
        nonlocal connect_calls
        connect_calls += 1
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    # 1. Missing configured token -> 503 before any DB connection
    with patch("psycopg.connect", side_effect=instrumented_connect):
        settings_no_token = _hosted_settings(pg_url, task_tick_token=None)
        app_no_token = create_app(settings_no_token)
        with TestClient(app_no_token, base_url=ORIGIN) as client:
            resp = getattr(client, method)(
                path,
                headers={"Authorization": f"Bearer {VALID_TICK_TOKEN}"},
            )
            assert resp.status_code == 503
            assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
            assert resp.headers.get("cache-control") == "no-store"
            assert connect_calls == 0

    # 2. Configured token present, but unauthorized / bad requests
    settings = _hosted_settings(pg_url)
    with patch("psycopg.connect", side_effect=instrumented_connect):
        app = create_app(settings)
        assert connect_calls == 0, "create_app must not perform DB operations"

        with TestClient(app, base_url=ORIGIN) as client:
            # A. Missing Authorization header
            resp = getattr(client, method)(path)
            assert resp.status_code == 401
            assert resp.json()["error"]["code"] == "UNAUTHORIZED"
            assert resp.headers.get("cache-control") == "no-store"
            assert connect_calls == 0

            # B. Wrong token
            wrong_token = "w" * 32 + "-wrong-token-value-0000"
            resp = getattr(client, method)(
                path,
                headers={"Authorization": f"Bearer {wrong_token}"},
            )
            assert resp.status_code == 401
            assert resp.json()["error"]["code"] == "UNAUTHORIZED"
            assert resp.headers.get("cache-control") == "no-store"
            assert connect_calls == 0

            # C. Malformed Authorization headers
            for bad_header in (
                "Basic dXNlcjpwYXNz",
                "Bearer",
                "Bearer ",
                f"bearer {VALID_TICK_TOKEN}",  # lowercase
                f"Token {VALID_TICK_TOKEN}",
                "Bearer short",  # < 32 chars
                f"Bearer {'x' * 257}",  # > 256 chars
                f"Bearer {VALID_TICK_TOKEN} trailing-junk",
                f"Bearer {VALID_TICK_TOKEN}@bad!",
            ):
                resp = getattr(client, method)(path, headers={"Authorization": bad_header})
                assert resp.status_code == 401
                assert resp.headers.get("cache-control") == "no-store"
                assert connect_calls == 0

            # D. Duplicate Authorization headers
            resp = getattr(client, method)(
                path,
                headers=[
                    ("authorization", f"Bearer {VALID_TICK_TOKEN}"),
                    ("authorization", f"Bearer {VALID_TICK_TOKEN}"),
                ],
            )
            assert resp.status_code == 401
            assert resp.headers.get("cache-control") == "no-store"
            assert connect_calls == 0

            # E. Cookie-only auth (bs_session or token cookie is not a substitute)
            client.cookies.set(SESSION_COOKIE, "dummy-session-id")
            client.cookies.set("token", VALID_TICK_TOKEN)
            resp = getattr(client, method)(path)
            assert resp.status_code == 401
            assert resp.headers.get("cache-control") == "no-store"
            assert connect_calls == 0

            # F. Query or body token parameters are not substitutes
            if method == "post":
                resp = client.post(
                    f"{path}?token={VALID_TICK_TOKEN}", json={"token": VALID_TICK_TOKEN}
                )
            else:
                resp = client.get(f"{path}?token={VALID_TICK_TOKEN}")
            assert resp.status_code == 401
            assert resp.headers.get("cache-control") == "no-store"
            assert connect_calls == 0


# ==============================================================================
# 2. Real PostgreSQL Execution Proofs
# ==============================================================================


def test_authorized_tick_and_fresh_app_persisted_status(pg_url: str) -> None:
    """Execute real authorized tick, verify response shape and fresh-instance status read."""
    settings = _hosted_settings(pg_url)
    app = create_app(settings)
    auth_header = {"Authorization": f"Bearer {VALID_TICK_TOKEN}"}

    with TestClient(app, base_url=ORIGIN) as client:
        # Initial status before any tick
        init_status = client.get("/api/internal/tasks/status", headers=auth_header)
        assert init_status.status_code == 200
        assert init_status.headers.get("cache-control") == "no-store"
        data = init_status.json()
        assert data["status"] == "never_run"
        assert data["last_outcome"] == "never_run"
        assert data["last_success_at"] is None
        assert data["counts_complete"] is True
        assert data["counts"]["considered"] == 0

        # Execute tick
        tick_resp = client.post("/api/internal/tasks/tick", headers=auth_header)
        assert tick_resp.status_code == 200
        assert tick_resp.headers.get("cache-control") == "no-store"
        tick_data = tick_resp.json()
        assert tick_data["outcome"] == "success"
        assert tick_data["capacity_limited"] is False
        assert "run_id" not in tick_data
        assert "error" not in tick_data
        assert tick_data["report"] == {
            "considered": 0,
            "marked_due": 0,
            "resolved_stale": 0,
            "unchanged": 0,
            "contended": 0,
            "stopped_early": False,
        }

    # Fresh application instance pointing to the same database
    fresh_app = create_app(settings)
    with TestClient(fresh_app, base_url=ORIGIN) as fresh_client:
        status_resp = fresh_client.get("/api/internal/tasks/status", headers=auth_header)
        assert status_resp.status_code == 200
        assert status_resp.headers.get("cache-control") == "no-store"
        status_data = status_resp.json()
        assert status_data["status"] == "success"
        assert status_data["last_outcome"] == "success"
        assert status_data["last_completed_at"] is not None
        assert status_data["last_success_at"] is not None
        assert status_data["last_success_recent"] is True
        assert status_data["stale_warning"] is False
        assert status_data["active_run_started_at"] is None
        assert status_data["active_run_expires_at"] is None
        assert status_data["counts_complete"] is True
        assert "run_id" not in status_data
        assert "active_run_id" not in status_data
        assert "last_run_id" not in status_data


def test_concurrent_or_busy_tick_returns_409(pg_url: str) -> None:
    """When an active unexpired lease is held, tick returns 409 busy."""
    settings = _hosted_settings(pg_url)
    app = create_app(settings)
    auth_header = {"Authorization": f"Bearer {VALID_TICK_TOKEN}"}

    # Simulate an active unexpired lease directly in PostgreSQL
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE scheduler_control SET "
            "active_run_id = 'competing-run-id', "
            "active_run_started_at = now(), "
            "active_run_expires_at = now() + interval '60 seconds' "
            "WHERE id = 1"
        )

    with TestClient(app, base_url=ORIGIN) as client:
        resp = client.post("/api/internal/tasks/tick", headers=auth_header)
        assert resp.status_code == 409
        assert resp.headers.get("cache-control") == "no-store"
        payload = resp.json()
        assert payload["outcome"] == "busy"
        assert payload["capacity_limited"] is False
        assert payload["report"] is None
        assert "run_id" not in payload

        # Status endpoint reflects 'running'
        status_resp = client.get("/api/internal/tasks/status", headers=auth_header)
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "running"
        assert "competing-run-id" not in status_resp.text


def test_tick_evidence_write_failure_returns_503(pg_url: str) -> None:
    """Evidence-write failure never returns success; maps to 503."""
    settings = _hosted_settings(pg_url)
    app = create_app(settings)
    auth_header = {"Authorization": f"Bearer {VALID_TICK_TOKEN}"}

    with (
        patch.object(
            HostedSchedulerService,
            "tick",
            side_effect=RuntimeError("Failed to persist scheduler execution outcome"),
        ),
        TestClient(app, base_url=ORIGIN) as client,
    ):
        resp = client.post("/api/internal/tasks/tick", headers=auth_header)
        assert resp.status_code == 503
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


def test_status_read_error_returns_503(pg_url: str) -> None:
    """Status read failures return generic 503, never a fabricated healthy status."""
    settings = _hosted_settings(pg_url)
    app = create_app(settings)
    auth_header = {"Authorization": f"Bearer {VALID_TICK_TOKEN}"}

    with (
        patch.object(
            HostedSchedulerService,
            "read_status",
            side_effect=RuntimeError("Database query timed out"),
        ),
        TestClient(app, base_url=ORIGIN) as client,
    ):
        resp = client.get("/api/internal/tasks/status", headers=auth_header)
        assert resp.status_code == 503
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


def test_unmigrated_or_future_schema_gates_on_operational_endpoints() -> None:
    """Schema 0 or future schema refusal returns 503 on operational endpoints."""
    # 1. Unmigrated database (version 0)
    with disposable_database() as raw_url:
        settings_raw = _hosted_settings(raw_url)
        app_raw = create_app(settings_raw)
        auth_header = {"Authorization": f"Bearer {VALID_TICK_TOKEN}"}

        with TestClient(app_raw, base_url=ORIGIN) as client:
            # Tick returns 503
            tick_res = client.post("/api/internal/tasks/tick", headers=auth_header)
            assert tick_res.status_code == 503
            assert tick_res.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
            assert tick_res.headers.get("cache-control") == "no-store"

            # Status returns 503
            status_res = client.get("/api/internal/tasks/status", headers=auth_header)
            assert status_res.status_code == 503
            assert status_res.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
            assert status_res.headers.get("cache-control") == "no-store"

        # Verify no tables were created
        with psycopg.connect(raw_url) as conn:
            row = conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            ).fetchone()
            assert row is not None
            assert row[0] == 0

        # 2. Database with future schema (version 3)
        with psycopg.connect(raw_url, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE schema_migrations (version INT PRIMARY KEY, applied_at TIMESTAMPTZ)"
            )
            conn.execute("INSERT INTO schema_migrations VALUES (3, now())")

        app_v3 = create_app(settings_raw)
        with TestClient(app_v3, base_url=ORIGIN) as client_v3:
            tick_v3 = client_v3.post("/api/internal/tasks/tick", headers=auth_header)
            assert tick_v3.status_code == 503
            assert tick_v3.json()["error"]["code"] == "SERVICE_UNAVAILABLE"

            status_v3 = client_v3.get("/api/internal/tasks/status", headers=auth_header)
            assert status_v3.status_code == 503
            assert status_v3.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


# ==============================================================================
# 3. Parameters Cannot Change Scheduler Limits or Scope
# ==============================================================================


def test_body_and_query_parameters_cannot_change_scheduler_options(pg_url: str) -> None:
    """Inject scheduler instrumentation to prove body/query params cannot alter limits or scope."""
    settings = _hosted_settings(pg_url)
    app = create_app(settings)
    auth_header = {"Authorization": f"Bearer {VALID_TICK_TOKEN}"}

    original_tick = app.state.scheduler.tick
    captured_kwargs: list[dict[str, object]] = []

    def spy_tick(**kwargs: object) -> object:
        captured_kwargs.append(kwargs)
        return original_tick(**kwargs)

    app.state.scheduler.tick = spy_tick

    with TestClient(app, base_url=ORIGIN) as client:
        resp = client.post(
            "/api/internal/tasks/tick?limit=500&processing_budget_seconds=999&lease_seconds=9999",
            json={
                "limit": 500,
                "workspace_id": "malicious-workspace-id",
                "processing_budget_seconds": 999.0,
            },
            headers=auth_header,
        )
        assert resp.status_code == 200
        assert len(captured_kwargs) == 1
        assert captured_kwargs[0] == {}


# ==============================================================================
# 4. OpenAPI Exclusion, Local Mode Exclusion, and Security Invariants
# ==============================================================================


def test_operational_endpoints_omitted_from_openapi(pg_url: str) -> None:
    """Both operational routes must be omitted from OpenAPI schema."""
    settings = _hosted_settings(pg_url)
    app = create_app(settings)

    with TestClient(app, base_url=ORIGIN) as client:
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        paths = schema.get("paths", {})
        assert "/api/internal/tasks/tick" not in paths
        assert "/api/internal/tasks/status" not in paths
        assert "/api/workspaces" in paths
        assert "/api/health" in paths


def test_local_mode_excludes_operational_routes(tmp_path: Path) -> None:
    """Local runtime mode does not expose operational routes (returns 404)."""
    settings = Settings(
        db_path=tmp_path / "local.db",
        allowed_origins=("http://localhost:5173",),
        cookie_secure=False,
        runtime="local",
        store="sqlite",
        tasks_enabled=False,
        task_tick_token=VALID_TICK_TOKEN,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        auth_header = {"Authorization": f"Bearer {VALID_TICK_TOKEN}"}
        resp_tick = client.post("/api/internal/tasks/tick", headers=auth_header)
        assert resp_tick.status_code == 404

        resp_status = client.get("/api/internal/tasks/status", headers=auth_header)
        assert resp_status.status_code == 404


def test_status_read_never_runs_tasks(pg_url: str) -> None:
    """GET /api/internal/tasks/status is strictly read-only and never runs tasks."""
    settings = _hosted_settings(pg_url)
    app = create_app(settings)
    auth_header = {"Authorization": f"Bearer {VALID_TICK_TOKEN}"}

    with (
        patch(
            "borrowed_steps.infrastructure.hosted_scheduler.process_due_tasks",
            side_effect=AssertionError("process_due_tasks must not be called by status check"),
        ),
        TestClient(app, base_url=ORIGIN) as client,
    ):
        resp = client.get("/api/internal/tasks/status", headers=auth_header)
        assert resp.status_code == 200


@pytest.mark.parametrize(
    ("path", "method", "operation"),
    [
        ("/api/internal/tasks/tick", "post", "tick"),
        ("/api/internal/tasks/status", "get", "read_status"),
    ],
)
def test_operational_exception_details_never_reach_logs(
    path: str,
    method: str,
    operation: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(_hosted_settings("postgresql://synthetic@127.0.0.1:1/test"))
    marker = "synthetic-private-database-error"
    with (
        patch.object(HostedSchedulerService, operation, side_effect=RuntimeError(marker)),
        TestClient(app, base_url=ORIGIN) as client,
    ):
        response = client.request(
            method, path, headers={"Authorization": f"Bearer {VALID_TICK_TOKEN}"}
        )
    assert response.status_code == 503
    assert marker not in response.text
    assert marker not in caplog.text
    assert not any(record.exc_info for record in caplog.records)


def test_hosted_intake_schema_gate_precedes_session_lookup() -> None:
    with disposable_database() as url:
        app = create_app(_hosted_settings(url))
        with (
            patch.object(
                PostgresStore, "get_session", side_effect=AssertionError("schema gate bypassed")
            ),
            TestClient(app, base_url=ORIGIN) as client,
        ):
            client.cookies.set(SESSION_COOKIE, "synthetic-session")
            response = client.post(
                "/api/intake/interpret",
                json={"text": "synthetic intake"},
                headers={"Origin": ORIGIN},
            )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


@pytest.mark.parametrize("failure", [None, "event", "finalize", "contention"])
def test_real_due_notice_http_outcomes(
    pg_url: str,
    failure: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PostgresStore(pg_url)
    services, session, equipment = _seed_workspace(store)
    _create_loan_with_due_task(services, session, equipment)
    with psycopg.connect(pg_url) as conn:
        conn.execute("UPDATE tasks SET due_at = '2000-01-01T00:00:00Z'")
    app = create_app(_hosted_settings(pg_url))
    auth = {"Authorization": f"Bearer {VALID_TICK_TOKEN}"}
    if failure == "event":

        def fail_event(*args: object, **kwargs: object) -> None:
            raise RuntimeError("synthetic-event-failure")

        monkeypatch.setattr(PostgresUnitOfWork, "add_event", fail_event)
    elif failure == "finalize":

        def fail_finalize(*args: object, **kwargs: object) -> None:
            raise RuntimeError("synthetic-evidence-failure")

        monkeypatch.setattr(HostedSchedulerService, "_finalize", fail_finalize)

    # A real competing transaction deterministically owns the workspace lock.
    with psycopg.connect(pg_url) as blocker:
        if failure == "contention":
            blocker.execute(
                "SELECT id FROM workspaces WHERE id=%s FOR UPDATE", (session.workspace_id,)
            )
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.post("/api/internal/tasks/tick", headers=auth)
            assert response.status_code == (503 if failure in {"event", "finalize"} else 200)
            if failure == "contention":
                assert response.json()["outcome"] == "partial"
                assert response.json()["report"]["contended"] == 1
            if failure is None:
                assert response.json()["report"]["marked_due"] == 1
                repeated = client.post("/api/internal/tasks/tick", headers=auth)
                assert repeated.json()["report"]["marked_due"] == 0
    with TestClient(create_app(_hosted_settings(pg_url)), base_url=ORIGIN) as fresh:
        status = fresh.get("/api/internal/tasks/status", headers=auth).json()
    if failure == "event":
        assert status["status"] == "failed"
        assert status["counts"] is None
        assert status["counts_complete"] is False
    elif failure == "finalize":
        assert status["status"] == "running"
        assert status["last_success_at"] is None
    elif failure == "contention":
        assert status["status"] == "partial"
        assert status["last_success_at"] is None
    else:
        assert status["status"] == "success"
    with store.transaction(session.workspace_id, write=False) as uow:
        notices = [event for event in uow.list_events() if event.action.value == "PICKUP_DUE"]
        assert len(notices) == (0 if failure in {"event", "contention"} else 1)
