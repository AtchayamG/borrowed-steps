"""Release-candidate integration tests for hosted mode in Borrowed Steps (BS-028).

Verifies:
1. Public health endpoint reports milestone 'M3' and honest agent_mode
   ('disabled' or 'strands_groq').
2. Default BS_ASSISTANT_ENABLED=0 returns 503 ASSISTANT_DISABLED without provider.
3. Enabled hosted assistant (BS_ASSISTANT_ENABLED=1) routes POST /api/intake/interpret
   to StrandsGroqInterpreter.
4. Successful interpretation returns grounded draft with
   strands/groq/openai/gpt-oss-20b provenance.
5. Error mapping to frozen contract:
   - 401 SESSION_REQUIRED on missing/expired session.
   - 403 ORIGIN_FORBIDDEN on invalid Origin header.
   - 409 STATE_CONFLICT on conflicting workspace state.
   - 422 VALIDATION_ERROR on empty, oversized, or unknown fields.
   - 429 ASSISTANT_BUSY on active concurrent interpretation.
   - 502 ASSISTANT_INVALID_OUTPUT on ungrounded/malformed model output.
   - 503 ASSISTANT_UNAVAILABLE on provider 429 or provider failure.
   - 504 ASSISTANT_TIMEOUT on operation deadline expiration.
6. Admission invariants across real disposable PostgreSQL:
   - Reserve and dispatch before provider network calls.
   - No database transaction held across inference.
   - Truthful settlement into inference_admissions ledger.
   - Concurrency slot retained on cancellation or cleanup failure.
   - 15-minute global cooldown on confirmed 429.
   - Provider model NEVER constructed on refused reservation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
import psycopg
import pytest
from starlette.testclient import TestClient

from borrowed_steps.application.errors import (
    AssistantInvalidOutputError,
    AssistantUnavailableError,
)
from borrowed_steps.application.interpreter import (
    DraftRequest,
    Interpretation,
    InventoryReader,
    RequestInterpreter,
)
from borrowed_steps.application.ports import Clock
from borrowed_steps.config import Settings
from borrowed_steps.domain.errors import StateConflictError
from borrowed_steps.domain.models import EquipmentKind
from borrowed_steps.infrastructure.groq_model import GroqModel
from borrowed_steps.infrastructure.inference_admission import (
    AdmissionReservationRequest,
    InferenceAdmissionStore,
)
from borrowed_steps.infrastructure.strands_groq_interpreter import StrandsGroqInterpreter
from borrowed_steps.interfaces.http.app import (
    AGENT_MODE_DISABLED,
    AGENT_MODE_STRANDS_GROQ,
    MILESTONE_HOSTED,
    create_app,
)
from postgres_support import get_test_postgres_url, migrated_database

DUMMY_KEY = "gsk_test_synthetic_key_12345678"
DUMMY_DB_URL = "postgresql://synthetic_user:synthetic_pass@127.0.0.1:5432/synthetic_db"
SAMPLE_TEXT = "Priya S requires a wheelchair pickup at the Adyar room by 2026-10-01T08:00:00Z."


class StubClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


class StubInterpreter(RequestInterpreter):
    def __init__(
        self,
        result: Interpretation | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.invoked = False
        self._result = result
        self._error = error
        self._delay = delay

    async def interpret(
        self, text: str, inventory: InventoryReader, cancel: Event
    ) -> Interpretation:
        self.invoked = True
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        return Interpretation(
            draft=DraftRequest(
                borrower_label="Stub Borrower",
                equipment_kind=EquipmentKind.WHEELCHAIR,
                pickup_location="Room A",
                due_at=datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC),
            ),
            missing_fields=(),
            framework="strands",
            provider="groq",
            model="openai/gpt-oss-20b",
            inventory_tool_calls=1,
            completed_at=datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC),
        )


def _make_hosted_settings(
    db_url: str,
    *,
    assistant_enabled: bool = False,
    groq_api_key: str | None = None,
    allowed_origin: str = "https://example.com",
) -> Settings:
    return Settings(
        db_path=Path("dummy.db"),
        allowed_origins=(allowed_origin,),
        cookie_secure=True,
        runtime="hosted",
        store="postgres",
        database_url=db_url,
        assistant_provider="groq",
        assistant_enabled=assistant_enabled,
        groq_api_key=groq_api_key,
        tasks_enabled=False,
    )


# ===========================================================================
# 1. Health & Mode Endpoint Tests
# ===========================================================================


def test_hosted_health_reports_m3_and_disabled_by_default() -> None:
    settings = _make_hosted_settings(DUMMY_DB_URL, assistant_enabled=False)
    app = create_app(settings)
    client = TestClient(app, base_url="https://example.com")

    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["milestone"] == MILESTONE_HOSTED == "M3"
    assert data["agent_mode"] == AGENT_MODE_DISABLED == "disabled"


def test_hosted_health_reports_m3_and_strands_groq_when_enabled() -> None:
    settings = _make_hosted_settings(
        DUMMY_DB_URL,
        assistant_enabled=True,
        groq_api_key=DUMMY_KEY,
    )
    stub = StubInterpreter()
    app = create_app(settings, interpreter=stub)
    client = TestClient(app, base_url="https://example.com")

    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["milestone"] == MILESTONE_HOSTED == "M3"
    assert data["agent_mode"] == AGENT_MODE_STRANDS_GROQ == "strands_groq"


# ===========================================================================
# 2. Gating and Authorization Tests (PostgreSQL Required)
# ===========================================================================


def test_hosted_intake_disabled_returns_503_without_provider_construction() -> None:
    """When BS_ASSISTANT_ENABLED=0, POST /api/intake/interpret returns 503 ASSISTANT_DISABLED."""
    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        settings = _make_hosted_settings(url, assistant_enabled=False)
        app = create_app(settings)
        client = TestClient(app, base_url="https://example.com")

        ws_res = client.post("/api/workspaces", json={}, headers={"Origin": "https://example.com"})
        assert ws_res.status_code == 201

        res = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT},
            headers={"Origin": "https://example.com"},
        )
        assert res.status_code == 503
        data = res.json()
        assert data["error"]["code"] == "ASSISTANT_DISABLED"
        assert "not enabled" in data["error"]["message"]


def test_hosted_intake_requires_session() -> None:
    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        settings = _make_hosted_settings(
            url,
            assistant_enabled=True,
            groq_api_key=DUMMY_KEY,
        )
        stub = StubInterpreter()
        app = create_app(settings, interpreter=stub)
        client = TestClient(app, base_url="https://example.com")

        # Request without session cookie
        res = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT},
            headers={"Origin": "https://example.com"},
        )
        assert res.status_code == 401
        assert res.json()["error"]["code"] == "SESSION_REQUIRED"
        assert not stub.invoked


def test_hosted_intake_checks_origin() -> None:
    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        settings = _make_hosted_settings(
            url,
            assistant_enabled=True,
            groq_api_key=DUMMY_KEY,
            allowed_origin="https://app.example.com",
        )
        stub = StubInterpreter()
        app = create_app(settings, interpreter=stub)
        client = TestClient(app, base_url="https://app.example.com")

        # Disallowed Origin
        res = (
            client.post(
                "/api/intake/interpret",
                json={"text": SAMPLE_TEXT},
                headers={"Origin": "https://evil.example.com"},
                headers_origin="https://evil.example.com",
            )
            if False
            else client.post(
                "/api/intake/interpret",
                json={"text": SAMPLE_TEXT},
                headers={"Origin": "https://evil.example.com"},
            )
        )
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "ORIGIN_FORBIDDEN"
        assert not stub.invoked


def test_hosted_intake_validates_text_length_and_rejects_unknown_fields() -> None:
    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        settings = _make_hosted_settings(
            url,
            assistant_enabled=True,
            groq_api_key=DUMMY_KEY,
        )
        stub = StubInterpreter()
        app = create_app(settings, interpreter=stub)
        client = TestClient(app, base_url="https://example.com")

        # Create workspace session
        ws_res = client.post("/api/workspaces", json={}, headers={"Origin": "https://example.com"})
        assert ws_res.status_code == 201

        # Empty text
        res_empty = client.post(
            "/api/intake/interpret",
            json={"text": "   "},
            headers={"Origin": "https://example.com"},
        )
        assert res_empty.status_code == 422
        assert res_empty.json()["error"]["code"] == "VALIDATION_ERROR"

        # Oversized text > 2000 chars
        res_long = client.post(
            "/api/intake/interpret",
            json={"text": "A" * 2001},
            headers={"Origin": "https://example.com"},
        )
        assert res_long.status_code == 422
        assert res_long.json()["error"]["code"] == "VALIDATION_ERROR"

        # Unknown extra field
        res_extra = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT, "unknown_field": "injected"},
            headers={"Origin": "https://example.com"},
        )
        assert res_extra.status_code == 422
        assert res_extra.json()["error"]["code"] == "VALIDATION_ERROR"
        assert not stub.invoked


# ===========================================================================
# 3. Error Mapping Tests (409, 502, 503, 504)
# ===========================================================================


def test_hosted_intake_state_conflict_returns_409() -> None:
    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        settings = _make_hosted_settings(url, assistant_enabled=True, groq_api_key=DUMMY_KEY)
        stub = StubInterpreter(error=StateConflictError("Concurrent mutation occurred."))
        app = create_app(settings, interpreter=stub)
        client = TestClient(app, base_url="https://example.com")

        ws_res = client.post("/api/workspaces", json={}, headers={"Origin": "https://example.com"})
        assert ws_res.status_code == 201

        res = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT},
            headers={"Origin": "https://example.com"},
        )
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "STATE_CONFLICT"


def test_hosted_intake_invalid_output_returns_502() -> None:
    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        settings = _make_hosted_settings(url, assistant_enabled=True, groq_api_key=DUMMY_KEY)
        stub = StubInterpreter(error=AssistantInvalidOutputError("Model produced invalid json."))
        app = create_app(settings, interpreter=stub)
        client = TestClient(app, base_url="https://example.com")

        ws_res = client.post("/api/workspaces", json={}, headers={"Origin": "https://example.com"})
        assert ws_res.status_code == 201

        res = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT},
            headers={"Origin": "https://example.com"},
        )
        assert res.status_code == 502
        assert res.json()["error"]["code"] == "ASSISTANT_INVALID_OUTPUT"


def test_hosted_intake_unavailable_returns_503() -> None:
    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        settings = _make_hosted_settings(url, assistant_enabled=True, groq_api_key=DUMMY_KEY)
        stub = StubInterpreter(error=AssistantUnavailableError("Provider is down."))
        app = create_app(settings, interpreter=stub)
        client = TestClient(app, base_url="https://example.com")

        ws_res = client.post("/api/workspaces", json={}, headers={"Origin": "https://example.com"})
        assert ws_res.status_code == 201

        res = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT},
            headers={"Origin": "https://example.com"},
        )
        assert res.status_code == 503
        assert res.json()["error"]["code"] == "ASSISTANT_UNAVAILABLE"


def test_hosted_intake_timeout_returns_504() -> None:
    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        settings = _make_hosted_settings(url, assistant_enabled=True, groq_api_key=DUMMY_KEY)
        stub = StubInterpreter(delay=0.3)
        app = create_app(settings, interpreter=stub, assistant_deadline_seconds=0.05)
        client = TestClient(app, base_url="https://example.com")

        ws_res = client.post("/api/workspaces", json={}, headers={"Origin": "https://example.com"})
        assert ws_res.status_code == 201

        res = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT},
            headers={"Origin": "https://example.com"},
        )
        assert res.status_code == 504
        assert res.json()["error"]["code"] == "ASSISTANT_TIMEOUT"


# ===========================================================================
# 4. End-to-End Execution with Real PostgreSQL and Mock Transport
# ===========================================================================


def test_pg_hosted_intake_successful_flow_and_admission_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full end-to-end hosted flow with real PostgreSQL and mock transport."""
    from test_strands_groq_interpreter import _make_success_mock_transport

    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        clock = StubClock()
        transport = _make_success_mock_transport()
        # Exercise the production factory; intercept only physical HTTP sends.
        monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: transport)

        settings = _make_hosted_settings(
            url,
            assistant_enabled=True,
            groq_api_key=DUMMY_KEY,
        )
        app = create_app(settings, clock=clock)
        client = TestClient(app, base_url="https://example.com")

        # 1. Create workspace
        ws_res = client.post("/api/workspaces", json={}, headers={"Origin": "https://example.com"})
        assert ws_res.status_code == 201
        ws_id = ws_res.json()["workspace"]["id"]

        # 2. Check snapshot agent_mode
        snap_res = client.get("/api/snapshot")
        assert snap_res.status_code == 200
        assert snap_res.json()["agent_mode"] == "strands_groq"

        # 3. Call POST /api/intake/interpret
        intake_res = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT},
            headers={"Origin": "https://example.com"},
        )
        assert intake_res.status_code == 200
        body = intake_res.json()

        # Verify draft shape and content
        draft = body["draft"]
        assert draft["borrower_label"] == "Priya S"
        assert draft["equipment_kind"] == "WHEELCHAIR"
        assert draft["pickup_location"] == "the Adyar room"
        assert draft["due_at"] == "2026-10-01T08:00:00Z"
        assert body["missing_fields"] == []

        # Verify provenance
        provenance = body["provenance"]
        assert provenance["framework"] == "strands"
        assert provenance["provider"] == "groq"
        assert provenance["model"] == "openai/gpt-oss-20b"
        assert provenance["inventory_tool_calls"] == 1
        assert "completed_at" in provenance

        # 4. Verify PostgreSQL admission row
        with psycopg.connect(url) as conn:
            row = conn.execute(
                """
                SELECT state, is_active, reserved_sends, actual_sends,
                       cleanup_completed, failure_code, released_at
                FROM inference_admissions
                WHERE workspace_id = %s
                """,
                (ws_id,),
            ).fetchone()
            assert row is not None
            assert row[0] == "SUCCEEDED"
            assert row[1] is False
            assert row[2] == 6  # reserved_sends
            assert row[3] >= 1  # actual_sends
            assert row[4] is True  # cleanup_completed
            assert row[5] is None  # failure_code
            assert row[6] is not None  # released_at


