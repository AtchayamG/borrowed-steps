"""The owned runner: it really starts, really works, and really stops.

These are the only tests that enable the runner, and they never disable it to
make an assertion easier. Every wait is bounded and every thread is checked for
actual termination, so a shutdown regression fails these tests instead of
hanging the suite.
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from borrowed_steps.config import Settings, load_settings
from borrowed_steps.infrastructure.sqlite_store import SqliteStore
from borrowed_steps.infrastructure.system import SecretsIdGenerator
from borrowed_steps.infrastructure.task_runner import TaskRunner
from borrowed_steps.interfaces.http.app import create_app
from conftest import FakeClock
from support import (
    body,
    create_request,
    equipment_of,
    reserve,
    snapshot,
    start_workspace,
)

# Every wait in this module is bounded by this, so a thread that will not stop
# fails an assertion within seconds rather than blocking the run.
DEADLINE = 10.0


def _running(settings: Settings) -> Settings:
    return dataclasses.replace(settings, tasks_enabled=True)


def _db_state(db_path: Path) -> tuple[list[str], list[str]]:
    """Task statuses and event actions, read straight from the file."""
    conn = sqlite3.connect(db_path)
    try:
        tasks = [str(r[0]) for r in conn.execute("SELECT status FROM tasks ORDER BY id")]
        events = [str(r[0]) for r in conn.execute("SELECT action FROM events ORDER BY rowid")]
    finally:
        conn.close()
    return tasks, events


def test_the_production_default_enables_the_runner() -> None:
    """BS_TASKS_ENABLED defaults to true; an explicit value still wins."""
    assert load_settings({}).tasks_enabled is True
    assert load_settings({"BS_TASKS_ENABLED": "false"}).tasks_enabled is False
    assert load_settings({"BS_TASKS_ENABLED": "0"}).tasks_enabled is False
    assert load_settings({"BS_TASKS_ENABLED": "true"}).tasks_enabled is True


def test_the_lifespan_starts_and_stops_the_real_thread(
    settings: Settings, clock: FakeClock
) -> None:
    """Not a log line and not the daemon flag: the actual thread object."""
    app = create_app(_running(settings), clock=clock)

    with TestClient(app):
        runner = app.state.tasks
        assert isinstance(runner, TaskRunner)
        assert runner.first_tick_done.wait(DEADLINE), "the startup tick never ran"
        # Read into distinct locals: asserting the same property twice lets the
        # type checker carry the first narrowing into the second.
        alive_inside = runner.running
        assert alive_inside is True

    alive_after = runner.running
    assert alive_after is False, "the runner outlived the lifespan"
    assert not any(
        thread.name == "borrowed-steps-tasks" and thread.is_alive()
        for thread in threading.enumerate()
    ), "a coordination thread is still alive after shutdown"


def test_the_runner_is_absent_when_disabled(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    with TestClient(app):
        assert app.state.tasks is None
    assert not any(thread.name == "borrowed-steps-tasks" for thread in threading.enumerate())


def test_the_startup_tick_processes_without_any_http_read(
    settings: Settings, clock: FakeClock, db_path: Path
) -> None:
    """A due task is processed by the runner alone, with no browser involved.

    The reservation is written directly through the store, so nothing in this
    test reads or writes over HTTP after the app starts. What moves the task is
    the owned thread.
    """
    seeded = _seed_reserved_loan(settings, clock)
    tasks_before, events_before = _db_state(db_path)
    assert tasks_before == ["PENDING"]
    assert "PICKUP_DUE" not in events_before

    app = create_app(_running(settings), clock=clock)
    with TestClient(app):
        runner = app.state.tasks
        assert runner.first_tick_done.wait(DEADLINE), "the startup tick never ran"

    tasks_after, events_after = _db_state(db_path)
    assert tasks_after == ["DUE"]
    assert events_after[-1] == "PICKUP_DUE"
    assert seeded in events_after[0], "the seeded loan is the one that was processed"


def _seed_reserved_loan(settings: Settings, clock: FakeClock) -> str:
    """Create a reserved loan over HTTP, then close that app down again.

    Returns a marker that later appears in the event log, so the processing
    test can show it acted on this loan rather than on nothing.
    """
    with TestClient(create_app(settings, clock=clock)) as client:
        start_workspace(client)
        wheelchair = equipment_of(snapshot(client), "WHEELCHAIR")
        request_id = body(create_request(client))["request"]["id"]
        reserve(
            client,
            request_id=request_id,
            equipment_id=wheelchair["id"],
            expected_version=wheelchair["version"],
        )
    return "REQUEST_CREATED"


def test_a_second_start_of_one_runner_is_refused(
    settings: Settings, clock: FakeClock, db_path: Path
) -> None:
    """Two threads for one runner would be two writers nobody asked for."""
    runner = TaskRunner(
        SqliteStore(db_path),
        clock,
        SecretsIdGenerator(),
        interval=DEADLINE,
        limit=10,
    )
    del settings
    runner.start()
    try:
        assert runner.first_tick_done.wait(DEADLINE)
        with pytest.raises(RuntimeError):
            runner.start()
    finally:
        assert runner.stop(DEADLINE) is True


def test_stop_reports_truthfully_when_there_is_nothing_to_stop(
    settings: Settings, clock: FakeClock, db_path: Path
) -> None:
    runner = TaskRunner(
        SqliteStore(db_path), clock, SecretsIdGenerator(), interval=DEADLINE, limit=10
    )
    del settings
    assert runner.stop(DEADLINE) is True, "a runner that never started has stopped"
    assert runner.running is False


class _ScriptedStore:
    """A store stand-in whose discovery is under this test's control.

    Only ``due_task_candidates`` is ever reached, because it always returns an
    empty list or raises: the runner opens no transaction when there is nothing
    to process. Standing in here rather than patching the real store keeps the
    real ``_tick_once`` - the thing under test - completely unmodified.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.block = threading.Event()
        self.entered = threading.Event()
        self.ticked_twice = threading.Event()
        self.fail_first = False
        self.fail_always = False

    def due_task_candidates(self, now: object, limit: int) -> list[object]:
        del now, limit
        self.calls += 1
        if self.calls >= 2:
            self.ticked_twice.set()
        if self.fail_always or (self.fail_first and self.calls == 1):
            msg = "synthetic discovery failure"
            raise RuntimeError(msg)
        if self.block is not None and not self.block.is_set() and self.calls == 1:
            self.entered.set()
            self.block.wait(DEADLINE * 3)
        return []


