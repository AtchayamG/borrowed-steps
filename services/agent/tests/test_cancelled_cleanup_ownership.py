"""A close whose waiter was cancelled is still outstanding to everyone.

BS-003-R6 kept ownership in `_closing` but had every consumer — the refusal in
`interpret`, `cleanup_unresolved`, registry settlement, shutdown — consult a
second list that only `_close`'s failure tail appended to. A waiter cancelled
mid-close never reached that tail, so an open client with a live close task
reported nothing outstanding and its slot looked free.

The saved `BS-003-R6-cancel-repro.py` is the first test here. The rest drive the
same situation through the real interpretation and HTTP paths, because the
private helper agreeing with itself was exactly what the previous round proved
insufficient.

Local fakes only. No model runs and no socket to a model is opened. Every
cancellation-resistant task is explicitly released at the end of its test.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from threading import Event
from typing import cast

import pytest
from fastapi.testclient import TestClient

from borrowed_steps.application.interpreter import Interpretation
from borrowed_steps.config import Settings
from borrowed_steps.infrastructure import strands_interpreter as adapter
from borrowed_steps.infrastructure.owned_ollama import OwnedOllamaModel
from borrowed_steps.infrastructure.strands_interpreter import (
    StrandsOllamaInterpreter,
    _Telemetry,
)
from borrowed_steps.interfaces.http.app import create_app
from borrowed_steps.interfaces.http.inference_slot import InferenceRegistry
from conftest import FakeClock
from support import error_code, start_workspace
from test_strands_adapter import StubClock

TEXT = "Meena R needs a wheelchair at the Velachery room by 2026-09-14T09:00:00Z."


class SlowCloseModel:
    """A provider whose close blocks until released. Counts its closes."""

    client_open: bool

    def __init__(self) -> None:
        self.client_open = True
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.starts = 0
        self.active = 0
        self.peak = 0

    async def aclose(self) -> None:
        self.starts += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        self.client_open = False


def _as_model(fake: SlowCloseModel) -> OwnedOllamaModel:
    """Hand a fake to code typed for the real provider.

    It implements exactly the surface cleanup uses — ``aclose`` and
    ``client_open`` — which keeps these tests about ownership.
    """
    return cast("OwnedOllamaModel", fake)


def _interpreter() -> StrandsOllamaInterpreter:
    return StrandsOllamaInterpreter(
        host="http://127.0.0.1:11434",
        model_id="llama3.2:3b",
        clock=StubClock(),
    )


async def _release(owner: StrandsOllamaInterpreter, model: SlowCloseModel) -> None:
    """Let every owned close finish, so no test leaves a task running."""
    model.release.set()
    tasks = list(owner._closing.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_the_saved_cancel_reproduction() -> None:
    """`BS-003-R6-cancel-repro.py`, verbatim in shape, as a permanent regression.

    On the R6 code this printed ``client_open=True, owned_close_tasks=1,
    cleanup_unresolved=False, slot_releasable=True``: an open client, a live
    close task, and every consumer of ownership saying there was nothing
    outstanding.
    """

    async def main() -> None:
        owner = _interpreter()
        model = SlowCloseModel()

        task = asyncio.create_task(
            owner._close(_as_model(model), _Telemetry(correlation_id="offline"))
        )
        await model.started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        registry = InferenceRegistry(owner)

        assert model.client_open is True, "the client is still open"
        assert len(owner._closing) == 1, "and its close is still owned"
        assert owner.cleanup_unresolved is True, "so cleanup is outstanding"
        assert registry.settle() is False, "and the slot is not releasable"

        await _release(owner, model)

    asyncio.run(main())


def test_the_cancelled_close_is_the_same_one_shutdown_finds() -> None:
    """Shutdown must reconcile the very close that was left running.

    Not a new one beside it: `_close_operation` returns the task already in
    flight, so `starts` stays at one throughout.
    """

    async def main() -> None:
        owner = _interpreter()
        model = SlowCloseModel()

        waiter = asyncio.create_task(
            owner._close(_as_model(model), _Telemetry(correlation_id="offline"))
        )
        await model.started.wait()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)

        registry = InferenceRegistry(owner)
        report = await registry.drain(0.05)

        assert report.cleanup_resolved is False
        assert report.clean is False, "an open client is not a clean shutdown"
        assert model.starts == 1, "shutdown started a competing close"
        assert model.peak == 1

        await _release(owner, model)

    asyncio.run(main())


def test_a_close_that_finishes_later_reconciles_its_own_ownership() -> None:
    """Ownership is released by evidence, not by assumption.

    Nobody is waiting on this close any more. When it completes on its own, the
    next thing that asks about cleanup must see it as settled.
    """

    async def main() -> None:
        owner = _interpreter()
        model = SlowCloseModel()

        waiter = asyncio.create_task(
            owner._close(_as_model(model), _Telemetry(correlation_id="offline"))
        )
        await model.started.wait()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)

        outstanding_while_running = owner.cleanup_unresolved
        assert outstanding_while_running is True

        # The abandoned close now finishes on its own terms.
        model.release.set()
        await asyncio.gather(*list(owner._closing.values()), return_exceptions=True)

        outstanding_after = owner.cleanup_unresolved
        assert outstanding_after is False, "a finished close is still counted"
        assert owner._closing == {}
        assert model.client_open is False

        registry = InferenceRegistry(owner)
        assert registry.settle() is True, "the slot is releasable once nothing is held"

    asyncio.run(main())


# --- the same situation through interpret and through HTTP ---------------


class _ThreadReleasedClose:
    """A close released from another thread, as this HTTP test must do.

    The application runs on the test client's own event loop in its own thread,
    so the release signal cannot be an ``asyncio.Event`` set from the test
    thread — that would not wake the waiter. A ``threading.Event`` polled from
    inside the close is the honest way across that boundary.
    """

    client_open: bool

    def __init__(self) -> None:
        self.client_open = True
        self.release = Event()
        self.started = Event()
        self.starts = 0

    async def aclose(self) -> None:
        self.starts += 1
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        self.client_open = False


class _CancelDuringCleanupInterpreter:
    """An interpreter whose close is still running when its caller is cancelled.

    It stands in for the real adapter at the one moment that matters: the run
    is over, the client is not closed yet, and the waiter goes away. Its
    ownership answers come from the same place the real one's do — whether a
    close it started has finished with the client shut.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.emitted: list[str] = []
        self.model = _ThreadReleasedClose()
        self.close_task: asyncio.Task[None] | None = None

    @property
    def cleanup_unresolved(self) -> bool:
        task = self.close_task
        if task is None:
            return False
        if task.done() and not self.model.client_open:
            self.close_task = None
            return False
        return True

    async def resolve_cleanup(self) -> bool:
        task = self.close_task
        if task is None:
            return True
        await asyncio.wait({task}, timeout=0.05)
        return not self.cleanup_unresolved

    async def interpret(self, text: str, inventory: object, cancel: Event) -> Interpretation:
        self.calls += 1
        try:
            await asyncio.sleep(3600)
        finally:
            # The run is over; its client is not shut yet. Ownership is taken
            # here, before anything is awaited, so a cancellation landing in
            # this finally cannot lose it.
            if self.close_task is None:
                self.close_task = asyncio.create_task(self.model.aclose())
            try:
                await asyncio.wait({self.close_task}, timeout=5)
            except asyncio.CancelledError:
                self.emitted.append("interrupted:cancelled_during_cleanup")
                raise
            self.emitted.append("finished")
        msg = "unreachable: this fake only ever ends by cancellation"
        raise AssertionError(msg)


