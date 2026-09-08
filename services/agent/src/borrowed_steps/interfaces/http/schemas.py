"""Strict request schemas for the frozen M1 API contract.

Every body rejects unknown fields. Strictness is applied per field rather than
globally, because FastAPI validates already-parsed Python objects: a global
strict mode would also reject the ISO-8601 *string* that the contract requires
for ``due_at``. So numbers, flags and text are strict, timestamps must arrive as
strings (never a bare epoch number), and enumerations accept only their exact
member values.

``human_approved`` is deliberately optional here: a missing affirmation is an
APPROVAL_REQUIRED decision made by the application, not a schema error.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field

from borrowed_steps.domain.models import EquipmentKind

__all__ = [
    "CreateRequestBody",
    "InspectionBody",
    "InterpretBody",
    "ReservationBody",
    "TransitionBody",
    "WorkspaceBody",
]

_ID_MAX = 100
INTAKE_TEXT_MAX_LENGTH = 2000


def _must_be_a_string(value: object) -> object:
    if not isinstance(value, str):
        msg = "must be an ISO-8601 timestamp string"
        raise ValueError(msg)
    return value


IsoTimestamp = Annotated[AwareDatetime, BeforeValidator(_must_be_a_string)]
"""An ISO-8601 timestamp string carrying an explicit offset."""

Approval = Annotated[bool | None, Field(default=None, strict=True)]
"""An explicit human affirmation. Absent and ``false`` are both refusals."""

ExpectedVersion = Annotated[int, Field(ge=1, strict=True)]
"""The equipment version the caller believes it is acting on."""


def _trimmed_intake(value: str) -> str:
    text = value.strip()
    if not 1 <= len(text) <= INTAKE_TEXT_MAX_LENGTH:
        msg = f"intake text must be 1 to {INTAKE_TEXT_MAX_LENGTH} characters once trimmed"
        raise ValueError(msg)
    return text


IntakeText = Annotated[str, Field(strict=True), AfterValidator(_trimmed_intake)]
"""Free intake text, measured after trimming and returned already trimmed."""


class _Strict(BaseModel):
    """Reject unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkspaceBody(_Strict):
    """``POST /api/workspaces`` takes an empty object."""


class CreateRequestBody(_Strict):
    """``POST /api/requests``."""

    borrower_label: str = Field(min_length=1, max_length=60, strict=True)
    equipment_kind: EquipmentKind
    pickup_location: str = Field(min_length=1, max_length=120, strict=True)
    due_at: IsoTimestamp


class ReservationBody(_Strict):
    """``POST /api/reservations``."""

    request_id: str = Field(min_length=1, max_length=_ID_MAX, strict=True)
    equipment_id: str = Field(min_length=1, max_length=_ID_MAX, strict=True)
    expected_equipment_version: ExpectedVersion
    human_approved: Approval


class TransitionBody(_Strict):
    """``POST /api/loans/{id}/pickup`` and ``POST /api/loans/{id}/return``."""

    expected_equipment_version: ExpectedVersion
    human_approved: Approval


class InspectionBody(_Strict):
    """``POST /api/equipment/{id}/inspection``."""

    expected_equipment_version: ExpectedVersion
    outcome: Literal["AVAILABLE", "REPAIR", "QUARANTINED"]
    human_approved: Approval


class InterpretBody(_Strict):
    """``POST /api/intake/interpret``. Read-only: this route writes nothing."""

    text: IntakeText