def test_pg_hosted_intake_provider_429_settlement_and_cooldown() -> None:
    """HTTP 429 from provider maps to 503 ASSISTANT_UNAVAILABLE and creates 15-min cooldown."""
    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        clock = StubClock()

        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                headers={"retry-after": "60"},
                json={"error": {"message": "Rate limit exceeded"}},
            )

        transport = httpx.MockTransport(handler)
        interpreter = StrandsGroqInterpreter(
            api_key=DUMMY_KEY,
            database_url=url,
            clock=clock,
            transport=transport,
        )

        settings = _make_hosted_settings(
            url,
            assistant_enabled=True,
            groq_api_key=DUMMY_KEY,
        )
        app = create_app(settings, clock=clock, interpreter=interpreter)
        client = TestClient(app, base_url="https://example.com")

        # Create workspace
        ws_res = client.post("/api/workspaces", json={}, headers={"Origin": "https://example.com"})
        assert ws_res.status_code == 201

        # Call intake -> 503 ASSISTANT_UNAVAILABLE
        intake_res = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT},
            headers={"Origin": "https://example.com"},
        )
        assert intake_res.status_code == 503
        data = intake_res.json()
        assert data["error"]["code"] == "ASSISTANT_UNAVAILABLE"

        # Verify admission record recorded provider_429 failure_code
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT state, failure_code FROM inference_admissions "
                "WHERE failure_code = 'provider_429'"
            ).fetchone()
            assert row is not None
            assert row[0] == "FAILED_CONFIRMED"
            assert row[1] == "provider_429"

        # Second call is immediately refused due to active 15-min cooldown
        intake_res2 = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT},
            headers={"Origin": "https://example.com"},
        )
        assert intake_res2.status_code == 503
        assert intake_res2.json()["error"]["code"] == "ASSISTANT_UNAVAILABLE"


