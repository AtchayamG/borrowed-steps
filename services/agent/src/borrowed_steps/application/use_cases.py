"""Use cases: the only place workflow decisions are made.

Every mutation runs inside a transaction opened by the caller and is scoped to
one workspace. Human approval is enforced here, on the server, for allocation,
pickup, return and every inspection outcome including release from quarantine.
No model, provider or network call takes part in any of this.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

from borrowed_steps.application.errors import NotFoundError, SessionRequiredError
from borrowed_steps.application.ports import (
    Clock,
    IdGenerator,
    Session,
    Snapshot,
    Store,
    WorkspaceUnitOfWork,
)
from borrowed_steps.domain import rules
from borrowed_steps.domain.models import (
    CoordinationTask,
    EntityType,
    Equipment,
    EquipmentKind,
    EquipmentState,
    Event,
    EventAction,
    Loan,
    LoanStatus,
    Request,
    RequestStatus,
    TaskKind,
    TaskStatus,
    inspection_action,
)

__all__ = [
    "SEED_EQUIPMENT",
    "SESSION_TTL",
    "Services",
    "create_request",
    "inspect_item",
    "pick_up_loan",
    "read_snapshot",
    "reserve_equipment",
    "resolve_session",
    "return_loan",
    "start_workspace",
]

SESSION_TTL = timedelta(hours=24)

SEED_EQUIPMENT: tuple[tuple[str, EquipmentKind, EquipmentState], ...] = (
    ("Wheelchair W-01", EquipmentKind.WHEELCHAIR, EquipmentState.AVAILABLE),
    ("Walker K-04", EquipmentKind.WALKER, EquipmentState.AVAILABLE),
    ("Crutches C-07", EquipmentKind.CRUTCHES, EquipmentState.QUARANTINED),
)


@dataclass(frozen=True, slots=True)
class Services:
    """The seams a use case needs: storage, time and identifiers."""

    store: Store
    clock: Clock
    ids: IdGenerator


_T = TypeVar("_T")


def _found(value: _T | None, message: str) -> _T:
    if value is None:
        raise NotFoundError(message)
    return value


def start_workspace(svc: Services) -> Session:
    """Create an isolated synthetic workspace and the session that owns it."""
    workspace_id = svc.ids.new_id()
    equipment = [
        Equipment(id=svc.ids.new_id(), label=label, kind=kind, state=state, version=1)
        for label, kind, state in SEED_EQUIPMENT
    ]
    svc.store.create_workspace(workspace_id, equipment)
    now = svc.clock.now()
    session = Session(
        id=svc.ids.new_session_id(),
        workspace_id=workspace_id,
        created_at=now,
        expires_at=now + SESSION_TTL,
    )
    svc.store.create_session(session)
    return session


def resolve_session(svc: Services, session_id: str | None) -> Session:
    """Resolve the cookie to a live session, or refuse the request."""
    message = "A synthetic workspace session is required."
    if not session_id:
        raise SessionRequiredError(message)
    session = svc.store.get_session(session_id)
    if session is None or session.expires_at <= svc.clock.now():
        raise SessionRequiredError(message)
    return session


def read_snapshot(uow: WorkspaceUnitOfWork) -> Snapshot:
    """Everything the caller's workspace can see."""
    return Snapshot(
        equipment=uow.list_equipment(),
        requests=uow.list_requests(),
        loans=uow.list_loans(),
        events=uow.list_events(),
        tasks=uow.list_tasks(),
    )


def _open_task(
    svc: Services,
    uow: WorkspaceUnitOfWork,
    *,
    loan_id: str,
    kind: TaskKind,
    due_at: datetime,
    now: datetime,
) -> CoordinationTask:
    """Record one PENDING coordination notice.

    Called from inside the caller's lifecycle transaction, so the task and the
    state change it belongs to commit or roll back together. There is no
    separate write and therefore no window in which a loan exists without its
    notice.
    """
    task = CoordinationTask(
        id=svc.ids.new_id(),
        loan_id=loan_id,
        kind=kind,
        status=TaskStatus.PENDING,
        due_at=due_at,
        created_at=now,
    )
    uow.add_task(task)
    return task


def _resolve_task(uow: WorkspaceUnitOfWork, loan_id: str, kind: TaskKind) -> None:
    """Close the notice a human action has just answered.

    Silent when there is nothing to close. A task may already be RESOLVED
    because the action raced the processor, or absent because the loan predates
    this schema - neither is a failure of the action, and neither should stop a
    human from recording what really happened.
    """
    task = uow.find_task(loan_id, kind)
    if task is not None:
        uow.resolve_task(task.id)


def create_request(
    svc: Services,
    uow: WorkspaceUnitOfWork,
    *,
    borrower_label: str,
    equipment_kind: EquipmentKind,
    pickup_location: str,
    due_at: datetime,
) -> Request:
    """Record a structured borrower request. No allocation happens here."""
    now = svc.clock.now()
    rules.validate_due_at(due_at, now)
    request = Request(
        id=svc.ids.new_id(),
        borrower_label=borrower_label,
        equipment_kind=equipment_kind,
        pickup_location=pickup_location,
        due_at=due_at,
        status=RequestStatus.REQUESTED,
        created_at=now,
    )
    uow.add_request(request)
    uow.add_event(
        Event(
            id=svc.ids.new_id(),
            entity_type=EntityType.REQUEST,
            entity_id=request.id,
            action=EventAction.REQUEST_CREATED,
            at=now,
        )
    )
    return request


