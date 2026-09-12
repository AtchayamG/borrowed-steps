"""Tests for StrandsGroqInterpreter hosted integration boundary.

Proves:
1. No Ollama import during hosted module import or execution.
2. Pinned model and endpoint, strict boundaries, and parameter validation.
3. GroqModel is NEVER constructed on refused, disabled, or malformed requests.
4. Two-stage Strands execution with real Strands/OpenAI serialization.
5. Accurate provenance: strands / groq / openai/gpt-oss-20b.
6. Truthful settlement in InferenceAdmissionStore: success, 429, timeout, invalid output,
   and cancellation/uncertainty retention.
7. CleanupOwner lifecycle and unresolved cleanup detection.
8. Real disposable PostgreSQL lifecycle, one-active concurrency, 429 cooldown,
   cancellation uncertainty slot retention, and recovery.
9. App construction seam and public assistant disabled enforcement.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from datetime import UTC, datetime
from threading import Event
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from starlette.testclient import TestClient

from borrowed_steps.application.errors import (
    AssistantBusyError,
    AssistantInvalidOutputError,
    AssistantTimeoutError,
    AssistantUnavailableError,
)
from borrowed_steps.application.interpreter import (
    Interpretation,
    InventoryCount,
)
from borrowed_steps.application.ports import Clock
from borrowed_steps.config import (
    Settings,
)
from borrowed_steps.domain.models import EquipmentKind
from borrowed_steps.infrastructure.groq_model import (
    GROQ_MODEL_ID,
    GroqModel,
)
from borrowed_steps.infrastructure.inference_admission import (
    AdmissionFailureCode,
    AdmissionReservationRequest,
    AdmissionState,
    InferenceAdmissionStore,
    RecoveryReason,
)
from borrowed_steps.infrastructure.strands_groq_interpreter import StrandsGroqInterpreter
from borrowed_steps.interfaces.http.app import _real_hosted_interpreter, create_app
from postgres_support import migrated_database

DUMMY_KEY = "gsk_test_key_0123456789abcdef0123456789abcdef"
DUMMY_DB_URL = "postgresql://postgres@127.0.0.1:5432/test?sslmode=disable"
SAMPLE_WS_ID = "11111111-2222-4333-8444-555555555555"
SAMPLE_TEXT = "Priya S needs a wheelchair from the Adyar room by 2026-10-01T08:00:00Z."


class StubClock(Clock):
    def __init__(self, dt: datetime | None = None) -> None:
        self._dt = dt or datetime(2026, 9, 12, 10, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._dt


class StubInventoryReader:
    def __init__(self, workspace_id: str = SAMPLE_WS_ID) -> None:
        self.workspace_id = workspace_id

    def kind_state_counts(self) -> list[InventoryCount]:
        return [InventoryCount(kind="WHEELCHAIR", state="AVAILABLE", count=2)]


def _make_sse_tool_call(tool_name: str, arguments: dict[str, Any], call_id: str = "call_1") -> str:
    chunk1 = {
        "id": "chatcmpl-tool-1",
        "object": "chat.completion.chunk",
        "created": 1725880000,
        "model": GROQ_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": None,
            }
        ],
    }
    chunk2 = {
        "id": "chatcmpl-tool-1",
        "object": "chat.completion.chunk",
        "created": 1725880000,
        "model": GROQ_MODEL_ID,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    return f"data: {json.dumps(chunk1)}\n\ndata: {json.dumps(chunk2)}\n\ndata: [DONE]\n\n"


def _make_sse_text_response(text: str) -> str:
    chunk = {
        "id": "chatcmpl-text-1",
        "object": "chat.completion.chunk",
        "created": 1725880000,
        "model": GROQ_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


def _make_parsed_response(content: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-parsed-1",
        "object": "chat.completion",
        "created": 1725880000,
        "model": GROQ_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(content) if content is not None else None,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 45,
            "total_tokens": 165,
        },
    }


def _make_success_mock_transport() -> httpx.MockTransport:
    tool_sse = _make_sse_tool_call("read_inventory", {})
    text_sse = _make_sse_text_response("Checked the room inventory.")
    parsed_json = _make_parsed_response(
        {
            "borrower_label": "Priya S",
            "equipment_kind": "wheelchair",
            "pickup_location": "the Adyar room",
            "due_at": "2026-10-01T08:00:00Z",
        }
    )

    turn = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal turn
        turn += 1
        body = json.loads(request.content.decode("utf-8"))
        if "response_format" in body:
            return httpx.Response(200, json=parsed_json)
        if turn == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=tool_sse)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text_sse)

    return httpx.MockTransport(handler)


# ===========================================================================
# 1. Ollama Isolation & Module Boundaries
# ===========================================================================


def test_strands_groq_interpreter_does_not_import_ollama() -> None:
    """Proves importing and using StrandsGroqInterpreter does not load ollama into sys.modules."""
    import subprocess
    from pathlib import Path

    code = (
        "import borrowed_steps.infrastructure.strands_groq_interpreter; "
        "import sys; assert 'ollama' not in sys.modules"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Subprocess failed: {res.stderr}"

    import ast

    import borrowed_steps.infrastructure.strands_common as common_mod
    import borrowed_steps.infrastructure.strands_groq_interpreter as target_mod

    for mod in (target_mod, common_mod):
        assert mod.__file__ is not None
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "ollama" not in alias.name.lower(), (
                        f"Found ollama import in {mod.__file__}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert "ollama" not in node.module.lower(), (
                    f"Found from ollama import in {mod.__file__}"
                )


def test_constructor_parameter_validation() -> None:
    """Constructor validates credentials, timeouts, and deadlines strictly."""
    clock = StubClock()
    with pytest.raises(ValueError, match="api_key must be an explicit non-empty string"):
        StrandsGroqInterpreter(api_key="", database_url=DUMMY_DB_URL, clock=clock)

    with pytest.raises(ValueError, match="database_url must be an explicit non-empty string"):
        StrandsGroqInterpreter(api_key=DUMMY_KEY, database_url="", clock=clock)

    with pytest.raises(ValueError, match="max_model_requests must be between 1 and 6"):
        StrandsGroqInterpreter(
            api_key=DUMMY_KEY, database_url=DUMMY_DB_URL, clock=clock, max_model_requests=7
        )

    with pytest.raises(ValueError, match=r"deadline_seconds must be between 0\.0 and 110\.0"):
        StrandsGroqInterpreter(
            api_key=DUMMY_KEY, database_url=DUMMY_DB_URL, clock=clock, deadline_seconds=111.0
        )

    with pytest.raises(
        ValueError, match=r"transport_timeout_seconds must be between 0\.0 and 60\.0"
    ):
        StrandsGroqInterpreter(
            api_key=DUMMY_KEY,
            database_url=DUMMY_DB_URL,
            clock=clock,
            transport_timeout_seconds=65.0,
        )


# ===========================================================================
# 2. Admission Gating: GroqModel Never Created on Refused / Malformed Requests
# ===========================================================================


class SpyAdmissionStore:
    """Spy fake admission store tracking invocations and allowing refusal injection."""

    def __init__(self) -> None:
        self.reserve_calls: list[AdmissionReservationRequest] = []
        self.dispatch_calls: list[tuple[str, str]] = []
        self.finish_calls: list[dict[str, Any]] = []
        self.refuse_reserve_error: Exception | None = None
        self.refuse_dispatch_error: Exception | None = None

    def reserve(self, req: AdmissionReservationRequest) -> dict[str, Any]:
        self.reserve_calls.append(req)
        if self.refuse_reserve_error is not None:
            raise self.refuse_reserve_error
        return {
            "reservation_id": req.reservation_id,
            "workspace_id": req.workspace_id,
            "owner_id": req.owner_id,
            "state": "RESERVED",
        }

    def mark_dispatched(self, reservation_id: str, owner_id: str) -> dict[str, Any]:
        self.dispatch_calls.append((reservation_id, owner_id))
        if self.refuse_dispatch_error is not None:
            raise self.refuse_dispatch_error
        return {
            "reservation_id": reservation_id,
            "owner_id": owner_id,
            "state": "DISPATCHED",
        }

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
    ) -> dict[str, Any]:
        record = {
            "reservation_id": reservation_id,
            "owner_id": owner_id,
            "state": state,
            "cleanup_completed": cleanup_completed,
            "actual_sends": actual_sends,
            "actual_total_tokens": actual_total_tokens,
            "failure_code": failure_code,
        }
        self.finish_calls.append(record)
        return record


def test_groq_model_never_created_on_invalid_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interpreter fails before constructing GroqModel when workspace ID is missing/invalid."""
    clock = StubClock()
    store = SpyAdmissionStore()
    groq_model_created = False

    orig_init = GroqModel.__init__

    def spy_groq_init(self: GroqModel, *args: object, **kwargs: object) -> None:
        nonlocal groq_model_created
        groq_model_created = True
        orig_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(GroqModel, "__init__", spy_groq_init)

    interpreter = StrandsGroqInterpreter(
        api_key=DUMMY_KEY,
        database_url=DUMMY_DB_URL,
        clock=clock,
        admission_store=store,  # type: ignore[arg-type]
    )

    class BadInventory:
        workspace_id = "not-a-uuid"

        def kind_state_counts(self) -> list[InventoryCount]:
            return []

    with pytest.raises(AssistantUnavailableError, match="Invalid workspace identity"):
        asyncio.run(interpreter.interpret(SAMPLE_TEXT, BadInventory(), Event()))

    assert not groq_model_created, "GroqModel must NEVER be created on invalid workspace"
    assert len(store.reserve_calls) == 0