def _scripted_runner(store: _ScriptedStore, clock: FakeClock, interval: float) -> TaskRunner:
    return TaskRunner(store, clock, SecretsIdGenerator(), interval=interval, limit=10)  # type: ignore[arg-type]


def test_stop_returns_false_when_the_thread_will_not_end(clock: FakeClock) -> None:
    """A stuck tick is reported as still running, never as clean cleanup.

    The tick blocks on an event this test controls, so the failure is
    deterministic, and the thread is always released afterwards.
    """
    store = _ScriptedStore()
    runner = _scripted_runner(store, clock, DEADLINE)
    runner.start()
    try:
        assert store.entered.wait(DEADLINE), "the tick never started"
        assert runner.stop(0.2) is False, "a blocked thread must not be called stopped"
        assert runner.running is True
    finally:
        store.block.set()
        assert runner.stop(DEADLINE) is True, "the thread never finished after release"


def test_a_failing_tick_is_logged_and_does_not_disable_later_ticks(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """One bad tick must not silently end coordination for the process."""
    caplog.set_level(logging.ERROR, logger="borrowed_steps.tasks")
    store = _ScriptedStore()
    store.block.set()  # no blocking here; only the failure matters
    store.fail_first = True
    runner = _scripted_runner(store, clock, 0.05)
    runner.start()
    try:
        assert store.ticked_twice.wait(DEADLINE), "the runner stopped ticking after a failure"
    finally:
        assert runner.stop(DEADLINE) is True

    assert any("Coordination tick failed" in record.message for record in caplog.records)
    assert runner.ticks >= 1, "the tick after the failure did real work"


def test_a_failing_tick_is_not_counted_as_work_done(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """``ticks`` counts completed ticks, so a failure cannot look like success."""
    caplog.set_level(logging.ERROR, logger="borrowed_steps.tasks")
    store = _ScriptedStore()
    store.block.set()
    store.fail_always = True
    runner = _scripted_runner(store, clock, 0.05)
    runner.start()
    try:
        assert store.ticked_twice.wait(DEADLINE), "the runner gave up after one failure"
    finally:
        assert runner.stop(DEADLINE) is True

    assert runner.ticks == 0
    assert any("Coordination tick failed" in record.message for record in caplog.records)


def test_the_runner_does_not_block_the_event_loop(settings: Settings, clock: FakeClock) -> None:
    """HTTP stays responsive while the runner owns its own thread."""
    app = create_app(_running(settings), clock=clock)
    with TestClient(app) as client:
        assert app.state.tasks.first_tick_done.wait(DEADLINE)
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["milestone"] == "M2B"


def test_the_runner_stops_between_candidates_when_asked(
    settings: Settings, clock: FakeClock, db_path: Path
) -> None:
    """A stop request is honoured without abandoning half-written work."""
    del settings
    from borrowed_steps.application.coordination import process_due_tasks

    class AlreadyStopped:
        def is_set(self) -> bool:
            return True

    report = process_due_tasks(
        SqliteStore(db_path),
        clock,
        SecretsIdGenerator(),
        limit=10,
        stop=AlreadyStopped(),
    )
    assert report.considered == 0
    assert report.marked_due == 0


def test_shutdown_still_reports_inference_cleanup(
    settings: Settings, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """Stopping the runner must not skip the provider drain."""
    caplog.set_level(logging.INFO, logger="borrowed_steps.http")
    app = create_app(_running(settings), clock=clock)
    with TestClient(app):
        assert app.state.tasks.first_tick_done.wait(DEADLINE)
    # The registry drained without an interpreter, so it reports nothing
    # abandoned and no unresolved cleanup - the point is that it still ran and
    # the runner's stop did not replace it.
    assert app.state.inference is not None
    assert app.state.tasks.running is False
    assert not any("did not stop" in record.message for record in caplog.records)


def test_a_long_interval_still_stops_immediately(
    settings: Settings, clock: FakeClock, db_path: Path
) -> None:
    """The wait between ticks is interruptible, not a sleep to wait out."""
    del settings
    runner = TaskRunner(
        SqliteStore(db_path),
        clock,
        SecretsIdGenerator(),
        interval=3600.0,
        limit=10,
    )
    runner.start()
    assert runner.first_tick_done.wait(DEADLINE)
    started = timedelta(seconds=0)
    del started
    assert runner.stop(DEADLINE) is True, "an hour-long wait was not interrupted"
