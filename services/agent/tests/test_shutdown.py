"""Application ownership of in-flight interpretations, and a bounded drain.

Deterministic in-process doubles only. No model is invoked and no socket is
opened: the interpretations here are plain coroutines that watch the same
cooperative cancel signal a real adapter watches.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Coroutine
from threading import Event

import pytest
from fastapi.testclient import TestClient

from borrowed_steps.application.interpreter import Interpretation, InventoryReader
from borrowed_steps.config import Settings
from borrowed_steps.interfaces.http.app import create_app
from borrowed_steps.interfaces.http.inference_slot import DrainReport, InferenceRegistry
from conftest import FakeClock
from support import error_code, start_workspace

TEXT = "Meena R needs a wheelchair at the Velachery room by 2026-09-14T09:00:00Z."


async def _cooperative(cancel: Event, started: asyncio.Event) -> str:
    """Winds down when its cancel signal is set, as the real adapter does."""
    started.set()
    while not cancel.is_set():
        await asyncio.sleep(0.01)
    return "wound down"


async def _deaf(started: asyncio.Event) -> str:
    """Never notices the cooperative signal, as a native provider call cannot.

    This is what task cancellation exists for: the threading Event is invisible
    from inside an await on a socket, so only cancelling the task ends it.
    """
    started.set()
    await asyncio.Event().wait()
    return "never"  # pragma: no cover - unreachable


async def _unstoppable(started: asyncio.Event, released: asyncio.Event) -> str:
    """Refuses both signals: swallows cancellation and keeps going.

    Nothing in this process can end such a run, which is exactly the case the
    drain must report as abandoned instead of waiting out or calling clean.
    """
    started.set()
    while not released.is_set():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            continue
    return "let go"


def _register(
    registry: InferenceRegistry, coro: Coroutine[object, object, str], cancel: Event
) -> asyncio.Task[str]:
    task: asyncio.Task[str] = asyncio.create_task(coro)
    registry.register(task, cancel)
    return task


# --- the registry itself -------------------------------------------------


def test_a_drain_with_nothing_running_reports_nothing() -> None:
    registry = InferenceRegistry()

    report = asyncio.run(registry.drain(1.0))

    assert (report.requested, report.ended, report.abandoned) == (0, 0, 0)
    assert report.clean is True
    assert registry.closing is True


def test_a_drain_signals_and_cancels_every_run_and_waits_for_it() -> None:
    async def run() -> tuple[DrainReport, tuple[asyncio.Task[str], asyncio.Task[str]], Event]:
        registry = InferenceRegistry()
        first, second = Event(), Event()
        started_a, started_b = asyncio.Event(), asyncio.Event()
        task_a = _register(registry, _cooperative(first, started_a), first)
        task_b = _register(registry, _cooperative(second, started_b), second)
        await started_a.wait()
        await started_b.wait()
        assert registry.active == 2

        report = await registry.drain(5.0)
        return report, (task_a, task_b), first

    report, tasks, first = asyncio.run(run())

    assert (report.requested, report.ended, report.abandoned) == (2, 2, 0)
    assert report.clean is True
    assert first.is_set(), "the cooperative signal is still sent"
    assert all(task.cancelled() for task in tasks), "and the tasks are really cancelled"


def test_a_drain_ends_a_run_the_cooperative_signal_cannot_reach() -> None:
    """The case the Event alone could never handle, and the reason for cancelling.

    ``_deaf`` never looks at its cancel Event — exactly like a provider call
    awaiting a socket. Before BS-003-R4 the drain set the Event, waited out its
    whole timeout and reported the run abandoned. Now it ends.
    """

    async def run() -> tuple[DrainReport, asyncio.Task[str]]:
        registry = InferenceRegistry()
        cancel = Event()
        started = asyncio.Event()
        task = _register(registry, _deaf(started), cancel)
        await started.wait()

        report = await registry.drain(5.0)
        return report, task

    report, task = asyncio.run(run())

    assert (report.requested, report.ended, report.abandoned) == (1, 1, 0)
    assert report.clean is True
    assert task.cancelled()


def test_a_run_that_refuses_to_stop_is_reported_not_claimed_clean() -> None:
    """A bounded drain must never describe an unfinished run as a clean exit.

    This run swallows cancellation, so neither signal ends it. The drain is
    bounded, gives up, and says so.
    """

    async def run() -> DrainReport:
        registry = InferenceRegistry()
        cancel = Event()
        started, released = asyncio.Event(), asyncio.Event()
        task = _register(registry, _unstoppable(started, released), cancel)
        await started.wait()

        report = await registry.drain(0.2)
        released.set()  # the test's own cleanup, not part of the drain
        assert await asyncio.wait_for(task, timeout=5) == "let go"
        assert cancel.is_set(), "the signal was still sent"
        return report

    report = asyncio.run(run())

    assert (report.requested, report.ended, report.abandoned) == (1, 0, 1)
    assert report.clean is False


def test_a_finished_run_is_forgotten_and_never_drained() -> None:
    async def run() -> DrainReport:
        registry = InferenceRegistry()
        cancel = Event()
        started = asyncio.Event()
        task = _register(registry, _cooperative(cancel, started), cancel)
        await started.wait()
        cancel.set()
        await task
        await asyncio.sleep(0)  # let the done callback run
        assert registry.active == 0
        return await registry.drain(1.0)

    assert asyncio.run(run()).requested == 0


def test_a_run_registered_after_a_drain_began_is_stopped_at_once() -> None:
    async def run() -> tuple[asyncio.Task[str], Event]:
        registry = InferenceRegistry()
        await registry.drain(1.0)
        cancel = Event()
        started = asyncio.Event()
        task = _register(registry, _cooperative(cancel, started), cancel)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        return task, cancel

    task, cancel = asyncio.run(run())

    assert cancel.is_set()
    assert task.cancelled()


# --- the route, once shutdown has begun ----------------------------------


class RefusingInterpreter:
    """Records whether it was ever asked to interpret. It never should be."""

    def __init__(self) -> None:
        self.calls = 0

    async def interpret(
        self, text: str, inventory: InventoryReader, cancel: Event
    ) -> Interpretation:
        self.calls += 1  # pragma: no cover - the point is that this never runs
        msg = "no interpretation should have been started"
        raise AssertionError(msg)


def test_a_closing_service_refuses_new_interpretations(
    settings: Settings, clock: FakeClock
) -> None:
    """Once the drain has begun, the route must decline rather than start work."""
    assistant = RefusingInterpreter()
    app = create_app(
        dataclasses.replace(settings, assistant_enabled=True),
        clock=clock,
        interpreter=assistant,
    )

    with TestClient(app) as client:
        start_workspace(client)
        registry = app.state.inference
        assert isinstance(registry, InferenceRegistry)
        asyncio.run(registry.drain(1.0))

        refused = client.post("/api/intake/interpret", json={"text": TEXT})

    assert refused.status_code == 503
    assert error_code(refused) == "ASSISTANT_UNAVAILABLE"
    assert assistant.calls == 0, "no inference may start during shutdown"


class WindingDownInterpreter:
    """Blocks until its cancel signal is set, then returns an empty draft."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def interpret(
        self, text: str, inventory: InventoryReader, cancel: Event
    ) -> Interpretation:
        self.started.set()
        while not cancel.is_set():
            await asyncio.sleep(0.01)
        msg = "cancelled during shutdown"
        raise TimeoutError(msg)