def test_groq_model_never_created_on_refused_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    """When admission reservation is refused, GroqModel is never created."""
    from borrowed_steps.infrastructure.inference_admission import AdmissionRefusedError

    clock = StubClock()
    store = SpyAdmissionStore()
    store.refuse_reserve_error = AdmissionRefusedError("Another operation is active")

    groq_model_created = False
    orig_init = GroqModel.__init__

    def spy_groq_init(self: GroqModel, *args: object, **kwargs: object) -> None:
        nonlocal groq_model_created
        groq_model_created = True
        orig_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(GroqModel, "__init__", spy_groq_init)

    interpreter = StrandsGroqInterpreter(
        api_key=DUMMY_KEY,
        database_url=DUMMY_DB_URL,
        clock=clock,
        admission_store=store,  # type: ignore[arg-type]
    )

    with pytest.raises(AssistantBusyError, match="Another interpretation is already running"):
        asyncio.run(interpreter.interpret(SAMPLE_TEXT, StubInventoryReader(), Event()))

    assert not groq_model_created, "GroqModel must NEVER be created on refused reservation"
    assert len(store.reserve_calls) == 1
    assert len(store.dispatch_calls) == 0


def test_groq_model_never_created_on_429_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """When 429 cooldown is active, admission refusal raises AssistantUnavailableError."""
    from borrowed_steps.infrastructure.inference_admission import AdmissionRefusedError

    clock = StubClock()
    store = SpyAdmissionStore()
    store.refuse_reserve_error = AdmissionRefusedError("Provider 429 cooldown active")

    groq_model_created = False
    orig_init = GroqModel.__init__

    def spy_groq_init(self: GroqModel, *args: object, **kwargs: object) -> None:
        nonlocal groq_model_created
        groq_model_created = True
        orig_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(GroqModel, "__init__", spy_groq_init)

    interpreter = StrandsGroqInterpreter(
        api_key=DUMMY_KEY,
        database_url=DUMMY_DB_URL,
        clock=clock,
        admission_store=store,  # type: ignore[arg-type]
    )

    with pytest.raises(
        AssistantUnavailableError, match="temporarily paused due to provider cooldown"
    ):
        asyncio.run(interpreter.interpret(SAMPLE_TEXT, StubInventoryReader(), Event()))

    assert not groq_model_created
    assert len(store.reserve_calls) == 1
    assert len(store.dispatch_calls) == 0