def reserve_equipment(
    svc: Services,
    uow: WorkspaceUnitOfWork,
    *,
    request_id: str,
    equipment_id: str,
    expected_equipment_version: int,
    human_approved: bool | None,
) -> tuple[Request, Equipment, Loan]:
    """A human allocates one available item to one open request."""
    rules.require_human_approval(human_approved)
    request = _found(uow.get_request(request_id), "Request not found.")
    equipment = _found(uow.get_equipment(equipment_id), "Equipment not found.")
    updated_request, updated_equipment = rules.reserve(
        request, equipment, expected_equipment_version
    )
    now = svc.clock.now()
    loan = Loan(
        id=svc.ids.new_id(),
        request_id=request.id,
        equipment_id=equipment.id,
        status=LoanStatus.RESERVED,
        due_at=request.due_at,
        created_at=now,
    )
    uow.save_request(updated_request)
    uow.save_equipment(updated_equipment)
    uow.add_loan(loan)
    uow.add_event(
        Event(
            id=svc.ids.new_id(),
            entity_type=EntityType.LOAN,
            entity_id=loan.id,
            action=EventAction.RESERVED,
            at=now,
        )
    )
    # Due immediately: arranging the pickup is what happens next, so the notice
    # applies from this instant. It is not a promised pickup time and it is not
    # a deadline the borrower can miss.
    _open_task(svc, uow, loan_id=loan.id, kind=TaskKind.PICKUP_DUE, due_at=now, now=now)
    return updated_request, updated_equipment, loan


def _loan_context(uow: WorkspaceUnitOfWork, loan_id: str) -> tuple[Request, Equipment, Loan]:
    loan = _found(uow.get_loan(loan_id), "Loan not found.")
    request = _found(uow.get_request(loan.request_id), "Request not found.")
    equipment = _found(uow.get_equipment(loan.equipment_id), "Equipment not found.")
    return request, equipment, loan


def pick_up_loan(
    svc: Services,
    uow: WorkspaceUnitOfWork,
    *,
    loan_id: str,
    expected_equipment_version: int,
    human_approved: bool | None,
) -> tuple[Request, Equipment, Loan]:
    """A human confirms the borrower collected the item."""
    rules.require_human_approval(human_approved)
    request, equipment, loan = _loan_context(uow, loan_id)
    updated = rules.pick_up(request, equipment, loan, expected_equipment_version)
    committed = _commit_transition(svc, uow, updated, EventAction.PICKED_UP)
    now = svc.clock.now()
    _resolve_task(uow, loan.id, TaskKind.PICKUP_DUE)
    # The return notice carries the loan's real due instant, the one the human
    # entered on the request. Nothing here invents or shifts a deadline.
    _open_task(
        svc,
        uow,
        loan_id=loan.id,
        kind=TaskKind.RETURN_DUE,
        due_at=loan.due_at,
        now=now,
    )
    return committed


def return_loan(
    svc: Services,
    uow: WorkspaceUnitOfWork,
    *,
    loan_id: str,
    expected_equipment_version: int,
    human_approved: bool | None,
) -> tuple[Request, Equipment, Loan]:
    """A human takes the item back. It stays unavailable until inspected."""
    rules.require_human_approval(human_approved)
    request, equipment, loan = _loan_context(uow, loan_id)
    updated = rules.return_item(request, equipment, loan, expected_equipment_version)
    committed = _commit_transition(svc, uow, updated, EventAction.RETURNED)
    _resolve_task(uow, loan.id, TaskKind.RETURN_DUE)
    return committed


def _commit_transition(
    svc: Services,
    uow: WorkspaceUnitOfWork,
    updated: tuple[Request, Equipment, Loan],
    action: EventAction,
) -> tuple[Request, Equipment, Loan]:
    request, equipment, loan = updated
    now = svc.clock.now()
    uow.save_request(request)
    uow.save_equipment(equipment)
    uow.save_loan(loan)
    uow.add_event(
        Event(
            id=svc.ids.new_id(),
            entity_type=EntityType.LOAN,
            entity_id=loan.id,
            action=action,
            at=now,
        )
    )
    return request, equipment, loan


def inspect_item(
    svc: Services,
    uow: WorkspaceUnitOfWork,
    *,
    equipment_id: str,
    outcome: EquipmentState,
    expected_equipment_version: int,
    human_approved: bool | None,
) -> tuple[Equipment, Request | None, Loan | None]:
    """A human records an inspection outcome, including release from quarantine."""
    rules.require_human_approval(human_approved)
    equipment = _found(uow.get_equipment(equipment_id), "Equipment not found.")
    updated_equipment = rules.inspect_equipment(equipment, outcome, expected_equipment_version)

    closed_request: Request | None = None
    closed_loan: Loan | None = None
    returned_loan = uow.find_returned_loan(equipment.id)
    if returned_loan is not None:
        request = _found(uow.get_request(returned_loan.request_id), "Request not found.")
        closed_request, closed_loan = rules.close_after_inspection(request, returned_loan)
        uow.save_request(closed_request)
        uow.save_loan(closed_loan)
        # Defensive: a closed loan has nothing left to coordinate. In the normal
        # path pickup and return already resolved both notices, so this closes
        # nothing; it exists so a loan closed by any route cannot leave a notice
        # behind that a later tick would announce.
        for open_task in uow.open_tasks_for_loan(closed_loan.id):
            uow.resolve_task(open_task.id)

    uow.save_equipment(updated_equipment)
    uow.add_event(
        Event(
            id=svc.ids.new_id(),
            entity_type=EntityType.EQUIPMENT,
            entity_id=updated_equipment.id,
            action=inspection_action(outcome),
            at=svc.clock.now(),
        )
    )
    return updated_equipment, closed_request, closed_loan
