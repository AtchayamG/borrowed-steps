"""One owned close per model, kept across timeout, cancellation and shutdown.

Built around the saved BS-003-R5 reproduction, which showed two concurrent
``aclose`` calls running on one model after ``_close`` had already returned.

The invariant under test is narrow and mechanical: at most one close operation
exists per model at any moment, and whoever stops waiting for it does not lose
it. What is bounded here is the *waiting*. A close that resists cancellation
keeps running, and none of this claims to force it to stop — it claims only
that the process still owns it, still observes it, and will not start a second
one beside it.

Every fake here is local and in-process; no socket is opened and no model runs.
Each resistant task is explicitly released at the end of its test so nothing
can hang the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import cast

import pytest

from borrowed_steps.infrastructure import strands_interpreter as adapter
from borrowed_steps.infrastructure.owned_ollama import OwnedOllamaModel
from borrowed_steps.infrastructure.strands_interpreter import (
    StrandsOllamaInterpreter,
    _Telemetry,
)
from borrowed_steps.interfaces.http.inference_slot import InferenceRegistry
from test_strands_adapter import StubClock


class ResistantModel:
    """A provider whose close ignores cancellation until explicitly released.

    Counts concurrent closes, which is the whole point: ``peak`` above one means
    two closes were running on the same transport at once.
    """

    client_open: bool

    def __init__(self, *, fail_with: BaseException | None = None) -> None:
        self.client_open = True
        self.release = asyncio.Event()
        self.active = 0
        self.peak = 0
        self.starts = 0
        self._fail_with = fail_with

    async def aclose(self) -> None:
        self.starts += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            while not self.release.is_set():
                # Swallowing the cancellation is the point: this stands in for
                # a close that cannot be interrupted where it is.
                with contextlib.suppress(asyncio.CancelledError):
                    await self.release.wait()
        finally:
            self.active -= 1
        if self._fail_with is not None:
            raise self._fail_with
        self.client_open = False


class PromptModel:
    """A provider that closes immediately, for the ordinary path."""

    client_open: bool

    def __init__(self) -> None:
        self.client_open = True
        self.starts = 0

    async def aclose(self) -> None:
        self.starts += 1
        self.client_open = False


def _as_model(fake: ResistantModel | PromptModel) -> OwnedOllamaModel:
    """Hand a fake to code typed for the real provider.

    These fakes implement exactly the surface ``_close`` uses — ``aclose`` and
    ``client_open`` — and nothing else, which is what keeps the tests about
    ownership rather than about Ollama.
    """
    return cast("OwnedOllamaModel", fake)


def _interpreter() -> StrandsOllamaInterpreter:
    return StrandsOllamaInterpreter(
        host="http://127.0.0.1:11434",
        model_id="llama3.2:3b",
        clock=StubClock(),
    )


async def _release(owner: StrandsOllamaInterpreter, model: ResistantModel) -> None:
    """Let every owned close finish, so no test leaves a task running."""
    model.release.set()
    tasks = list(owner._closing.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
def _short_close_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound the wait tightly so these tests are fast and deterministic."""
    monkeypatch.setattr(adapter, "_CLOSE_TIMEOUT_SECONDS", 0.01)


def test_a_timed_out_close_is_never_joined_by_a_second_one() -> None:
    """The saved BS-003-R5 reproduction, as a permanent regression.

    Before the fix this printed ``peak concurrent closes=2``: the first close
    was cancelled, ignored the cancellation, and a second was started on top of
    it while it still ran.
    """

    async def main() -> tuple[ResistantModel, StrandsOllamaInterpreter]:
        owner = _interpreter()
        model = ResistantModel()

        failure = await owner._close(_as_model(model), _Telemetry(correlation_id="offline"))

        assert failure is not None, "a close that did not finish is not a success"
        assert model.peak == 1, "two closes ran on one transport at once"
        assert model.starts == 1, "a second close was started while the first ran"
        assert model.active == 1, "the resistant close is still running, as it must be"
        return model, owner

    async def run() -> None:
        model, owner = await main()
        # It is still owned, which is what makes it recoverable at shutdown.
        assert len(owner._closing) == 1
        assert not next(iter(owner._closing.values())).done()
        await _release(owner, model)

    asyncio.run(run())


def test_no_second_close_starts_while_the_first_is_still_running() -> None:
    """Retrying is allowed only once the previous close definitively ended."""

    async def run() -> None:
        owner = _interpreter()
        model = ResistantModel()

        await owner._close(_as_model(model), _Telemetry(correlation_id="one"))
        await owner._close(_as_model(model), _Telemetry(correlation_id="two"))
        await owner.resolve_cleanup()

        assert model.starts == 1, "every later attempt reused the running close"
        assert model.peak == 1
        await _release(owner, model)

    asyncio.run(run())


def test_a_close_that_finally_succeeds_resolves_the_cleanup() -> None:
    """Once the previous close really ends, the next attempt may proceed."""

    async def run() -> None:
        owner = _interpreter()
        model = ResistantModel()

        await owner._close(_as_model(model), _Telemetry(correlation_id="one"))
        # Read into locals: asserting the same property twice would let the
        # type checker carry the first narrowing into the second.
        held_while_running = owner.cleanup_unresolved
        assert held_while_running is True

        # The resistant close now completes on its own terms.
        model.release.set()
        await asyncio.sleep(0.05)

        assert await owner.resolve_cleanup() is True
        held_after = owner.cleanup_unresolved
        assert held_after is False
        assert model.client_open is False
        assert owner._closing == {}, "a finished close is no longer owned"

    asyncio.run(run())