def test_groq_model_never_created_on_refused_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """When mark_dispatched fails, GroqModel is never created."""
    from borrowed_steps.infrastructure.inference_admission import AdmissionRefusedError

    clock = StubClock()
    store = SpyAdmissionStore()
    store.refuse_dispatch_error = AdmissionRefusedError("Reservation deadline expired")

    groq_model_created = False
    orig_init = GroqModel.__init__

    def spy_groq_init(self: GroqModel, *args: object, **kwargs: object) -> None:
        nonlocal groq_model_created
        groq_model_created = True
        orig_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(GroqModel, "__init__", spy_groq_init)

    interpreter = StrandsGroqInterpreter(
        api_key=DUMMY_KEY,
        database_url=DUMMY_DB_URL,
        clock=clock,
        admission_store=store,  # type: ignore[arg-type]
    )

    with pytest.raises(AssistantUnavailableError, match="Admission dispatch was refused"):
        asyncio.run(interpreter.interpret(SAMPLE_TEXT, StubInventoryReader(), Event()))

    assert not groq_model_created
    assert len(store.reserve_calls) == 1
    assert len(store.dispatch_calls) == 1
    assert len(store.finish_calls) == 0


# ===========================================================================
# 3. Two-Stage Flow, Provenance, and Successful Settlement
# ===========================================================================


