"""Due processing for persisted coordination tasks.

One application operation, driven by an injected clock and called by whatever
owns a schedule - the runner in production, a test directly. It reads and writes
only through the storage ports, makes no model, network or provider call, and
never changes equipment, request or loan state. A notice becoming due is a
record that a deadline passed; it is not an action taken on anyone's behalf.

Two properties do the real work here:

* **Discovery is a hint, never a decision.** ``due_task_candidates`` crosses
  workspaces and returns identifiers only. Everything that decides anything is
  re-read inside the candidate's own workspace transaction, so a row that
  changed between discovery and the write is handled as it is now, not as it
  was.
* **The claim is conditional.** ``mark_task_due`` carries its own PENDING
  condition, so two runners cannot both believe they won and a later tick over
  an already-DUE task writes no second event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from borrowed_steps.application.errors import StorageBusyError
from borrowed_steps.application.ports import Clock, IdGenerator, Store
from borrowed_steps.domain.models import (
    EntityType,
    Event,
    TaskStatus,
    due_action,
    required_loan_status,
)

__all__ = ["TickReport", "process_due_tasks"]

_LOGGER = logging.getLogger("borrowed_steps.tasks")


class StopSignal(Protocol):
    """Anything that can say "stop between candidates"."""

    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class TickReport:
    """What one tick actually did. Counts only - no workspace or loan data.

    ``contended`` is reported rather than hidden: a busy database is a normal
    outcome under a second writer, and a tick that quietly swallowed it would
    look identical to one where nothing was due.
    """

    considered: int = 0
    marked_due: int = 0
    resolved_stale: int = 0
    unchanged: int = 0
    contended: int = 0
    stopped_early: bool = False

    def __str__(self) -> str:
        return (
            f"considered={self.considered} due={self.marked_due} "
            f"stale_resolved={self.resolved_stale} unchanged={self.unchanged} "
            f"contended={self.contended} stopped_early={self.stopped_early}"
        )


def process_due_tasks(
    store: Store,
    clock: Clock,
    ids: IdGenerator,
    *,
    limit: int,
    stop: StopSignal | None = None,
) -> TickReport:
    """Process at most ``limit`` due tasks, one workspace transaction each.

    Returns what happened. Raises nothing for an ordinary contended row: that
    task keeps its PENDING status and is simply picked up by a later tick.
    """
    now = clock.now()
    candidates = store.due_task_candidates(now, limit)

    considered = marked = stale = unchanged = contended = 0
    stopped_early = False

    for candidate in candidates:
        # Checked between transactions, never inside one, so a stop request can
        # never abandon half-written work.
        if stop is not None and stop.is_set():
            stopped_early = True
            break
        considered += 1
        try:
            outcome = _process_one(store, ids, candidate.workspace_id, candidate.task_id, now)
        except StorageBusyError:
            # The write transaction could not be taken within the busy timeout,
            # so nothing began: neither the task nor an event changed. Recorded
            # in bounded form - counts only, no workspace, loan or borrower
            # data - and left for a later tick.
            contended += 1
            _LOGGER.warning(
                "Coordination tick could not take the write lock for one task; "
                "it stays pending and will be retried."
            )
            continue
        if outcome == "due":
            marked += 1
        elif outcome == "stale":
            stale += 1
        else:
            unchanged += 1

    return TickReport(
        considered=considered,
        marked_due=marked,
        resolved_stale=stale,
        unchanged=unchanged,
        contended=contended,
        stopped_early=stopped_early,
    )


def _process_one(
    store: Store,
    ids: IdGenerator,
    workspace_id: str,
    task_id: str,
    now: datetime,
) -> str:
    """Decide and write one task, inside its own workspace transaction.

    Every fact is re-read here. The discovery result is treated as nothing more
    than "this identifier was worth a second look".
    """
    with store.transaction(workspace_id) as uow:
        task = uow.get_task(task_id)
        if task is None or task.status is not TaskStatus.PENDING or task.due_at > now:
            # Already handled, resolved by a human action, or no longer due
            # because the clock this tick uses is the one that discovered it.
            return "unchanged"

        loan = uow.get_loan(task.loan_id)
        if loan is None or loan.status is not required_loan_status(task.kind):
            # The human action this notice was about already happened, or the
            # loan is gone. Close the notice quietly: announcing a pickup that
            # has already been collected would be telling the volunteer
            # something untrue.
            uow.resolve_task(task.id)
            return "stale"

        if not uow.mark_task_due(task.id):
            # Another writer claimed it between the read and this statement.
            # Theirs is the one true transition; this one writes no event.
            return "unchanged"

        # Same transaction as the claim, so a rollback loses both or neither.
        uow.add_event(
            Event(
                id=ids.new_id(),
                entity_type=EntityType.LOAN,
                entity_id=loan.id,
                action=due_action(task.kind),
                at=now,
            )
        )
        return "due"