def test_cancelling_the_waiter_does_not_lose_the_close_it_was_waiting_on() -> None:
    """Parent cancellation is the other way the handle used to be dropped.

    The waiter goes away; the close it started does not. It keeps running, stays
    owned, and no second close is created for that model afterwards.
    """

    async def run() -> None:
        owner = _interpreter()
        model = ResistantModel()
        started = asyncio.Event()

        async def waiter() -> None:
            started.set()
            await owner._close(_as_model(model), _Telemetry(correlation_id="parent"))

        task = asyncio.create_task(waiter())
        await started.wait()
        await asyncio.sleep(0)  # let the close operation actually begin
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert model.starts == 1
        assert model.active == 1, "the close survived its waiter"
        assert len(owner._closing) == 1, "and is still owned"

        # A later attempt joins that same close rather than starting another.
        await owner._close(_as_model(model), _Telemetry(correlation_id="later"))
        assert model.starts == 1
        assert model.peak == 1

        await _release(owner, model)

    asyncio.run(run())


def test_a_late_close_failure_is_observed_and_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A close that fails after the wait gave up must not vanish.

    Without retrieving it, the failure surfaces as an unretrieved task exception
    at interpreter exit instead of being reported where it happened.
    """
    caplog.set_level(logging.WARNING, logger="borrowed_steps.assistant")

    async def run() -> None:
        owner = _interpreter()
        model = ResistantModel(fail_with=OSError("closed badly, late"))

        await owner._close(_as_model(model), _Telemetry(correlation_id="late"))

        model.release.set()
        await asyncio.sleep(0.05)  # let the close finish on its own terms
        tasks = list(owner._closing.values())
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())

    assert "close finished late with OSError" in caplog.text


def test_the_ordinary_path_closes_once_and_owns_nothing_afterwards() -> None:
    """The fix must not leave bookkeeping behind on the normal route."""

    async def run() -> None:
        owner = _interpreter()
        model = PromptModel()

        assert await owner._close(_as_model(model), _Telemetry(correlation_id="ok")) is None
        assert model.starts == 1
        assert owner.cleanup_unresolved is False
        assert owner._closing == {}

    asyncio.run(run())


# --- the registry's own cleanup supervisor ------------------------------


class SlowCleanupOwner:
    """A cleanup owner whose resolve_cleanup resists cancellation."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.peak = 0

    @property
    def cleanup_unresolved(self) -> bool:
        return True

    async def resolve_cleanup(self) -> bool:
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            while not self.release.is_set():
                # Swallowing the cancellation is the point: this stands in
                # for work that cannot be interrupted where it is.
                with contextlib.suppress(asyncio.CancelledError):
                    await self.release.wait()
        finally:
            self.active -= 1
        return False


def test_the_registry_owns_its_cleanup_supervisor_across_drains() -> None:
    """A drain that stops waiting keeps its supervisor instead of dropping it.

    The second drain must join the first supervisor, not run another beside it.
    """

    async def run() -> None:
        cleanup = SlowCleanupOwner()
        registry = InferenceRegistry(cleanup)

        first = await registry.drain(0.01)
        assert first.cleanup_resolved is False
        assert first.clean is False, "an unresolved cleanup is not a clean shutdown"

        second = await registry.drain(0.01)
        assert second.cleanup_resolved is False

        assert cleanup.calls == 1, "the second drain started a second supervisor"
        assert cleanup.peak == 1
        assert registry._cleanup_task is not None, "the supervisor is still owned"

        cleanup.release.set()
        supervisor = registry._cleanup_task
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)

    asyncio.run(run())


def test_a_late_supervisor_failure_is_observed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="borrowed_steps.assistant")

    class Failing(SlowCleanupOwner):
        async def resolve_cleanup(self) -> bool:
            await super().resolve_cleanup()
            msg = "cleanup blew up late"
            raise RuntimeError(msg)

    async def run() -> None:
        cleanup = Failing()
        registry = InferenceRegistry(cleanup)

        report = await registry.drain(0.01)
        assert report.cleanup_resolved is False

        cleanup.release.set()
        supervisor = registry._cleanup_task
        assert supervisor is not None
        await asyncio.gather(supervisor, return_exceptions=True)

    asyncio.run(run())

    assert "cleanup supervisor finished late with RuntimeError" in caplog.text


def test_an_unresolved_shutdown_keeps_both_the_model_and_its_close_owned() -> None:
    """Shutdown reports honestly and hands nothing back that is still held."""

    async def run() -> None:
        owner = _interpreter()
        model = ResistantModel()
        await owner._close(_as_model(model), _Telemetry(correlation_id="held"))

        registry = InferenceRegistry(owner)
        report = await registry.drain(0.05)

        assert report.cleanup_resolved is False
        assert report.clean is False
        assert owner.cleanup_unresolved is True, "the model is still owned"
        assert len(owner._closing) == 1, "and so is its close operation"
        assert model.peak == 1, "shutdown did not start a competing close"

        supervisor = registry._cleanup_task
        if supervisor is not None:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        await _release(owner, model)

    asyncio.run(run())