def test_successful_interpretation_and_truthful_settlement() -> None:
    """Two-stage flow executes tool and extraction, producing draft and settling SUCCEEDED."""
    clock = StubClock()
    store = SpyAdmissionStore()
    transport = _make_success_mock_transport()

    interpreter = StrandsGroqInterpreter(
        api_key=DUMMY_KEY,
        database_url=DUMMY_DB_URL,
        clock=clock,
        transport=transport,
        admission_store=store,  # type: ignore[arg-type]
    )

    result: Interpretation = asyncio.run(
        interpreter.interpret(SAMPLE_TEXT, StubInventoryReader(), Event())
    )

    # 1. Verify interpretation structure and provenance
    assert result.framework == "strands"
    assert result.provider == "groq"
    assert result.model == GROQ_MODEL_ID
    assert result.inventory_tool_calls == 1
    assert result.draft.borrower_label == "Priya S"
    assert result.draft.equipment_kind == EquipmentKind.WHEELCHAIR
    assert result.draft.pickup_location == "the Adyar room"
    assert result.draft.due_at == datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)
    assert len(result.missing_fields) == 0

    # 2. Verify admission lifecycle and settlement
    assert len(store.reserve_calls) == 1
    assert len(store.dispatch_calls) == 1
    assert len(store.finish_calls) == 1

    finish_rec = store.finish_calls[0]
    assert finish_rec["state"] == AdmissionState.SUCCEEDED
    assert finish_rec["cleanup_completed"] is True
    # 2 turns in stage 1 + 1 parsed send in stage 2 = 3 sends
    assert finish_rec["actual_sends"] == 3
    assert finish_rec["actual_total_tokens"] is None
    assert finish_rec["failure_code"] is None

    # 3. Verify CleanupOwner status
    assert not interpreter.cleanup_unresolved


# ===========================================================================
# 4. Error Mapping & Conservative Settlement
# ===========================================================================