def test_a_run_cancelled_during_cleanup_keeps_its_slot_and_is_refused_over_http(
    settings: Settings, clock: FakeClock
) -> None:
    """End to end: cancel the caller mid-cleanup, then ask the service for more.

    The slot must not be handed on, the next caller must be refused rather than
    told the service is merely busy, and no second run or client may start.
    """
    assistant = _CancelDuringCleanupInterpreter()
    app = create_app(
        dataclasses.replace(settings, assistant_enabled=True),
        clock=clock,
        interpreter=assistant,
        assistant_deadline_seconds=0.05,
        assistant_cancel_grace_seconds=0.05,
    )

    with TestClient(app) as client:
        start_workspace(client)

        # The deadline fires and the route cancels the run. Its cleanup is
        # still in flight when the route gives up waiting and answers 504.
        timed_out = client.post("/api/intake/interpret", json={"text": TEXT})
        assert timed_out.status_code == 504
        assert error_code(timed_out) == "ASSISTANT_TIMEOUT"

        # Read into a local: asserting the same property twice would let the
        # type checker carry the first narrowing into the second.
        outstanding = assistant.cleanup_unresolved
        assert outstanding is True, "the open client is outstanding"
        assert assistant.emitted == [], (
            "nothing claimed the run had finished while its close was still running"
        )

        refused = client.post("/api/intake/interpret", json={"text": TEXT})
        assert refused.status_code == 503, "the slot was handed on with a client open"
        assert error_code(refused) == "ASSISTANT_UNAVAILABLE"
        assert assistant.calls == 1, "a second run started while cleanup was outstanding"
        assert assistant.model.starts == 1, "a second client close was started"
        # No slot-retention log is expected here: that line comes from the run's
        # done callback, and the run has not finished — its close is still going.
        # The refusal above is what proves the slot was not handed on.

        # Let the abandoned close finish; the service becomes usable again.
        assistant.model.release.set()
        settled = False
        for _ in range(500):
            settled = not assistant.cleanup_unresolved
            if settled:
                break
            time.sleep(0.01)
        assert settled is True, "the finished close was never reconciled"

        served = client.post("/api/intake/interpret", json={"text": TEXT})
        assert served.status_code == 504, "the service accepts work again"
        assert assistant.calls == 2

    # Nothing may be left running when the test ends.
    task = assistant.close_task
    if task is not None and not task.done():  # pragma: no cover - defensive
        task.cancel()


