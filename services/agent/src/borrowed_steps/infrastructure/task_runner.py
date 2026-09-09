"""The owned thread that runs coordination ticks.

One ``threading.Thread`` and one ``threading.Event``. No scheduler framework, no
queue, no executor, no network and no provider: this exists only to call
``process_due_tasks`` on a schedule, so persisted notices are processed whether
or not a browser is open.

Why a thread rather than an asyncio task: the store is synchronous ``sqlite3``,
so a tick inside the event loop would block it for the duration of every
transaction. The thread also opens its own connections, which is what SQLite
requires across threads.

Three things this deliberately does not do:

* It does not treat daemon status as cleanup. ``stop`` joins with a bounded
  timeout and reports whether the thread really ended.
* It does not disable itself after a bad tick. An unexpected error is logged
  and the next tick still runs, so one transient failure cannot silently end
  coordination for the life of the process.
* It does not log anything about a workspace, loan or borrower. Tick records
  are counts.
"""

from __future__ import annotations

import logging
import threading

from borrowed_steps.application.coordination import process_due_tasks
from borrowed_steps.application.ports import Clock, IdGenerator, Store

__all__ = ["TaskRunner"]

_LOGGER = logging.getLogger("borrowed_steps.tasks")


class TaskRunner:
    """Owns one thread that ticks immediately, then every ``interval`` seconds."""

    __slots__ = (
        "_clock",
        "_ids",
        "_interval",
        "_limit",
        "_started",
        "_stop",
        "_store",
        "_thread",
        "ticks",
    )

    def __init__(
        self,
        store: Store,
        clock: Clock,
        ids: IdGenerator,
        *,
        interval: float,
        limit: int,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._interval = interval
        self._limit = limit
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Set once the first tick has finished, so a caller (a test, a smoke
        # run) can wait for real work to have happened instead of sleeping and
        # hoping.
        self._started = threading.Event()
        self.ticks = 0

    @property
    def running(self) -> bool:
        """True while the owned thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def first_tick_done(self) -> threading.Event:
        """Set after the startup tick completes."""
        return self._started

    def start(self) -> None:
        """Start the thread. Starting twice is refused rather than ignored."""
        if self._thread is not None:
            msg = "this task runner has already been started"
            raise RuntimeError(msg)
        self._stop.clear()
        thread = threading.Thread(target=self._loop, name="borrowed-steps-tasks", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self, timeout: float) -> bool:
        """Ask the thread to stop and wait up to ``timeout`` seconds.

        Returns True only when the thread really ended. A False here is a real
        shutdown problem and the caller is expected to report it; it is never
        rewritten into success, and the daemon flag is not offered as a
        substitute for the thread actually finishing.
        """
        thread = self._thread
        if thread is None:
            return True
        self._stop.set()
        thread.join(timeout)
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def _loop(self) -> None:
        """Tick once immediately, then wait interruptibly between ticks."""
        while True:
            self._tick_once()
            self._started.set()
            if self._stop.wait(self._interval):
                return

    def _tick_once(self) -> None:
        if self._stop.is_set():
            return
        try:
            report = process_due_tasks(
                self._store,
                self._clock,
                self._ids,
                limit=self._limit,
                stop=self._stop,
            )
        except Exception:
            # Observable, and not fatal to the schedule. Swallowing this would
            # end coordination for the process; letting it out would kill the
            # thread and leave a service that looks healthy but processes
            # nothing. The exception text carries no workspace data.
            _LOGGER.exception("Coordination tick failed; the next tick will still run.")
            return
        self.ticks += 1
        if report.considered or report.contended:
            _LOGGER.info("Coordination tick: %s", report)
