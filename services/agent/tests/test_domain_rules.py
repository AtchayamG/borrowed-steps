"""Unit tests for the deterministic loan rules. No database, no framework."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from borrowed_steps.domain.errors import (
    ApprovalRequiredError,
    StateConflictError,
    ValidationFailedError,
)
from borrowed_steps.domain.models import (
    Equipment,
    EquipmentKind,
    EquipmentState,
    EventAction,
    Loan,
    LoanStatus,
    Request,
    RequestStatus,
    inspection_action,
)
from borrowed_steps.domain.rules import (
    close_after_inspection,
    inspect_equipment,
    pick_up,
    require_human_approval,
    reserve,
    return_item,
    validate_due_at,
)

NOW = datetime(2026, 9, 7, 9, 0, 0, tzinfo=UTC)


def _equipment(
    state: EquipmentState = EquipmentState.AVAILABLE,
    kind: EquipmentKind = EquipmentKind.WHEELCHAIR,
    version: int = 1,
) -> Equipment:
    return Equipment(id="e1", label="Wheelchair W-01", kind=kind, state=state, version=version)


def _request(status: RequestStatus = RequestStatus.REQUESTED) -> Request:
    return Request(
        id="r1",
        borrower_label="Borrower A",
        equipment_kind=EquipmentKind.WHEELCHAIR,
        pickup_location="Velachery",
        due_at=NOW + timedelta(days=7),
        status=status,
        created_at=NOW,
    )


def _loan(status: LoanStatus = LoanStatus.RESERVED) -> Loan:
    return Loan(
        id="l1",
        request_id="r1",
        equipment_id="e1",
        status=status,
        due_at=NOW + timedelta(days=7),
        created_at=NOW,
    )


def test_require_human_approval_accepts_only_true() -> None:
    require_human_approval(True)
    for value in (None, False):
        with pytest.raises(ApprovalRequiredError):
            require_human_approval(value)


def test_validate_due_at_window() -> None:
    validate_due_at(NOW + timedelta(days=7), NOW)
    validate_due_at(NOW + timedelta(days=30), NOW)
    with pytest.raises(ValidationFailedError):
        validate_due_at(NOW, NOW)
    with pytest.raises(ValidationFailedError):
        validate_due_at(NOW - timedelta(seconds=1), NOW)
    with pytest.raises(ValidationFailedError):
        validate_due_at(NOW + timedelta(days=30, seconds=1), NOW)


def test_reserve_moves_request_and_equipment() -> None:
    request, equipment = reserve(_request(), _equipment(), 1)
    assert request.status is RequestStatus.RESERVED
    assert equipment.state is EquipmentState.RESERVED
    assert equipment.version == 2


@pytest.mark.parametrize(
    ("request_status", "state", "kind", "expected_version"),
    [
        (RequestStatus.RESERVED, EquipmentState.AVAILABLE, EquipmentKind.WHEELCHAIR, 1),
        (RequestStatus.REQUESTED, EquipmentState.QUARANTINED, EquipmentKind.WHEELCHAIR, 1),
        (RequestStatus.REQUESTED, EquipmentState.AWAITING_INSPECTION, EquipmentKind.WHEELCHAIR, 1),
        (RequestStatus.REQUESTED, EquipmentState.ON_LOAN, EquipmentKind.WHEELCHAIR, 1),
        (RequestStatus.REQUESTED, EquipmentState.AVAILABLE, EquipmentKind.WALKER, 1),
        (RequestStatus.REQUESTED, EquipmentState.AVAILABLE, EquipmentKind.WHEELCHAIR, 2),
    ],
)
def test_reserve_rejects_illegal_input(
    request_status: RequestStatus,
    state: EquipmentState,
    kind: EquipmentKind,
    expected_version: int,
) -> None:
    with pytest.raises(StateConflictError):
        reserve(_request(request_status), _equipment(state, kind), expected_version)


def test_pick_up_and_return_happy_path() -> None:
    request, equipment, loan = pick_up(
        _request(RequestStatus.RESERVED), _equipment(EquipmentState.RESERVED), _loan(), 1
    )
    assert (request.status, equipment.state, loan.status) == (
        RequestStatus.ON_LOAN,
        EquipmentState.ON_LOAN,
        LoanStatus.ON_LOAN,
    )
    assert equipment.version == 2

    request, equipment, loan = return_item(request, equipment, loan, 2)
    assert (request.status, equipment.state, loan.status) == (
        RequestStatus.RETURNED,
        EquipmentState.AWAITING_INSPECTION,
        LoanStatus.RETURNED,
    )
    assert equipment.version == 3


def test_pick_up_rejects_wrong_states() -> None:
    with pytest.raises(StateConflictError):
        pick_up(
            _request(RequestStatus.RESERVED),
            _equipment(EquipmentState.RESERVED),
            _loan(LoanStatus.ON_LOAN),
            1,
        )
    with pytest.raises(StateConflictError):
        pick_up(_request(), _equipment(EquipmentState.RESERVED), _loan(), 1)
    with pytest.raises(StateConflictError):
        pick_up(_request(RequestStatus.RESERVED), _equipment(), _loan(), 1)


def test_return_rejects_wrong_states_and_stale_version() -> None:
    with pytest.raises(StateConflictError):
        return_item(
            _request(RequestStatus.RESERVED),
            _equipment(EquipmentState.RESERVED),
            _loan(),
            1,
        )
    with pytest.raises(StateConflictError):
        return_item(
            _request(RequestStatus.ON_LOAN),
            _equipment(EquipmentState.ON_LOAN, version=3),
            _loan(LoanStatus.ON_LOAN),
            2,
        )


@pytest.mark.parametrize(
    "state",
    [EquipmentState.AWAITING_INSPECTION, EquipmentState.REPAIR, EquipmentState.QUARANTINED],
)
def test_inspection_allowed_from_inspectable_states(state: EquipmentState) -> None:
    item = inspect_equipment(_equipment(state), EquipmentState.AVAILABLE, 1)
    assert item.state is EquipmentState.AVAILABLE
    assert item.version == 2


@pytest.mark.parametrize(
    "state",
    [EquipmentState.AVAILABLE, EquipmentState.RESERVED, EquipmentState.ON_LOAN],
)
def test_inspection_refused_from_active_states(state: EquipmentState) -> None:
    with pytest.raises(StateConflictError):
        inspect_equipment(_equipment(state), EquipmentState.AVAILABLE, 1)


def test_inspection_rejects_unsupported_outcome_and_stale_version() -> None:
    with pytest.raises(ValidationFailedError):
        inspect_equipment(_equipment(EquipmentState.AWAITING_INSPECTION), EquipmentState.ON_LOAN, 1)
    with pytest.raises(StateConflictError):
        inspect_equipment(
            _equipment(EquipmentState.AWAITING_INSPECTION), EquipmentState.AVAILABLE, 9
        )


def test_close_after_inspection_only_from_returned() -> None:
    request, loan = close_after_inspection(
        _request(RequestStatus.RETURNED), _loan(LoanStatus.RETURNED)
    )
    assert request.status is RequestStatus.CLOSED
    assert loan.status is LoanStatus.CLOSED
    with pytest.raises(StateConflictError):
        close_after_inspection(_request(RequestStatus.ON_LOAN), _loan(LoanStatus.ON_LOAN))


def test_inspection_action_mapping() -> None:
    assert inspection_action(EquipmentState.AVAILABLE) is EventAction.INSPECTED_AVAILABLE
    assert inspection_action(EquipmentState.REPAIR) is EventAction.INSPECTED_REPAIR
    assert inspection_action(EquipmentState.QUARANTINED) is EventAction.INSPECTED_QUARANTINED