# --- the real interpret path, cancelled while its close is running -------


class _StuckCloseModel(SlowCloseModel):
    """Also stands in for the provider during the run itself."""

    async def __aenter__(self) -> _StuckCloseModel:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def test_interpret_cancelled_during_cleanup_emits_one_truthful_diagnostic(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The real `interpret` finally, interrupted mid-close.

    Its terminal diagnostic is the only account of what the run charged and
    what it left open, so it must still be written — once — and it must not
    describe the run as finished. The cancellation must still propagate.
    """
    caplog.set_level(logging.INFO, logger="borrowed_steps.assistant")
    model = _StuckCloseModel()
    monkeypatch.setattr(adapter, "OwnedOllamaModel", lambda **kwargs: model)

    class _Agent:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def invoke_async(self, prompt: str, **kwargs: object) -> object:
            msg = "the run failed before cleanup"
            raise RuntimeError(msg)

    monkeypatch.setattr(adapter, "Agent", _Agent)
    interpreter = _interpreter()

    async def main() -> None:
        from test_strands_adapter import StubInventory

        task = asyncio.create_task(interpreter.interpret(TEXT, StubInventory(), Event()))
        await model.started.wait()

        # Cancel while the close is running, twice, to prove the second one
        # cannot land between writing the record and propagating.
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert interpreter.cleanup_unresolved is True, "the open client is still owned"
        assert len(interpreter._closing) == 1
        assert model.starts == 1, "a competing close was started"

        await _release(interpreter, model)

    asyncio.run(main())

    terminal = [line for line in caplog.text.splitlines() if "cleanup=" in line]
    assert len(terminal) == 1, "exactly one terminal record, whatever the outcome"
    assert "cleanup=close_cancelled" in terminal[0]
    assert " success at " not in terminal[0], "a run with an open client is not a success"


def test_a_cancelled_cleanup_can_still_be_resolved_without_a_second_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown reconciles the abandoned close rather than starting another."""
    model = _StuckCloseModel()
    monkeypatch.setattr(adapter, "OwnedOllamaModel", lambda **kwargs: model)

    class _Agent:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def invoke_async(self, prompt: str, **kwargs: object) -> object:
            msg = "the run failed before cleanup"
            raise RuntimeError(msg)

    monkeypatch.setattr(adapter, "Agent", _Agent)
    interpreter = _interpreter()

    async def main() -> None:
        from test_strands_adapter import StubInventory

        task = asyncio.create_task(interpreter.interpret(TEXT, StubInventory(), Event()))
        await model.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        registry = InferenceRegistry(interpreter)
        assert registry.settle() is False, "the slot was releasable with a client open"

        # The close completes on its own; shutdown then finds nothing held.
        model.release.set()
        assert await interpreter.resolve_cleanup() is True
        assert model.starts == 1, "resolve_cleanup started a second close"
        assert registry.settle() is True

    asyncio.run(main())


# --- the real adapter, cancelled mid-cleanup by the HTTP route ----------


class _ThreadStuckCloseModel(_ThreadReleasedClose):
    """The provider for a real-adapter run driven over HTTP.

    The adapter enters the model and later calls ``aclose`` on it, so those are
    the two methods a stand-in needs. The release is a ``threading.Event``
    because it is set from the pytest thread while the close itself runs on the
    test client's portal thread.
    """

    async def __aenter__(self) -> _ThreadStuckCloseModel:
        return self


def test_the_real_adapter_keeps_its_slot_when_http_cancels_it_mid_cleanup(
    settings: Settings, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole path at once: real adapter, real route, real slot.

    The test above it proves the slot honours an interpreter that reports
    outstanding cleanup. This one proves the adapter actually reports it, which
    is the half BS-003-R6 got wrong: there the route's cancellation skipped the
    record every consumer read, so this second request was served instead of
    refused while a client was still open.
    """
    model = _ThreadStuckCloseModel()
    monkeypatch.setattr(adapter, "OwnedOllamaModel", lambda **kwargs: model)

    class _Agent:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def invoke_async(self, prompt: str, **kwargs: object) -> object:
            msg = "the run failed before cleanup"
            raise RuntimeError(msg)

    monkeypatch.setattr(adapter, "Agent", _Agent)
    interpreter = _interpreter()

    app = create_app(
        dataclasses.replace(settings, assistant_enabled=True),
        clock=clock,
        interpreter=interpreter,
        assistant_deadline_seconds=0.05,
        assistant_cancel_grace_seconds=0.05,
    )

    try:
        with TestClient(app) as client:
            start_workspace(client)

            # The run fails, its cleanup blocks, and the route's deadline fires
            # while the close is still going — the exact moment at issue.
            timed_out = client.post("/api/intake/interpret", json={"text": TEXT})
            assert timed_out.status_code == 504
            assert error_code(timed_out) == "ASSISTANT_TIMEOUT"
            assert model.started.wait(timeout=5) is True, "the close never began"

            outstanding = interpreter.cleanup_unresolved
            assert outstanding is True, "the cancelled close is still outstanding"
            assert len(interpreter._closing) == 1

            refused = client.post("/api/intake/interpret", json={"text": TEXT})
            assert refused.status_code == 503, "the slot was handed on with a client open"
            assert error_code(refused) == "ASSISTANT_UNAVAILABLE"
            assert model.starts == 1, "a second client close was started"

            # Released, the abandoned close finishes and reconciles itself.
            model.release.set()
            settled = False
            for _ in range(500):
                settled = not interpreter.cleanup_unresolved
                if settled:
                    break
                time.sleep(0.01)
            assert settled is True, "the finished close was never reconciled"
            assert model.starts == 1, "reconciling started another close"
    finally:
        # Whatever the assertions did, nothing is left blocked on the release.
        model.release.set()
