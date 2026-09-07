"""Domain entities and enumerations for the equipment loan workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = [
    "ACTIVE_LOAN_STATUSES",
    "INSPECTABLE_STATES",
    "INSPECTION_OUTCOMES",
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
    "inspection_action",
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


def inspection_action(outcome: EquipmentState) -> EventAction:
    """Event action recorded for an inspection ``outcome``."""
    return _INSPECTION_ACTIONS[outcome]


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
class Event:
    """An append-only history entry."""

    id: str
    entity_type: EntityType
    entity_id: str
    action: EventAction
    at: datetime
