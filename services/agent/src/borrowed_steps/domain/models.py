"""Domain entities and enumerations for the equipment loan workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = [
    "ACTIVE_LOAN_STATUSES",
    "BORROWER_LABEL_MAX_LENGTH",
    "INSPECTABLE_STATES",
    "INSPECTION_OUTCOMES",
    "OPEN_TASK_STATUSES",
    "PICKUP_LOCATION_MAX_LENGTH",
    "CoordinationTask",
    "EntityType",
    "Equipment",
    "EquipmentKind",
    "EquipmentState",
    "Event",
    "EventAction",
    "Loan",
    "LoanStatus",
    "Request",
    "RequestStatus",
    "TaskKind",
    "TaskStatus",
    "due_action",
    "inspection_action",
    "required_loan_status",
]


class EquipmentKind(StrEnum):
    """Kinds of mobility equipment the room lends."""

    WHEELCHAIR = "WHEELCHAIR"
    WALKER = "WALKER"
    CRUTCHES = "CRUTCHES"


class EquipmentState(StrEnum):
    """Readiness state of one item."""

    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    ON_LOAN = "ON_LOAN"
    AWAITING_INSPECTION = "AWAITING_INSPECTION"
    REPAIR = "REPAIR"
    QUARANTINED = "QUARANTINED"


class RequestStatus(StrEnum):
    """Lifecycle of a borrower request."""

    REQUESTED = "REQUESTED"
    RESERVED = "RESERVED"
    ON_LOAN = "ON_LOAN"
    RETURNED = "RETURNED"
    CLOSED = "CLOSED"


class LoanStatus(StrEnum):
    """Lifecycle of a loan."""

    RESERVED = "RESERVED"
    ON_LOAN = "ON_LOAN"
    RETURNED = "RETURNED"
    CLOSED = "CLOSED"


class TaskKind(StrEnum):
    """The two coordination notices this service keeps.

    Both are in-app records for the volunteer. Nothing here is sent anywhere:
    there is no email, SMS, calendar or notification provider in this service.
    """

    PICKUP_DUE = "PICKUP_DUE"
    RETURN_DUE = "RETURN_DUE"


class TaskStatus(StrEnum):
    """Lifecycle of one coordination task.

    ``PENDING`` means the runner has not yet written the notice; the underlying
    human action is already available. ``DUE`` means the notice was processed.
    ``RESOLVED`` means the human action it was tracking happened.
    """

    PENDING = "PENDING"
    DUE = "DUE"
    RESOLVED = "RESOLVED"


class EntityType(StrEnum):
    """Entity an event refers to."""

    EQUIPMENT = "EQUIPMENT"
    REQUEST = "REQUEST"
    LOAN = "LOAN"


class EventAction(StrEnum):
    """Durable history actions."""

    REQUEST_CREATED = "REQUEST_CREATED"
    RESERVED = "RESERVED"
    PICKED_UP = "PICKED_UP"
    RETURNED = "RETURNED"
    INSPECTED_AVAILABLE = "INSPECTED_AVAILABLE"
    INSPECTED_REPAIR = "INSPECTED_REPAIR"
    INSPECTED_QUARANTINED = "INSPECTED_QUARANTINED"
    PICKUP_DUE = "PICKUP_DUE"
    RETURN_DUE = "RETURN_DUE"


BORROWER_LABEL_MAX_LENGTH = 60
PICKUP_LOCATION_MAX_LENGTH = 120

ACTIVE_LOAN_STATUSES: tuple[LoanStatus, ...] = (LoanStatus.RESERVED, LoanStatus.ON_LOAN)

INSPECTABLE_STATES: tuple[EquipmentState, ...] = (
    EquipmentState.AWAITING_INSPECTION,
    EquipmentState.REPAIR,
    EquipmentState.QUARANTINED,
)

INSPECTION_OUTCOMES: tuple[EquipmentState, ...] = (
    EquipmentState.AVAILABLE,
    EquipmentState.REPAIR,
    EquipmentState.QUARANTINED,
)

_INSPECTION_ACTIONS: dict[EquipmentState, EventAction] = {
    EquipmentState.AVAILABLE: EventAction.INSPECTED_AVAILABLE,
    EquipmentState.REPAIR: EventAction.INSPECTED_REPAIR,
    EquipmentState.QUARANTINED: EventAction.INSPECTED_QUARANTINED,
}


OPEN_TASK_STATUSES: tuple[TaskStatus, ...] = (TaskStatus.PENDING, TaskStatus.DUE)

# The loan state each notice is about. A pickup notice only makes sense while
# the item is still waiting to be collected; a return notice only while it is
# out. Anything else means the human action already happened, and the notice is
# resolved without a due event rather than announced late.
_REQUIRED_LOAN_STATUS: dict[TaskKind, LoanStatus] = {
    TaskKind.PICKUP_DUE: LoanStatus.RESERVED,
    TaskKind.RETURN_DUE: LoanStatus.ON_LOAN,
}

_DUE_ACTIONS: dict[TaskKind, EventAction] = {
    TaskKind.PICKUP_DUE: EventAction.PICKUP_DUE,
    TaskKind.RETURN_DUE: EventAction.RETURN_DUE,
}


def inspection_action(outcome: EquipmentState) -> EventAction:
    """Event action recorded for an inspection ``outcome``."""
    return _INSPECTION_ACTIONS[outcome]


def required_loan_status(kind: TaskKind) -> LoanStatus:
    """The loan status that still makes ``kind`` applicable."""
    return _REQUIRED_LOAN_STATUS[kind]


def due_action(kind: TaskKind) -> EventAction:
    """Event action written when ``kind`` really becomes due."""
    return _DUE_ACTIONS[kind]


@dataclass(frozen=True, slots=True)
class Equipment:
    """One physical item in the equipment room."""

    id: str
    label: str
    kind: EquipmentKind
    state: EquipmentState
    version: int


@dataclass(frozen=True, slots=True)
class Request:
    """A borrower's structured request. Carries no medical or contact data."""

    id: str
    borrower_label: str
    equipment_kind: EquipmentKind
    pickup_location: str
    due_at: datetime
    status: RequestStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Loan:
    """The allocation of one item to one request."""

    id: str
    request_id: str
    equipment_id: str
    status: LoanStatus
    due_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CoordinationTask:
    """One persisted in-app notice about a loan.

    It records only what the volunteer needs to coordinate: which loan, which
    kind of notice, whether the notice has been processed, and when it applies.
    It carries no borrower detail, and it never changes equipment, request or
    loan state - a task is a record, not an actor.
    """

    id: str
    loan_id: str
    kind: TaskKind
    status: TaskStatus
    due_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Event:
    """An append-only history entry."""

    id: str
    entity_type: EntityType
    entity_id: str
    action: EventAction
    at: datetime
