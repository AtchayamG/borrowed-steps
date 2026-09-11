"""Pure validation checks for the operator receipt boundary."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from borrowed_steps.infrastructure.canary_receipt import (
    CanaryReceiptError,
    CanaryReservationRequest,
    request_payload,
)


def candidate() -> CanaryReservationRequest:
    return CanaryReservationRequest(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        datetime.now(UTC) + timedelta(hours=1),
        live_authorized=True,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"receipt_id": ""},
        {"receipt_id": "not-random"},
        {"owner_id": ""},
        {"authorization_id": ""},
        {"authorization_expires_at": datetime(2026, 1, 1)},
        {"live_authorized": False},
        {"live_authorized": 1},
        {"live_authorized": "true"},
        {"plan_hash": "wrong"},
        {"provider": "other"},
        {"model": "other"},
        {"reserved_sends": 1},
        {"reserved_sends": 7},
        {"reserved_sends": True},
        {"reserved_output_tokens": 1024},
        {"reserved_output_tokens": 6145},
        {"reserved_output_tokens": -1},
        {"reserved_output_tokens": None},
    ],
)
def test_invalid_fields_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(CanaryReceiptError):
        request_payload(replace(candidate(), **changes))


def test_canonical_payload_and_changed_identity() -> None:
    req = candidate()
    assert request_payload(req) == request_payload(replace(req))
    assert request_payload(req) != request_payload(replace(req, owner_id=str(uuid4())))
    assert '"reserved_output_tokens":6144' in request_payload(req)
