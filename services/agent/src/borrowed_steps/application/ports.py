"""Ports the application depends on. Implementations live in outer adapters."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from borrowed_steps.domain.models import Equipment, Event, Loan, Request

__all__ = [
    "Clock",
    "IdGenerator",
    "IdempotencyRecord",
    "Session",
    "Snapshot",
    "Store",
    "WorkspaceUnitOfWork",
]


@dataclass(frozen=True, slots=True)
class Session:
    """A server-issued handle binding one browser to one synthetic workspace."""

    id: str
    workspace_id: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """The stored outcome of a completed mutation, replayed on identical retry."""

    key: str
    route: str
    request_hash: str
    status_code: int
    response_body: str


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything one workspace can see. Events are newest first."""

    equipment: list[Equipment]
    requests: list[Request]
    loans: list[Loan]
    events: list[Event]


class Clock(Protocol):
    """Time seam, so expiry and due-date rules are testable."""

    def now(self) -> datetime:
        """Current whole-second UTC time."""
        ...


class IdGenerator(Protocol):
    """Identifier seam, so tests can make identifiers deterministic."""

    def new_id(self) -> str:
        """An opaque entity identifier."""
        ...

    def new_session_id(self) -> str:
        """An opaque, unpredictable session identifier."""
        ...


class WorkspaceUnitOfWork(Protocol):
    """Reads and writes inside one open transaction, scoped to one workspace.

    Every method is already bound to the caller's workspace: an identifier from
    another workspace simply does not resolve.
    """

    def list_equipment(self) -> list[Equipment]: ...

    def list_requests(self) -> list[Request]: ...

    def list_loans(self) -> list[Loan]: ...

    def list_events(self) -> list[Event]: ...

    def get_equipment(self, equipment_id: str) -> Equipment | None: ...

    def get_request(self, request_id: str) -> Request | None: ...

    def get_loan(self, loan_id: str) -> Loan | None: ...

    def find_returned_loan(self, equipment_id: str) -> Loan | None: ...

    def add_request(self, request: Request) -> None: ...

    def add_loan(self, loan: Loan) -> None: ...

    def add_event(self, event: Event) -> None: ...

    def save_request(self, request: Request) -> None: ...

    def save_loan(self, loan: Loan) -> None: ...

    def save_equipment(self, equipment: Equipment) -> None: ...

    def get_idempotency(self, key: str) -> IdempotencyRecord | None: ...

    def save_idempotency(self, record: IdempotencyRecord) -> None: ...


class Store(Protocol):
    """Durable storage for every workspace."""

    def transaction(
        self, workspace_id: str, *, write: bool = True
    ) -> AbstractContextManager[WorkspaceUnitOfWork]:
        """Open a transaction scoped to ``workspace_id``.

        The transaction commits on clean exit and rolls back on any exception,
        so a failed mutation leaves no partial state.
        """
        ...

    def create_workspace(self, workspace_id: str, equipment: Sequence[Equipment]) -> None:
        """Create a workspace and its seeded equipment in one transaction."""
        ...

    def create_session(self, session: Session) -> None:
        """Persist a session handle."""
        ...

    def get_session(self, session_id: str) -> Session | None:
        """Load a session handle, or ``None`` when it is unknown."""
        ...
