"""One active interpretation per service process, and who owns it at shutdown.

Held by the event loop only. ``try_acquire`` checks and sets without awaiting in
between, so on a single-threaded loop the pair is atomic and two callers can
never both win.

The slot is released when the owned inference actually finishes, not when a
caller stops waiting for it. A caller that times out therefore leaves the slot
held until its own inference really ends, which is what stops a slow request
from being replaced by a second concurrent one.

``InferenceRegistry`` adds the other half of that ownership: the application,
not the request handler, holds every in-flight interpretation, so shutdown can
stop accepting new work, signal the runs it knows about and wait a bounded time
for them — then say truthfully whether they ended.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from threading import Event
from typing import Any

from borrowed_steps.application.interpreter import CleanupOwner

__all__ = ["DrainReport", "InferenceRegistry", "InferenceSlot"]

_LOGGER = logging.getLogger("borrowed_steps.assistant")


class InferenceSlot:
    """A single non-queuing occupancy flag."""

    __slots__ = ("_busy",)

    def __init__(self) -> None:
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def try_acquire(self) -> bool:
        """Take the slot, or report that someone else holds it."""
        if self._busy:
            return False
        self._busy = True
        return True

    def release(self) -> None:
        self._busy = False


@dataclass(frozen=True, slots=True)
class DrainReport:
    """What a shutdown drain actually achieved. Counted, never assumed."""

    requested: int
    ended: int
    abandoned: int
    cleanup_resolved: bool | None = None
    """True/False when there was cleanup to finish, None when there was none."""

    @property
    def clean(self) -> bool:
        """True only when every run really ended and nothing is still held open."""
        return self.abandoned == 0 and self.cleanup_resolved is not False


class InferenceRegistry:
    """Application-level ownership of the interpretations currently running.

    A request handler registers its task and cooperative cancel signal here and
    forgets about them; the application owns them from that moment. Shutdown then
    has something concrete to act on instead of hoping in-flight work is over.

    Draining does two distinct things, and the difference matters. It sets each
    run's cooperative ``Event``, which a Python loop can notice at a boundary,
    and it *cancels* each task, which is what actually interrupts a provider
    call already awaiting a reply — the Event cannot reach inside one. Even
    together they are a local action: they end this process's work and its
    request, and they say nothing about whether the model on the other end
    stopped computing. This class never claims otherwise.

    The drain is bounded, so a task that will not end is reported as abandoned
    rather than waited on forever. If the interpreter still holds a client it
    could not close, that is reported too, and the drain is not called clean.
    """

    __slots__ = ("_active", "_cleanup", "_cleanup_task", "_closing")

    def __init__(self, cleanup: CleanupOwner | None = None) -> None:
        self._active: dict[asyncio.Task[Any], Event] = {}
        self._closing = False
        self._cleanup = cleanup
        # The one cleanup supervisor this registry has started, retained so a
        # drain that stopped waiting has not lost it.
        self._cleanup_task: asyncio.Task[bool] | None = None

    @property
    def closing(self) -> bool:
        """True once a drain has started. New interpretations must be refused."""
        return self._closing

    @property
    def active(self) -> int:
        return len(self._active)

    def register(self, task: asyncio.Task[Any], cancel: Event) -> None:
        """Take ownership of one running interpretation and its cancel signal.

        Never raises: a task created in the same event-loop step as the caller's
        ``closing`` check cannot see a different answer, but if a run is somehow
        registered after a drain began it is signalled at once rather than left
        unattended.
        """
        self._active[task] = cancel
        task.add_done_callback(self._forget)
        if self._closing:
            self.stop(task)

    @property
    def cleanup_unresolved(self) -> bool:
        """True while the interpreter still holds a client it could not close."""
        return self._cleanup is not None and self._cleanup.cleanup_unresolved

    def _forget(self, task: asyncio.Task[Any]) -> None:
        """Drop a finished run — unless it left a client open.

        A task that ended while its provider is still open has not finished
        being owned. Keeping it here means the drain still sees it and the
        report still says something is outstanding.
        """
        if self.cleanup_unresolved:
            _LOGGER.error(
                "An interpretation ended with its provider client still open; "
                "ownership is retained for shutdown."
            )
            return
        self._active.pop(task, None)

    def settle(self) -> bool:
        """Drop runs that are finished *and* no longer holding anything open.

        True when nothing is outstanding, which is the only condition under
        which the caller may hand the inference slot to someone else. A run that
        ended with its client still open is not finished in this sense, and
        neither ``_forget`` nor this method will let go of it.

        Called both when a run ends and when the next caller arrives, so a slot
        retained for an unresolved cleanup is released as soon as that cleanup
        is resolved — rather than leaving the service answering "busy" forever
        when nothing is running.
        """
        if self.cleanup_unresolved:
            return False
        for task in [task for task in self._active if task.done()]:
            self._active.pop(task, None)
        return not self._active

    def _cleanup_operation(self, cleanup: CleanupOwner) -> asyncio.Task[bool]:
        """The one cleanup supervisor task, started only if none is running.

        A drain that gave up waiting leaves its supervisor running rather than
        dropping it, so a later drain waits on that same task instead of
        starting a second one beside it. The result is retrieved whenever it
        finishes, late or not, so a failure is reported rather than surfacing as
        an unretrieved task exception.
        """
        running = self._cleanup_task
        if running is not None and not running.done():
            return running
        task = asyncio.create_task(cleanup.resolve_cleanup())
        self._cleanup_task = task
        task.add_done_callback(self._cleanup_settled)
        return task

    def _cleanup_settled(self, task: asyncio.Task[bool]) -> None:
        """Observe a supervisor that finished after the drain stopped waiting."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.warning(
                "Assistant cleanup supervisor finished late with %s", type(error).__name__
            )

    def stop(self, task: asyncio.Task[Any]) -> None:
        """Signal and cancel one run, without waiting for it.

        The Event is the cooperative half and the cancellation is the half that
        can interrupt a provider call already in flight. Neither says anything
        about the model at the other end.
        """
        cancel = self._active.get(task)
        if cancel is not None:
            cancel.set()
        task.cancel()

    async def drain(self, timeout: float) -> DrainReport:
        """Stop accepting work, stop every known run, and wait a bounded time."""
        self._closing = True
        tasks = list(self._active)
        for task in tasks:
            self.stop(task)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)

        ended = abandoned = 0
        if tasks:
            tasks_timeout = max(0.0, deadline - loop.time())
            done, pending = await asyncio.wait(tasks, timeout=tasks_timeout)
            ended, abandoned = len(done), len(pending)

        resolved: bool | None = None
        if self._cleanup is not None and self._cleanup.cleanup_unresolved:
            remaining = max(0.0, deadline - loop.time())
            if remaining <= 0.0:
                resolved = False
            else:
                cleanup_task = self._cleanup_operation(self._cleanup)
                done_cleanup, _ = await asyncio.wait({cleanup_task}, timeout=remaining)
                if cleanup_task in done_cleanup:
                    try:
                        resolved = bool(cleanup_task.result())
                    except Exception:
                        resolved = False
                else:
                    # Requested, not guaranteed. The task stays owned in
                    # _cleanup_task so a later drain observes it instead of
                    # starting a second one beside it.
                    cleanup_task.cancel()
                    resolved = False
            if resolved:
                # Only now is a run that ended with an open client really over.
                self.settle()

        return DrainReport(
            requested=len(tasks),
            ended=ended,
            abandoned=abandoned,
            cleanup_resolved=resolved,
        )