def test_provider_429_settlement() -> None:
    """Provider 429 response settles as FAILED_CONFIRMED with failure_code=PROVIDER_429."""
    clock = StubClock()
    store = SpyAdmissionStore()

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "60"},
            json={"error": {"message": "Rate limit reached", "type": "rate_limit_exceeded"}},
        )

    transport = httpx.MockTransport(handler)
    interpreter = StrandsGroqInterpreter(
        api_key=DUMMY_KEY,
        database_url=DUMMY_DB_URL,
        clock=clock,
        transport=transport,
        admission_store=store,  # type: ignore[arg-type]
    )

    with pytest.raises(AssistantUnavailableError, match="temporarily unavailable"):
        asyncio.run(interpreter.interpret(SAMPLE_TEXT, StubInventoryReader(), Event()))

    assert len(store.finish_calls) == 1
    rec = store.finish_calls[0]
    assert rec["state"] == AdmissionState.FAILED_CONFIRMED
    assert rec["cleanup_completed"] is True
    assert rec["failure_code"] == AdmissionFailureCode.PROVIDER_429
    assert rec["actual_sends"] == 1
    assert not interpreter.cleanup_unresolved


def test_timeout_settlement() -> None:
    """Timeout during execution settles as FAILED_CONFIRMED with DEADLINE_EXPIRED."""
    clock = StubClock()
    store = SpyAdmissionStore()

    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2.0)
        return httpx.Response(200, text="never")

    transport = httpx.MockTransport(handler)
    interpreter = StrandsGroqInterpreter(
        api_key=DUMMY_KEY,
        database_url=DUMMY_DB_URL,
        clock=clock,
        deadline_seconds=0.1,  # Short deadline to trigger timeout
        transport=transport,
        admission_store=store,  # type: ignore[arg-type]
    )

    with pytest.raises(AssistantTimeoutError, match="took too long"):
        asyncio.run(interpreter.interpret(SAMPLE_TEXT, StubInventoryReader(), Event()))

    assert len(store.finish_calls) == 1
    rec = store.finish_calls[0]
    assert rec["state"] == AdmissionState.FAILED_CONFIRMED
    assert rec["cleanup_completed"] is True
    assert rec["failure_code"] == AdmissionFailureCode.DEADLINE_EXPIRED


def test_invalid_output_no_tool_execution_settlement() -> None:
    """Model that skips tool execution settles as FAILED_CONFIRMED with INVALID_OUTPUT."""
    clock = StubClock()
    store = SpyAdmissionStore()
    text_sse = _make_sse_text_response("I ignored the instructions and checked nothing.")

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text_sse)

    transport = httpx.MockTransport(handler)
    interpreter = StrandsGroqInterpreter(
        api_key=DUMMY_KEY,
        database_url=DUMMY_DB_URL,
        clock=clock,
        transport=transport,
        admission_store=store,  # type: ignore[arg-type]
    )

    with pytest.raises(AssistantInvalidOutputError, match="could not produce a usable suggestion"):
        asyncio.run(interpreter.interpret(SAMPLE_TEXT, StubInventoryReader(), Event()))

    assert len(store.finish_calls) == 1
    rec = store.finish_calls[0]
    assert rec["state"] == AdmissionState.FAILED_CONFIRMED
    assert rec["cleanup_completed"] is True
    assert rec["failure_code"] == AdmissionFailureCode.INVALID_OUTPUT


def test_cancellation_settles_uncertain_and_retains_slot() -> None:
    """Cancellation settles as UNCERTAIN with CANCELLED failure code, retaining concurrency."""
    clock = StubClock()
    store = SpyAdmissionStore()
    cancel = Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        cancel.set()
        await asyncio.sleep(0.05)
        return httpx.Response(200, text=_make_sse_text_response("late"))

    transport = httpx.MockTransport(handler)
    interpreter = StrandsGroqInterpreter(
        api_key=DUMMY_KEY,
        database_url=DUMMY_DB_URL,
        clock=clock,
        transport=transport,
        admission_store=store,  # type: ignore[arg-type]
    )

    with pytest.raises((AssistantTimeoutError, asyncio.CancelledError)):
        asyncio.run(interpreter.interpret(SAMPLE_TEXT, StubInventoryReader(), cancel))

    assert len(store.finish_calls) == 1
    rec = store.finish_calls[0]
    assert rec["state"] == AdmissionState.UNCERTAIN
    assert rec["failure_code"] == AdmissionFailureCode.CANCELLED