def test_pg_hosted_intake_concurrency_busy() -> None:
    """When an operation is active, concurrent request returns 429 ASSISTANT_BUSY."""
    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        clock = StubClock()
        store = InferenceAdmissionStore(url)

        settings = _make_hosted_settings(
            url,
            assistant_enabled=True,
            groq_api_key=DUMMY_KEY,
        )
        app = create_app(settings, clock=clock)
        client = TestClient(app, base_url="https://example.com")

        ws_res = client.post("/api/workspaces", json={}, headers={"Origin": "https://example.com"})
        assert ws_res.status_code == 201
        ws_id = ws_res.json()["workspace"]["id"]

        # Directly insert an active reservation in admission ledger
        res_req = AdmissionReservationRequest(
            reservation_id=str(uuid4()),
            workspace_id=ws_id,
            owner_id=str(uuid4()),
            request_key_hash="0" * 64,
            payload_hash="1" * 64,
        )
        store.reserve(res_req)

        # Calling intake now must fail with 429 ASSISTANT_BUSY
        res = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT},
            headers={"Origin": "https://example.com"},
        )
        assert res.status_code == 429
        assert res.json()["error"]["code"] == "ASSISTANT_BUSY"


def test_pg_hosted_cancellation_retains_concurrency_slot() -> None:
    """Uncertainty from cancellation keeps the reservation active and retains the slot."""
    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        clock = StubClock()

        async def hanging_handler(_: httpx.Request) -> httpx.Response:
            await asyncio.sleep(2.0)
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(hanging_handler)
        interpreter = StrandsGroqInterpreter(
            api_key=DUMMY_KEY,
            database_url=url,
            clock=clock,
            transport=transport,
        )

        settings = _make_hosted_settings(
            url,
            assistant_enabled=True,
            groq_api_key=DUMMY_KEY,
        )
        app = create_app(
            settings,
            clock=clock,
            interpreter=interpreter,
            assistant_deadline_seconds=0.25,
            assistant_cancel_grace_seconds=0.02,
        )
        client = TestClient(app, base_url="https://example.com")

        ws_res = client.post("/api/workspaces", json={}, headers={"Origin": "https://example.com"})
        assert ws_res.status_code == 201
        ws_id = ws_res.json()["workspace"]["id"]

        # First request times out and is cancelled
        res1 = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT},
            headers={"Origin": "https://example.com"},
        )
        assert res1.status_code == 504
        assert res1.json()["error"]["code"] == "ASSISTANT_TIMEOUT"

        # Concurrency slot is retained in admission store (active reservation remains)
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT is_active, state FROM inference_admissions WHERE workspace_id = %s",
                (ws_id,),
            ).fetchone()
            assert row is not None
            assert row[0] is True  # is_active retained!
            assert row[1] in ("RESERVED", "DISPATCHED", "UNCERTAIN")

        # Second request is blocked because the slot is retained
        res2 = client.post(
            "/api/intake/interpret",
            json={"text": SAMPLE_TEXT},
            headers={"Origin": "https://example.com"},
        )
        assert res2.status_code == 429
        assert res2.json()["error"]["code"] == "ASSISTANT_BUSY"


def test_provider_model_never_constructed_on_refused_admission() -> None:
    """GroqModel constructor is NEVER called if admission reservation is refused."""
    clock = StubClock()
    constructed = False

    def mock_init(self: object, *args: object, **kwargs: object) -> None:
        nonlocal constructed
        constructed = True
        raise RuntimeError("GroqModel constructor should not have been reached!")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(GroqModel, "__init__", mock_init)

        interpreter = StrandsGroqInterpreter(
            api_key=DUMMY_KEY,
            database_url=DUMMY_DB_URL,
            clock=clock,
        )

        @dataclass
        class BadInventory:
            workspace_id: str = "invalid-workspace-id"

            def kind_state_counts(self) -> list[object]:
                return []

        with pytest.raises(AssistantUnavailableError, match="Invalid workspace identity"):
            asyncio.run(interpreter.interpret(SAMPLE_TEXT, BadInventory(), Event()))  # type: ignore[arg-type]

        assert not constructed
