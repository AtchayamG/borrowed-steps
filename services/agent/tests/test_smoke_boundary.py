"""The smoke proof checked against a diagnostic the real adapter produced.

Every other smoke test hands ``_check_case`` a diagnostic dictionary written by
hand in the test. That is useful for the failure branches, but it cannot catch a
checker that disagrees with the adapter — which is exactly what BS-003-R5 did:
it required ``stage == "complete"``, a value the adapter has never set, so every
genuinely successful run would have been reported as a failed proof while the
hand-written fakes said everything was fine.

This test closes that loop. The real ``StrandsOllamaInterpreter`` runs inside the
real application, served over a real loopback HTTP socket, and the real smoke
``Client`` reads the real ``X-Assistant-Diagnostic`` header off the wire and
feeds it to the real ``_check_case``. The only fake is the provider transport:
an in-process ``ollama.AsyncClient`` stand-in. No model runs and no socket to a
model is opened.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import ollama
import pytest
import uvicorn

from borrowed_steps.config import Settings
from borrowed_steps.infrastructure.strands_interpreter import StrandsOllamaInterpreter
from borrowed_steps.infrastructure.system import SystemClock
from borrowed_steps.interfaces.http.app import create_app

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import assistant_smoke  # noqa: E402

CASE_ONE = assistant_smoke._cases()[0]
DUE_AT = CASE_ONE["expect"]["due_at"][0]

# Every value is an exact span of case one's own message, which is what
# exact-source grounding requires. Nothing here is invented.
_EXTRACTION = {
    "borrower_label": "Meena R",
    "equipment_kind": "wheelchair",
    "pickup_location": "the Velachery equipment room",
    "due_at": DUE_AT,
}


class _Function:
    def __init__(self, name: str) -> None:
        self.name = name
        self.arguments: dict[str, Any] = {}


class _ToolCall:
    def __init__(self, name: str) -> None:
        self.function = _Function(name)


class _Msg:
    def __init__(self, content: str = "", tool_calls: list[_ToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Chunk:
    def __init__(self, message: _Msg, done_reason: str | None = "stop") -> None:
        self.message = message
        self.done_reason = done_reason
        self.prompt_eval_count = 1
        self.eval_count = 1
        self.total_duration = 1_000_000


class _Stream:
    def __init__(self, chunks: list[_Chunk], owner: FakeOllamaClient) -> None:
        self._chunks = list(chunks)
        self._owner = owner

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> _Chunk:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self._owner.streams_closed += 1


class FakeOllamaClient:
    """In-process stand-in for ollama.AsyncClient. Opens no socket.

    Sequenced for the real two-stage path: chat 1 asks for the inventory tool,
    chat 2 replies with prose, chat 3 is the non-streaming schema request.
    """

    created: ClassVar[list[FakeOllamaClient]] = []

    def __init__(self, host: str | None = None, **kwargs: object) -> None:
        self.host = host
        self.kwargs = kwargs
        self.chats = 0
        self.closed = 0
        self.streams_closed = 0
        FakeOllamaClient.created.append(self)

    async def chat(self, **request: object) -> object:
        self.chats += 1
        if request.get("stream") is False:
            return _Chunk(_Msg(content=json.dumps(_EXTRACTION)))
        if self.chats == 1:
            return _Stream([_Chunk(_Msg(tool_calls=[_ToolCall("read_inventory")]))], self)
        return _Stream([_Chunk(_Msg(content="checked"))], self)

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture
def live_base_url(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The real app, with the real adapter, on a real loopback port.

    A real socket is used deliberately: the smoke ``Client`` is urllib, and the
    point of this test is that the diagnostic survives the actual wire. The only
    thing faked is the provider transport, so nothing reaches a model.
    """
    FakeOllamaClient.created = []
    monkeypatch.setattr(ollama, "AsyncClient", FakeOllamaClient)

    app = create_app(
        dataclasses.replace(settings, assistant_enabled=True),
        clock=SystemClock(),
        interpreter=StrandsOllamaInterpreter(
            host="http://127.0.0.1:11434",
            model_id=assistant_smoke.EXPECTED_MODEL,
            clock=SystemClock(),
        ),
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 20.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:  # pragma: no cover - only on a hopelessly slow host
        server.should_exit = True
        thread.join(timeout=10)
        pytest.fail("the test server did not start")

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)


def test_the_checker_accepts_a_diagnostic_the_real_adapter_produced(
    live_base_url: str,
) -> None:
    """The full boundary: real adapter, real header, real Client, real checker.

    This is the test BS-003-R5 did not have. It fails on the R5 code, because
    ``_check_case`` demanded a stage the adapter never emits, and no amount of
    hand-written fixtures would have shown that.
    """
    client = assistant_smoke.Client(live_base_url)

    status, health = client.call("GET", "/api/health")
    assert status == 200, health
    assert health["agent_mode"] == "strands_ollama"

    status, _created = client.call("POST", "/api/workspaces", {})
    assert status == 201

    status, before = client.call("GET", "/api/snapshot")
    assert status == 200

    status, payload = client.call("POST", "/api/intake/interpret", {"text": CASE_ONE["text"]})
    assert status == 200, payload

    diagnostic = client.last_diagnostic
    assert diagnostic is not None, "the adapter's diagnostic did not survive the wire"
    # The value the checker used to demand was never produced by anything.
    assert diagnostic["stage"] == assistant_smoke.TERMINAL_SUCCESS_STAGE
    assert diagnostic["stage"] != "complete"

    status, after = client.call("GET", "/api/snapshot")
    assert status == 200

    failures = assistant_smoke._check_case(
        CASE_ONE,
        200,
        payload,
        snapshot_unchanged=after == before,
        diagnostic=diagnostic,
        seen_correlation_ids=set(),
        snapshot_statuses=(200, 200),
    )

    assert failures == [], "a genuinely successful interpretation failed the proof"


def test_the_real_adapter_never_emits_the_stage_the_checker_used_to_demand() -> None:
    """States the mismatch directly, so it cannot silently return.

    The adapter's stage vocabulary is fixed and small; ``complete`` is not in it.
    """
    source = (
        Path(assistant_smoke.__file__).resolve().parent.parent
        / "src"
        / "borrowed_steps"
        / "infrastructure"
        / "strands_interpreter.py"
    ).read_text(encoding="utf-8")

    assigned = {
        line.split("=", 1)[1].strip().strip('"')
        for line in source.splitlines()
        if line.strip().startswith("telemetry.stage =")
    }

    assert assigned == {"inventory", "inventory_recovery", "extraction"}
    assert "complete" not in assigned
    assert assistant_smoke.TERMINAL_SUCCESS_STAGE in assigned