# ===========================================================================
# 5. CleanupOwner Protocol & Cleanup Unresolved Refusal
# ===========================================================================


def test_cleanup_unresolved_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """When client fails to close, interpreter marks cleanup_unresolved and refuses next run."""

    async def _test() -> None:
        clock = StubClock()
        store = SpyAdmissionStore()

        interpreter = StrandsGroqInterpreter(
            api_key=DUMMY_KEY,
            database_url=DUMMY_DB_URL,
            clock=clock,
            admission_store=store,  # type: ignore[arg-type]
        )

        # Simulate an unresolved model stuck in _closing
        dummy_model = GroqModel(
            api_key=DUMMY_KEY, max_sends=3, transport=_make_success_mock_transport()
        )

        # Task that never finishes
        async def infinite_task() -> None:
            await asyncio.sleep(9999)

        task = asyncio.create_task(infinite_task())
        interpreter._closing[dummy_model] = task

        assert interpreter.cleanup_unresolved is True

        # Next interpret call must refuse before doing anything
        with pytest.raises(AssistantUnavailableError, match="previous run is cleaned up"):
            await interpreter.interpret(SAMPLE_TEXT, StubInventoryReader(), Event())

        # Clean up background task
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_test())


# ===========================================================================
# 6. Real Disposable PostgreSQL Tests
# ===========================================================================


