"""RequestInterpreter port and its plain data types.

Standard library only. Strands, Pydantic and Ollama live in outer adapters; this
module exists so the application can ask for an interpretation without knowing
that a model is involved at all.

Interpretation is read-only. Nothing here creates a request, loan, event or any
other business record: the result is an ephemeral suggestion that a human edits
and then submits through the existing structured route.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Event
from typing import Any, Protocol, runtime_checkable

from borrowed_steps.domain.models import EquipmentKind

__all__ = [
    "CleanupOwner",
    "DraftRequest",
    "Interpretation",
    "InventoryCount",
    "InventoryReader",
    "RequestInterpreter",
]


@dataclass(frozen=True, slots=True)
class InventoryCount:
    """How many items of one kind are in one readiness state.

    Deliberately carries no identity: no equipment id, borrower, request or loan.
    """

    kind: str
    state: str
    count: int


@dataclass(frozen=True, slots=True)
class DraftRequest:
    """A suggested, unsaved request. Any field the text did not clearly supply is None."""

    borrower_label: str | None
    equipment_kind: EquipmentKind | None
    pickup_location: str | None
    due_at: datetime | None


@dataclass(frozen=True, slots=True)
class Interpretation:
    """One completed interpretation and the provenance of its real execution."""

    draft: DraftRequest
    missing_fields: tuple[str, ...]
    framework: str
    provider: str
    model: str
    inventory_tool_calls: int
    completed_at: datetime
    diagnostic: dict[str, Any] | None = None


class InventoryReader(Protocol):
    """Read-only, server-bound view of one workspace's equipment room.

    The caller binds the workspace; there is no workspace argument, so a model
    cannot ask about anyone else's data.
    """

    def kind_state_counts(self) -> list[InventoryCount]:
        """Point-in-time counts by kind and readiness state."""
        ...


class RequestInterpreter(Protocol):
    """Turns free text into a suggested draft. Never writes anything."""

    async def interpret(
        self, text: str, inventory: InventoryReader, cancel: Event
    ) -> Interpretation:
        """Interpret ``text``.

        ``cancel`` is set by the caller when it has stopped waiting; an
        implementation must stop at its next safe boundary.

        The caller may also cancel the surrounding task. That is the stronger
        signal and the one that interrupts a provider call already in flight;
        ``cancel`` alone cannot reach inside a native request.
        """
        ...


@runtime_checkable
class CleanupOwner(Protocol):
    """An interpreter that may still own a resource it failed to release.

    Releasing a provider client can fail, and it can be interrupted by a second
    cancellation arriving during the release itself. When that happens the
    interpreter keeps the handle rather than dropping it, and says so here, so
    the application can refuse further inference and try again at shutdown
    instead of assuming a clean exit.

    Optional: implementations without a resource to own simply do not provide
    this, and the application treats them as always resolved.
    """

    @property
    def cleanup_unresolved(self) -> bool:
        """True while a provider client this interpreter opened is still open."""
        ...

    async def resolve_cleanup(self) -> bool:
        """Try again to release what is still held. True when nothing remains."""
        ...