def test_application_shutdown_drains_a_running_interpretation(
    settings: Settings, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """A run left in flight at shutdown is signalled and waited for, then logged.

    The HTTP layer is bypassed on purpose: the point under test is that the
    *application*, not the request handler, owns the task once it is registered.
    """
    caplog.set_level(logging.INFO, logger="borrowed_steps")
    app = create_app(
        dataclasses.replace(settings, assistant_enabled=True),
        clock=clock,
        interpreter=WindingDownInterpreter(),
        assistant_shutdown_seconds=5.0,
    )
    registry = app.state.inference
    assert isinstance(registry, InferenceRegistry)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            cancel = Event()
            started = asyncio.Event()
            _register(registry, _cooperative(cancel, started), cancel)
            await started.wait()
            assert registry.active == 1
        # Leaving the context runs the shutdown drain.
        assert registry.active == 0
        assert registry.closing is True

    asyncio.run(run())

    assert "all 1 in-flight interpretations ended" in caplog.text


def test_drain_bounds_shutdown_with_stalled_cleanup() -> None:
    """Stalled cleanup resolution must not cause drain to exceed its deadline."""

    class StalledCleanup:
        cleanup_unresolved = True

        async def resolve_cleanup(self) -> bool:
            await asyncio.Event().wait()
            return True

    async def run() -> DrainReport:
        registry = InferenceRegistry(StalledCleanup())
        task = asyncio.create_task(registry.drain(0.01))
        done, _ = await asyncio.wait({task}, timeout=0.1)
        assert task in done, "drain must finish within deadline when cleanup is stalled"
        return task.result()

    report = asyncio.run(run())
    assert report.clean is False
    assert report.cleanup_resolved is False


def test_drain_bounds_shutdown_with_cancellation_resistant_cleanup() -> None:
    """Cleanup that swallows CancelledError must not hang supervisor drain."""

    class CancellationResistantCleanup:
        cleanup_unresolved = True

        def __init__(self) -> None:
            self.stopped = asyncio.Event()

        async def resolve_cleanup(self) -> bool:
            while not self.stopped.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.01)
            return False

    async def run() -> DrainReport:
        cleanup = CancellationResistantCleanup()
        registry = InferenceRegistry(cleanup)
        task = asyncio.create_task(registry.drain(0.01))
        done, _ = await asyncio.wait({task}, timeout=0.1)
        assert task in done, "drain must finish within deadline when cleanup resists cancellation"
        report = task.result()
        cleanup.stopped.set()
        return report

    report = asyncio.run(run())
    assert report.clean is False
    assert report.cleanup_resolved is False


def test_drain_retains_tasks_and_blocks_new_work_when_cleanup_fails() -> None:
    """Unresolved cleanup leaves ownership intact and new interpretations blocked."""

    class StalledCleanup:
        cleanup_unresolved = True

        async def resolve_cleanup(self) -> bool:
            await asyncio.Event().wait()
            return True

    async def run() -> tuple[DrainReport, InferenceRegistry]:
        cleanup = StalledCleanup()
        registry = InferenceRegistry(cleanup)
        cancel = Event()
        started = asyncio.Event()
        _register(registry, _cooperative(cancel, started), cancel)
        await started.wait()

        report = await registry.drain(0.02)
        return report, registry

    report, registry = asyncio.run(run())
    assert report.requested == 1
    assert report.ended == 1
    assert report.cleanup_resolved is False
    assert report.clean is False
    assert registry.closing is True
    assert registry.cleanup_unresolved is True
    assert registry.settle() is False, "settle refuses release while cleanup is unresolved"