def test_pg_full_admission_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full lifecycle on real PostgreSQL: reserve -> dispatch -> finish(SUCCEEDED) released."""
    from postgres_support import get_test_postgres_url

    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        # Create workspace in PostgreSQL
        wid = str(uuid4())
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO workspaces (id, created_at) VALUES (%s, clock_timestamp())", (wid,)
            )

        clock = StubClock()
        transport = _make_success_mock_transport()
        admission_store = InferenceAdmissionStore(url)

        interpreter = StrandsGroqInterpreter(
            api_key=DUMMY_KEY,
            database_url=url,
            clock=clock,
            transport=transport,
            admission_store=admission_store,
        )

        reader = StubInventoryReader(workspace_id=wid)
        result = asyncio.run(interpreter.interpret(SAMPLE_TEXT, reader, Event()))
        assert result.draft.borrower_label == "Priya S"

        # Check DB state
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT state, is_active, actual_sends, cleanup_completed, released_at"
                " FROM inference_admissions WHERE workspace_id = %s",
                (wid,),
            ).fetchone()
            assert row is not None
            assert row[0] == "SUCCEEDED"  # state
            assert row[1] is False  # is_active
            assert row[2] == 3  # actual_sends
            assert row[3] is True  # cleanup_completed
            assert row[4] is not None  # released_at


def test_pg_one_active_concurrency_refusal() -> None:
    """Active operation in PostgreSQL raises AssistantBusyError on concurrent reservation."""
    from postgres_support import get_test_postgres_url

    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        wid1 = str(uuid4())
        wid2 = str(uuid4())
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO workspaces (id, created_at) VALUES (%s, clock_timestamp())", (wid1,)
            )
            conn.execute(
                "INSERT INTO workspaces (id, created_at) VALUES (%s, clock_timestamp())", (wid2,)
            )

        clock = StubClock()
        store = InferenceAdmissionStore(url)

        # Create active reservation for ws1
        res1 = AdmissionReservationRequest(
            reservation_id=str(uuid4()),
            workspace_id=wid1,
            owner_id=str(uuid4()),
            request_key_hash="a" * 64,
            payload_hash="b" * 64,
        )
        store.reserve(res1)

        # Now try to run interpreter on ws2
        interpreter = StrandsGroqInterpreter(
            api_key=DUMMY_KEY,
            database_url=url,
            clock=clock,
            admission_store=store,
        )
        reader2 = StubInventoryReader(workspace_id=wid2)
        with pytest.raises(AssistantBusyError, match="Another interpretation is already running"):
            asyncio.run(interpreter.interpret(SAMPLE_TEXT, reader2, Event()))


def test_pg_429_cooldown_enforcement() -> None:
    """Confirmed 429 in PostgreSQL creates 15-minute cooldown refusing new interpretations."""
    from postgres_support import get_test_postgres_url

    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        wid = str(uuid4())
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO workspaces (id, created_at) VALUES (%s, clock_timestamp())", (wid,)
            )

        clock = StubClock()
        store = InferenceAdmissionStore(url)

        # Trigger a 429 interpretation
        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429, headers={"retry-after": "60"}, json={"error": {"message": "throttled"}}
            )

        interpreter = StrandsGroqInterpreter(
            api_key=DUMMY_KEY,
            database_url=url,
            clock=clock,
            transport=httpx.MockTransport(handler),
            admission_store=store,
        )
        reader = StubInventoryReader(workspace_id=wid)
        with pytest.raises(AssistantUnavailableError):
            asyncio.run(interpreter.interpret(SAMPLE_TEXT, reader, Event()))

        # Next interpretation must fail on 429 cooldown immediately
        with pytest.raises(
            AssistantUnavailableError, match="temporarily paused due to provider cooldown"
        ):
            asyncio.run(interpreter.interpret(SAMPLE_TEXT, reader, Event()))


def test_pg_cancellation_uncertain_retention_and_recovery() -> None:
    """Cancelled interpretation settles as UNCERTAIN in PostgreSQL, retaining active slot."""
    from postgres_support import get_test_postgres_url

    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        wid = str(uuid4())
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO workspaces (id, created_at) VALUES (%s, clock_timestamp())", (wid,)
            )

        clock = StubClock()
        store = InferenceAdmissionStore(url)
        cancel = Event()

        async def handler(_: httpx.Request) -> httpx.Response:
            cancel.set()
            await asyncio.sleep(0.05)
            return httpx.Response(200, text=_make_sse_text_response("delayed"))

        interpreter = StrandsGroqInterpreter(
            api_key=DUMMY_KEY,
            database_url=url,
            clock=clock,
            transport=httpx.MockTransport(handler),
            admission_store=store,
        )
        reader = StubInventoryReader(workspace_id=wid)
        res_id = str(uuid4())

        with pytest.raises((AssistantTimeoutError, asyncio.CancelledError)):
            asyncio.run(interpreter.interpret(SAMPLE_TEXT, reader, cancel, reservation_id=res_id))

        # Verify DB row is UNCERTAIN and is_active is TRUE
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT state, is_active, failure_code"
                " FROM inference_admissions WHERE reservation_id = %s",
                (res_id,),
            ).fetchone()
            assert row is not None
            assert row[0] == "UNCERTAIN"
            assert row[1] is True  # still active!
            assert row[2] == "cancelled"

        # Concurrency slot is retained, blocking new reservation
        with pytest.raises(AssistantBusyError, match="Another interpretation is already running"):
            asyncio.run(interpreter.interpret(SAMPLE_TEXT, reader, Event()))

        # Explicit operator recovery clears the slot
        operator_id = str(uuid4())
        store.recover_dead(
            res_id,
            operator_id=operator_id,
            reason=RecoveryReason.TIMEOUT_TERMINATED,
            confirmed_dead=True,
        )

        # Verify slot is cleared
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT state, is_active FROM inference_admissions WHERE reservation_id = %s",
                (res_id,),
            ).fetchone()
            assert row is not None
            assert row[0] == "RECOVERED"
            assert row[1] is False

        # Advance released_at past the global 60-second rolling window
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "UPDATE inference_admissions"
                " SET released_at = clock_timestamp() - INTERVAL '65 seconds'"
            )

        # Now new interpretation can proceed
        interpreter_ok = StrandsGroqInterpreter(
            api_key=DUMMY_KEY,
            database_url=url,
            clock=clock,
            transport=_make_success_mock_transport(),
            admission_store=store,
        )
        res_ok = asyncio.run(interpreter_ok.interpret(SAMPLE_TEXT, reader, Event()))
        assert res_ok.draft.borrower_label == "Priya S"


# ===========================================================================
# 7. App Construction Seam & Assistant Disabled
# ===========================================================================


def test_app_health_offline_hosted() -> None:
    """In hosted mode, health endpoint reports disabled and milestone M3 without contacting DB."""
    from pathlib import Path

    settings = Settings(
        db_path=Path("dummy.db"),
        allowed_origins=("https://example.com",),
        cookie_secure=True,
        runtime="hosted",
        store="postgres",
        database_url=DUMMY_DB_URL,
        assistant_provider="groq",
        assistant_enabled=False,
        tasks_enabled=False,
    )

    app = create_app(settings)
    client = TestClient(app)

    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["agent_mode"] == "disabled"
    assert res.json()["milestone"] == "M3"


def test_app_factory_seam_and_assistant_disabled() -> None:
    """In hosted mode with PostgreSQL, BS_ASSISTANT_ENABLED=0 returns 503 ASSISTANT_DISABLED."""
    from pathlib import Path

    from postgres_support import get_test_postgres_url

    base_url = get_test_postgres_url()
    with migrated_database(base_url) as url:
        settings = Settings(
            db_path=Path("dummy.db"),
            allowed_origins=("https://example.com",),
            cookie_secure=True,
            runtime="hosted",
            store="postgres",
            database_url=url,
            assistant_provider="groq",
            assistant_enabled=False,
            tasks_enabled=False,
        )

        app = create_app(settings)
        with TestClient(app, base_url="https://example.com") as client:
            # Health endpoint reports disabled
            res = client.get("/api/health")
            assert res.status_code == 200
            assert res.json()["agent_mode"] == "disabled"
            assert res.json()["milestone"] == "M3"

            # Start a session
            ws_res = client.post(
                "/api/workspaces",
                headers={"origin": "https://example.com"},
                json={},
            )
            assert ws_res.status_code == 201

            # Intake interpret returns 503 ASSISTANT_DISABLED
            interpret_res = client.post(
                "/api/intake/interpret",
                headers={"origin": "https://example.com"},
                json={"text": SAMPLE_TEXT},
            )
            assert interpret_res.status_code == 503
            assert interpret_res.json()["error"]["code"] == "ASSISTANT_DISABLED"


def test_real_hosted_interpreter_factory_requires_credentials() -> None:
    """_real_hosted_interpreter raises ValueError if groq_api_key or database_url is missing."""
    from pathlib import Path

    clock = StubClock()
    settings_no_key = Settings(
        db_path=Path("dummy.db"),
        allowed_origins=("https://example.com",),
        cookie_secure=True,
        runtime="hosted",
        store="postgres",
        database_url=DUMMY_DB_URL,
        assistant_provider="groq",
        assistant_enabled=False,
        tasks_enabled=False,
        groq_api_key=None,
    )
    with pytest.raises(ValueError, match="Hosted interpreter requires an explicit groq_api_key"):
        _real_hosted_interpreter(settings_no_key, clock)

    settings_with_key = Settings(
        db_path=Path("dummy.db"),
        allowed_origins=("https://example.com",),
        cookie_secure=True,
        runtime="hosted",
        store="postgres",
        database_url=DUMMY_DB_URL,
        assistant_provider="groq",
        assistant_enabled=False,
        tasks_enabled=False,
        groq_api_key=DUMMY_KEY,
    )
    interp = _real_hosted_interpreter(settings_with_key, clock)
    assert isinstance(interp, StrandsGroqInterpreter)
