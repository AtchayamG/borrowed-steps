"""Deterministic equipment-loan rules.

Every transition is a pure function over domain entities. Nothing here reads a
clock, a database or a request: callers pass the values in. A human decides
allocation and inspection outcomes; these rules only decide whether the decision
the human made is legal for the current state.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from borrowed_steps.domain.errors import (
    ApprovalRequiredError,
    StateConflictError,
    ValidationFailedError,
)
from borrowed_steps.domain.models import (
    INSPECTABLE_STATES,
    INSPECTION_OUTCOMES,
    Equipment,
    EquipmentState,
    Loan,
    LoanStatus,
    Request,
    RequestStatus,
)

__all__ = [
    "MAX_DUE_AT_HORIZON",
    "close_after_inspection",
    "inspect_equipment",
    "pick_up",
    "require_human_approval",
    "reserve",
    "return_item",
    "validate_due_at",
]

MAX_DUE_AT_HORIZON = timedelta(days=30)


def require_human_approval(human_approved: bool | None) -> None:
    """A human must affirm every allocation and inspection outcome."""
    if human_approved is not True:
        msg = "A human must approve this action."
        raise ApprovalRequiredError(msg)


def validate_due_at(due_at: datetime, now: datetime) -> None:
    """``due_at`` must be in the future and no more than 30 days ahead."""
    if due_at <= now:
        msg = "due_at must be in the future."
        raise ValidationFailedError(msg)
    if due_at - now > MAX_DUE_AT_HORIZON:
        msg = "due_at must be within 30 days."
        raise ValidationFailedError(msg)


def _check_version(equipment: Equipment, expected_version: int) -> None:
    if equipment.version != expected_version:
        msg = "The equipment changed since it was read. Refresh and try again."
        raise StateConflictError(msg)


def reserve(
    request: Request,
    equipment: Equipment,
    expected_version: int,
) -> tuple[Request, Equipment]:
    """Allocate an AVAILABLE item of the requested kind to a REQUESTED request."""
    if request.status is not RequestStatus.REQUESTED:
        msg = "Only an open request can be allocated."
        raise StateConflictError(msg)
    if equipment.state is not EquipmentState.AVAILABLE:
        msg = "Only an available item can be allocated."
        raise StateConflictError(msg)
    if equipment.kind is not request.equipment_kind:
        msg = "The item does not match the requested equipment kind."
        raise StateConflictError(msg)
    _check_version(equipment, expected_version)
    return (
        replace(request, status=RequestStatus.RESERVED),
        replace(equipment, state=EquipmentState.RESERVED, version=equipment.version + 1),
    )


def pick_up(
    request: Request,
    equipment: Equipment,
    loan: Loan,
    expected_version: int,
) -> tuple[Request, Equipment, Loan]:
    """Confirm collection of a reserved item."""
    if loan.status is not LoanStatus.RESERVED:
        msg = "Only a reserved loan can be picked up."
        raise StateConflictError(msg)
    if request.status is not RequestStatus.RESERVED:
        msg = "Only a reserved request can be picked up."
        raise StateConflictError(msg)
    if equipment.state is not EquipmentState.RESERVED:
        msg = "Only a reserved item can be picked up."
        raise StateConflictError(msg)
    _check_version(equipment, expected_version)
    return (
        replace(request, status=RequestStatus.ON_LOAN),
        replace(equipment, state=EquipmentState.ON_LOAN, version=equipment.version + 1),
        replace(loan, status=LoanStatus.ON_LOAN),
    )


def return_item(
    request: Request,
    equipment: Equipment,
    loan: Loan,
    expected_version: int,
) -> tuple[Request, Equipment, Loan]:
    """Take an item back. It stays unavailable until a human inspects it."""
    if loan.status is not LoanStatus.ON_LOAN:
        msg = "Only a loan that is out can be returned."
        raise StateConflictError(msg)
    if request.status is not RequestStatus.ON_LOAN:
        msg = "Only a request that is out can be returned."
        raise StateConflictError(msg)
    if equipment.state is not EquipmentState.ON_LOAN:
        msg = "Only an item that is out can be returned."
        raise StateConflictError(msg)
    _check_version(equipment, expected_version)
    return (
        replace(request, status=RequestStatus.RETURNED),
        replace(
            equipment,
            state=EquipmentState.AWAITING_INSPECTION,
            version=equipment.version + 1,
        ),
        replace(loan, status=LoanStatus.RETURNED),
    )


def inspect_equipment(
    equipment: Equipment,
    outcome: EquipmentState,
    expected_version: int,
) -> Equipment:
    """Record a human inspection outcome, including release from quarantine."""
    if outcome not in INSPECTION_OUTCOMES:
        msg = "Unsupported inspection outcome."
        raise ValidationFailedError(msg)
    if equipment.state not in INSPECTABLE_STATES:
        msg = "Only an item awaiting inspection, in repair or quarantined can be inspected."
        raise StateConflictError(msg)
    _check_version(equipment, expected_version)
    return replace(equipment, state=outcome, version=equipment.version + 1)


def close_after_inspection(request: Request, loan: Loan) -> tuple[Request, Loan]:
    """Close the returned loan and its request once the item has been inspected."""
    if loan.status is not LoanStatus.RETURNED:
        msg = "Only a returned loan can be closed."
        raise StateConflictError(msg)
    return (
        replace(request, status=RequestStatus.CLOSED),
        replace(loan, status=LoanStatus.CLOSED),
    )
