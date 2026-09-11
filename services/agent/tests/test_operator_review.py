"""Regressions for runner boundaries missed by the original worker suite."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from admission_probe import OFFLINE_DUMMY_KEY
from operator_canary import (
    OperatorGrantError,
    TransportObserver,
    run_operator_canary,
)

from borrowed_steps.infrastructure.canary_receipt import CanaryReceiptStore, CanaryState
from postgres_support import migrated_database
from test_operator_canary import make_happy_path_transport, make_valid_grant


@pytest.mark.anyio
@pytest.mark.parametrize("override", ["fixture", "base", "inventory"])
async def test_override_rejected_before_database(override: str) -> None:
    options: dict[str, dict[str, Any]] = {
        "fixture": {"fixture_text": "unapproved source"},
        "base": {"base_dir": Path.cwd()},
        "inventory": {"inv_reader": object()},
    }
    kwargs = options[override]
    with patch.object(CanaryReceiptStore, "reserve") as reserve:
        with pytest.raises(OperatorGrantError):
            await run_operator_canary(
                OFFLINE_DUMMY_KEY,
                "postgresql://test@127.0.0.1:1/test",
                make_valid_grant(),
                fixture_transport=make_happy_path_transport(),
                **kwargs,
            )
        reserve.assert_not_called()


@pytest.mark.anyio
async def test_observer_close_is_idempotent_and_bounded() -> None:
    class HangingClose(httpx.AsyncBaseTransport):
        async def aclose(self) -> None:
            await asyncio.Event().wait()

    observer = TransportObserver(HangingClose())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(observer.aclose(), timeout=3)
    assert not observer.closed
    transport = make_happy_path_transport()
    observer = TransportObserver(transport)
    await observer.aclose()
    await observer.aclose()
    assert observer.closed


@pytest.mark.anyio
async def test_model_construction_failure_closes_observer_and_blocks() -> None:
    with migrated_database() as db:
        transport = make_happy_path_transport()
        grant = make_valid_grant()
        with patch("operator_canary.GroqModel", side_effect=RuntimeError("synthetic")):
            result = await run_operator_canary(
                OFFLINE_DUMMY_KEY,
                db,
                grant,
                fixture_transport=transport,
            )
        assert result.state == CanaryState.UNCERTAIN
        assert result.actual_sends == 0
        row = CanaryReceiptStore(db).get_receipt(grant.receipt_id)
        assert row is not None
        assert row["concurrency_active"]
        assert result.status_codes == []


@pytest.mark.anyio
async def test_transport_failure_is_uncertain() -> None:
    async def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("synthetic raw error")

    with migrated_database() as db:
        result = await run_operator_canary(
            OFFLINE_DUMMY_KEY,
            db,
            make_valid_grant(),
            fixture_transport=httpx.MockTransport(broken),
        )
        assert result.state == CanaryState.UNCERTAIN
        assert result.actual_sends == 1
        assert "synthetic raw error" not in str(result.as_dict())


@pytest.mark.anyio
async def test_outer_deadline_settles_uncertain() -> None:
    async def stalled(**kwargs: object) -> None:
        await asyncio.Event().wait()

    with migrated_database() as db:
        with (
            patch("operator_canary.DEFAULT_OPERATION_DEADLINE_SECONDS", 0.1),
            patch("operator_canary.run_canary_stages", side_effect=stalled),
        ):
            grant = make_valid_grant()
            result = await asyncio.wait_for(
                run_operator_canary(
                    OFFLINE_DUMMY_KEY,
                    db,
                    grant,
                    fixture_transport=make_happy_path_transport(),
                ),
                timeout=3,
            )
        assert result.state == CanaryState.UNCERTAIN
        assert result.actual_sends == 0
        assert result.cleanup_completed
        row = CanaryReceiptStore(db).get_receipt(grant.receipt_id)
        assert row is not None
        assert row["concurrency_active"]
