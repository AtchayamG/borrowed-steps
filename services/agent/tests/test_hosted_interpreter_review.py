"""Independent regression checks for the hosted execution boundary; offline only."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql
from starlette.testclient import TestClient

from borrowed_steps.application.errors import (
    AssistantBusyError,
    AssistantInvalidOutputError,
    AssistantTimeoutError,
    AssistantUnavailableError,
)
from borrowed_steps.config import Settings
from borrowed_steps.infrastructure.groq_model import GroqModel
from borrowed_steps.infrastructure.inference_admission import (
    AdmissionState,
    AdmissionUnavailableError,
    InferenceAdmissionStore,
)
from borrowed_steps.infrastructure.inventory import StoreInventoryReader
from borrowed_steps.infrastructure.postgres_store import PostgresStore
from borrowed_steps.infrastructure.strands_common import _Extraction
from borrowed_steps.infrastructure.strands_groq_interpreter import StrandsGroqInterpreter
from borrowed_steps.infrastructure.system import SecretsIdGenerator
from borrowed_steps.interfaces.http.app import create_app
from postgres_support import migrated_database
from test_strands_groq_interpreter import (
    DUMMY_DB_URL,
    DUMMY_KEY,
    SAMPLE_TEXT,
    SpyAdmissionStore,
    StubClock,
    StubInventoryReader,
    _make_success_mock_transport,
)


def make_interpreter(store: SpyAdmissionStore) -> StrandsGroqInterpreter:
    return StrandsGroqInterpreter(
        api_key=DUMMY_KEY,
        database_url=DUMMY_DB_URL,
        clock=StubClock(),
        transport=_make_success_mock_transport(),
        admission_store=store,  # type: ignore[arg-type]
    )


def test_lost_settlement_confirmation_never_returns_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SpyAdmissionStore()

    def fail_finish(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        raise AdmissionUnavailableError("synthetic lost commit confirmation")

    monkeypatch.setattr(store, "finish", fail_finish)
    interpreter = make_interpreter(store)
    with pytest.raises(AssistantUnavailableError):
        asyncio.run(interpreter.interpret(SAMPLE_TEXT, StubInventoryReader(), Event()))
    assert not interpreter.cleanup_unresolved


@pytest.mark.parametrize("field", ["reservation_id", "owner_id"])
def test_invalid_explicit_execution_identity_is_not_replaced(field: str) -> None:
    store = SpyAdmissionStore()
    interpreter = make_interpreter(store)
    with pytest.raises(AssistantUnavailableError):
        asyncio.run(
            interpreter.interpret(SAMPLE_TEXT, StubInventoryReader(), Event(), **{field: "invalid"})
        )
    assert store.reserve_calls == []


def test_generated_workspace_identity_is_supported() -> None:
    store = SpyAdmissionStore()
    wid = SecretsIdGenerator().new_id()
    result = asyncio.run(
        make_interpreter(store).interpret(SAMPLE_TEXT, StubInventoryReader(wid), Event())
    )
    assert result.draft.borrower_label == "Priya S"
    assert store.reserve_calls[0].workspace_id == wid


def test_no_fabricated_send_count(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stages(*args: Any, **kwargs: Any) -> _Extraction:  # noqa: ANN401
        return _Extraction(
            borrower_label=None, equipment_kind=None, pickup_location=None, due_at=None
        )

    monkeypatch.setattr(StrandsGroqInterpreter, "_stages", stages)
    store = SpyAdmissionStore()
    with pytest.raises(AssistantInvalidOutputError):
        asyncio.run(make_interpreter(store).interpret(SAMPLE_TEXT, StubInventoryReader(), Event()))
    assert store.finish_calls[0]["actual_sends"] == 0


def test_admission_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SpyAdmissionStore()
    original = store.reserve

    def slow_reserve(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        time.sleep(0.25)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "reserve", slow_reserve)

    async def scenario() -> None:
        started = time.monotonic()
        task = asyncio.create_task(
            make_interpreter(store).interpret(SAMPLE_TEXT, StubInventoryReader(), Event())
        )
        await asyncio.sleep(0.03)
        latency = time.monotonic() - started
        await task
        assert latency < 0.2

    asyncio.run(scenario())


def test_provider_constructor_failure_keeps_dispatch_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_constructor(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        raise RuntimeError("synthetic provider construction error")

    monkeypatch.setattr(GroqModel, "__init__", fail_constructor)
    store = SpyAdmissionStore()
    with pytest.raises(AssistantUnavailableError):
        asyncio.run(make_interpreter(store).interpret(SAMPLE_TEXT, StubInventoryReader(), Event()))
    assert len(store.dispatch_calls) == 1
    assert store.finish_calls[0]["state"] == AdmissionState.FAILED_CONFIRMED
    assert store.finish_calls[0]["actual_sends"] == 0


@pytest.mark.parametrize("phase", ["reserve", "mark_dispatched"])
def test_cancel_while_db_call_is_in_flight_never_constructs_provider(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    store = SpyAdmissionStore()
    entered, release = Event(), Event()
    original = getattr(store, phase)
    constructed: list[bool] = []

    def slow_call(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    def forbidden_model(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        constructed.append(True)
        raise AssertionError("Provider constructed after cancellation")

    monkeypatch.setattr(store, phase, slow_call)
    monkeypatch.setattr(GroqModel, "__init__", forbidden_model)

    async def scenario() -> None:
        task = asyncio.create_task(
            make_interpreter(store).interpret(SAMPLE_TEXT, StubInventoryReader(), Event())
        )
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(scenario())
    assert not constructed
    assert not store.finish_calls


@pytest.mark.parametrize("task_cancel", [False, True])
def test_cancellation_during_actual_cleanup_retains_uncertainty(task_cancel: bool) -> None:
    async def scenario() -> None:
        store = SpyAdmissionStore()
        entered, release = asyncio.Event(), asyncio.Event()
        transport = _make_success_mock_transport()

        class ClosingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                return await transport.handle_async_request(request)

            async def aclose(self) -> None:
                entered.set()
                await release.wait()
                await transport.aclose()

        interpreter = make_interpreter(store)
        interpreter._transport = ClosingTransport()
        cancel = Event()
        task = asyncio.create_task(
            interpreter.interpret(SAMPLE_TEXT, StubInventoryReader(), cancel)
        )
        await asyncio.wait_for(entered.wait(), 2)
        if task_cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert interpreter.cleanup_unresolved
            release.set()
        else:
            cancel.set()
            release.set()
            with pytest.raises(AssistantTimeoutError):
                await task
        assert store.finish_calls[0]["state"] == AdmissionState.UNCERTAIN
        assert await interpreter.resolve_cleanup()
        # Late cleanup must never automatically change the durable uncertain row.
        assert len(store.finish_calls) == 1

    asyncio.run(scenario())


def test_http_created_workspace_real_inventory_and_durable_replay() -> None:
    with migrated_database() as url:
        settings = Settings(
            db_path=Path("unused.db"),
            runtime="hosted",
            store="postgres",
            database_url=url,
            assistant_provider="groq",
            tasks_enabled=False,
            cookie_secure=True,
            allowed_origins=("https://example.com",),
        )
        with TestClient(create_app(settings), base_url="https://example.com") as client:
            response = client.post(
                "/api/workspaces", headers={"origin": "https://example.com"}, json={}
            )
            assert response.status_code == 201
            wid = response.json()["workspace"]["id"]
            before = client.get("/api/snapshot").json()
            assert len(wid) == 24

            def business_rows() -> list[Any]:
                with psycopg.connect(url) as conn:
                    return [
                        conn.execute(
                            sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
                        ).fetchall()
                        for table in (
                            "workspaces",
                            "sessions",
                            "equipment",
                            "requests",
                            "loans",
                            "events",
                            "tasks",
                            "idempotency",
                        )
                    ]

            rows_before = business_rows()
            mock = _make_success_mock_transport()
            sends: list[int] = []

            async def wire(request: httpx.Request) -> httpx.Response:
                with psycopg.connect(url) as conn:
                    assert conn.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() "
                        "AND pid<>pg_backend_pid() AND xact_start IS NOT NULL"
                    ).fetchone() == (0,)
                sends.append(1)
                return await mock.handle_async_request(request)

            store = InferenceAdmissionStore(url)
            reader = StoreInventoryReader(PostgresStore(url), wid)
            rid, owner = str(uuid4()), str(uuid4())
            interpreter = StrandsGroqInterpreter(
                api_key=DUMMY_KEY,
                database_url=url,
                clock=StubClock(),
                transport=httpx.MockTransport(wire),
            )
            result = asyncio.run(
                interpreter.interpret(
                    SAMPLE_TEXT, reader, Event(), reservation_id=rid, owner_id=owner
                )
            )
            assert result.inventory_tool_calls == 1
            assert len(sends) == 3
            row = store.get(rid)
            assert row is not None
            assert row["state"] == "SUCCEEDED"
            assert row["cleanup_completed"] is True
            assert store.get_by_request_key(wid, row["request_key_hash"]) == row
            with pytest.raises(AssistantUnavailableError, match="dispatch was refused"):
                asyncio.run(
                    interpreter.interpret(
                        SAMPLE_TEXT, reader, Event(), reservation_id=rid, owner_id=owner
                    )
                )
            assert len(sends) == 3
            assert store.get(rid) == row
            assert client.get("/api/snapshot").json() == before
            assert business_rows() == rows_before


def test_two_instances_compete_while_provider_is_in_flight() -> None:
    with migrated_database() as url:
        wids = [str(uuid4()), str(uuid4())]
        with psycopg.connect(url) as conn:
            for wid in wids:
                conn.execute("INSERT INTO workspaces(id,created_at) VALUES(%s,now())", (wid,))

        async def scenario() -> None:
            entered, release = asyncio.Event(), asyncio.Event()
            mock = _make_success_mock_transport()

            async def wire(request: httpx.Request) -> httpx.Response:
                entered.set()
                await release.wait()
                return await mock.handle_async_request(request)

            first = StrandsGroqInterpreter(
                api_key=DUMMY_KEY,
                database_url=url,
                clock=StubClock(),
                transport=httpx.MockTransport(wire),
            )
            second = StrandsGroqInterpreter(
                api_key=DUMMY_KEY,
                database_url=url,
                clock=StubClock(),
                transport=_make_success_mock_transport(),
            )
            task = asyncio.create_task(
                first.interpret(SAMPLE_TEXT, StubInventoryReader(wids[0]), Event())
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                with pytest.raises(AssistantBusyError):
                    await second.interpret(SAMPLE_TEXT, StubInventoryReader(wids[1]), Event())
            finally:
                release.set()
                await task

        asyncio.run(scenario())
