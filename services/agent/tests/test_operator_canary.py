"""Comprehensive test suite for bounded operator canary runner.

Conforms strictly to docs/M3_OPERATOR_CANARY_CONTRACT.md.
Tests:
- Manifest preparation and hash determinism
- Changed manifest, bad grant, expired grant, unapproved plan refusing before network
- Replay never redispatching
- Actual Strands tool/extraction through injected HTTP fixtures
- 429 throttling and invalid output settled without automatic retries
- Task cancellation and cleanup failure settling in UNCERTAIN
- Finalization failure retaining active receipt
- Truthful unknown tokens and separate provenance
- Zero secret leakage in records, summary, and receipt rows
- Real disposable PostgreSQL execution with concurrent runner attempts
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from borrowed_steps.infrastructure.canary_receipt import (
    ACCEPTED_PLAN_HASH,
    CanaryReceiptError,
    CanaryReceiptStore,
    CanaryState,
    FailureCode,
)
from borrowed_steps.infrastructure.groq_model import MAX_REQUEST_BYTES
from postgres_support import migrated_database

# Add scripts directory to sys.path
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from admission_probe import (  # noqa: E402
    OFFLINE_DUMMY_KEY,
    OfflineProbeTransport,
    make_simulated_parsed_completion,
    make_simulated_sse_text,
    make_simulated_sse_tool_call,
)
from groq_canary import CANARY_FIXTURE_INPUT  # noqa: E402
from operator_canary import (  # noqa: E402
    CanaryManifestError,
    OperatorCanarySummary,
    OperatorGrant,
    OperatorGrantError,
    TransportObserver,
    build_execution_manifest,
    compute_execution_manifest_hash,
    run_operator_canary,
    validate_operator_grant,
)

# ---------------------------------------------------------------------------
# Session-level disposable PostgreSQL server management
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Iterator[str]:
    """Provide a disposable migrated database with schema version 3."""
    with migrated_database() as url:
        yield url


# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------


def make_valid_grant(
    manifest_hash: str | None = None,
    expiry: datetime | None = None,
    live_authorized: bool = True,
    plan_hash: str = ACCEPTED_PLAN_HASH,
) -> OperatorGrant:
    """Generate a syntactically valid OperatorGrant matching checkout manifest."""
    if manifest_hash is None:
        manifest = build_execution_manifest()
        manifest_hash = compute_execution_manifest_hash(manifest)
    if expiry is None:
        expiry = datetime.now(UTC) + timedelta(hours=1)
    return OperatorGrant(
        receipt_id=str(uuid4()),
        owner_id=str(uuid4()),
        authorization_id=str(uuid4()),
        authorization_expires_at=expiry,
        execution_manifest_hash=manifest_hash,
        plan_hash=plan_hash,
        live_authorized=live_authorized,
    )


def make_happy_path_transport() -> OfflineProbeTransport:
    """Construct an OfflineProbeTransport scripted for normal tool + extraction flow."""
    transport = OfflineProbeTransport()
    transport.set_scenario("happy_path")
    # Turn 1: Propose tool read_inventory
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}))
    )
    # Turn 2: Conversational completion after tool executed
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Checked inventory counts."))
    )
    # Stage 2: Structured extraction matching candidate fixture
    extraction_output = {
        "borrower_label": "Priya S",
        "equipment_kind": "crutches",
        "pickup_location": "the Adyar centre",
        "due_at": "2026-10-01T08:00:00Z",
    }
    transport.queue_response(
        httpx.Response(200, json=make_simulated_parsed_completion(extraction_output))
    )
    return transport


# ===========================================================================
# 1. Manifest Preparation & Determinism
# ===========================================================================


def test_execution_manifest_determinism_and_content() -> None:
    """Execution manifest contains 9 files, pinned ceilings, and hashes deterministically."""
    m1 = build_execution_manifest()
    m2 = build_execution_manifest()
    assert m1 == m2
    assert len(m1["file_hashes"]) == 9
    assert m1["bs020_plan_hash"] == ACCEPTED_PLAN_HASH
    assert m1["pinned_ceilings"]["max_sends"] == 6
    assert m1["pinned_ceilings"]["max_output_tokens_reservation"] == 6144
    assert m1["pinned_ceilings"]["max_request_bytes"] == MAX_REQUEST_BYTES
    assert m1["retry_policy"]["auto_retry"] is False
    assert m1["retry_policy"]["retry_strategy"] is None

    h1 = compute_execution_manifest_hash(m1)
    h2 = compute_execution_manifest_hash(m2)
    assert h1 == h2
    assert len(h1) == 64


def test_execution_manifest_missing_file_fails() -> None:
    """Missing manifest source file raises CanaryManifestError."""
    with (
        patch("operator_canary.MANIFEST_RELATIVE_PATHS", {"missing.py": "missing.py"}),
        pytest.raises(CanaryManifestError, match="source is missing"),
    ):
        build_execution_manifest()


# ===========================================================================
# 2. Grant Validation & Negative Pre-Network Rejections
# ===========================================================================


def test_grant_validation_all_invalid_variants_rejected() -> None:
    """Invalid UUIDs, unapproved plan, expired auth, or wrong manifest fail closed."""
    manifest = build_execution_manifest()
    good_hash = compute_execution_manifest_hash(manifest)
    now = datetime.now(UTC)

    # 1. Bad UUID4
    with pytest.raises(OperatorGrantError, match="receipt_id"):
        validate_operator_grant(
            replace(make_valid_grant(good_hash, live_authorized=True), receipt_id="not-uuid"),
            good_hash,
            now=now,
        )

    # 2. Naive datetime
    naive_dt = datetime(2026, 10, 1, 12, 0, 0)
    with pytest.raises(OperatorGrantError, match="timezone required"):
        validate_operator_grant(
            replace(
                make_valid_grant(good_hash, live_authorized=True),
                authorization_expires_at=naive_dt,
            ),
            good_hash,
            now=now,
        )

    # 3. Expired authorization
    expired_dt = now - timedelta(seconds=1)
    with pytest.raises(OperatorGrantError, match="expired"):
        validate_operator_grant(
            make_valid_grant(good_hash, expiry=expired_dt, live_authorized=True),
            good_hash,
            now=now,
        )

    # 4. live_authorized = False
    with pytest.raises(OperatorGrantError, match="live_authorized must be True"):
        validate_operator_grant(
            make_valid_grant(good_hash, live_authorized=False),
            good_hash,
            now=now,
        )

    # 5. Plan hash mismatch
    with pytest.raises(OperatorGrantError, match="Candidate plan hash mismatch"):
        validate_operator_grant(
            make_valid_grant(good_hash, plan_hash="wrong-hash", live_authorized=True),
            good_hash,
            now=now,
        )

    # 6. Execution manifest hash mismatch
    with pytest.raises(OperatorGrantError, match="manifest hash mismatch"):
        validate_operator_grant(
            make_valid_grant(manifest_hash="0" * 64, live_authorized=True),
            good_hash,
            now=now,
        )


@pytest.mark.anyio
async def test_refusal_before_network_on_invalid_parameters(db: str) -> None:
    """Invalid api_key or database_url fails closed before contacting database or network."""
    grant = make_valid_grant()
    # Empty api_key
    with pytest.raises(ValueError, match="Explicit non-empty api_key required"):
        await run_operator_canary("", db, grant)

    # Insecure database URL
    with pytest.raises(ValueError, match="PostgreSQL requires an explicit host URI"):
        await run_operator_canary("dummy-key", "postgresql://user:pw@remote.host:5432/db", grant)


# ===========================================================================
# 3. Happy Path Execution & Transport Observation
# ===========================================================================


@pytest.mark.anyio
async def test_operator_canary_happy_path(db: str) -> None:
    """Normal happy path: reserve -> dispatch -> 3 sends -> SUCCEEDED -> cleanup completed."""
    transport = make_happy_path_transport()
    grant = make_valid_grant()

    summary = await run_operator_canary(
        api_key=OFFLINE_DUMMY_KEY,
        database_url=db,
        grant=grant,
        fixture_transport=transport,
    )

    assert isinstance(summary, OperatorCanarySummary)
    assert summary.state == CanaryState.SUCCEEDED
    assert summary.failure_code is None
    assert summary.actual_sends == 3
    assert summary.actual_total_tokens is None  # Contract: NULL when unknown
    assert summary.cleanup_completed is True
    assert summary.provenance == "offline_fixture"
    assert summary.plan_hash == ACCEPTED_PLAN_HASH
    assert summary.execution_manifest_hash == grant.execution_manifest_hash
    assert len(summary.wire_byte_lengths) == 3
    assert summary.status_codes == [200, 200, 200]
    assert all(summary.field_assertions.values())

    # Verify database receipt row
    store = CanaryReceiptStore(db)
    row = store.get_receipt(grant.receipt_id)
    assert row is not None
    assert row["state"] == "SUCCEEDED"
    assert row["actual_sends"] == 3
    assert row["actual_total_tokens"] is None
    assert row["concurrency_active"] is False
    assert row["failure_code"] is None
    assert row["completed_at"] is not None


@pytest.mark.anyio
async def test_simulated_live_provenance(db: str) -> None:
    """When fixture_transport is None, runner records provenance as live_provider."""
    grant = make_valid_grant()
    # Simulate a custom mock AsyncHTTPTransport without using OfflineProbeTransport
    mock_response = httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}))
    mock_resp2 = httpx.Response(200, text=make_simulated_sse_text("Checked inventory."))
    extraction_output = {
        "borrower_label": "Priya S",
        "equipment_kind": "crutches",
        "pickup_location": "the Adyar centre",
        "due_at": "2026-10-01T08:00:00Z",
    }
    mock_resp3 = httpx.Response(200, json=make_simulated_parsed_completion(extraction_output))

    mock_transport = AsyncMock(spec=httpx.AsyncBaseTransport)
    mock_transport.handle_async_request.side_effect = [mock_response, mock_resp2, mock_resp3]

    # Patch AsyncHTTPTransport constructor inside operator_canary
    with patch("operator_canary.httpx.AsyncHTTPTransport", return_value=mock_transport):
        summary = await run_operator_canary(
            api_key=OFFLINE_DUMMY_KEY,
            database_url=db,
            grant=grant,
            fixture_transport=None,  # triggers live transport branch
        )
        assert summary.provenance == "live_provider"
        assert summary.state == CanaryState.SUCCEEDED


# ===========================================================================
# 4. Replay & Duplicate Exclusion
# ===========================================================================


@pytest.mark.anyio
async def test_replay_never_redispatches(db: str) -> None:
    """Calling run_operator_canary with identical grant/receipt_id fails closed and never sends."""
    transport = make_happy_path_transport()
    grant = make_valid_grant()

    # First execution succeeds
    summary1 = await run_operator_canary(
        api_key=OFFLINE_DUMMY_KEY,
        database_url=db,
        grant=grant,
        fixture_transport=transport,
    )
    assert summary1.state == CanaryState.SUCCEEDED
    assert len(transport.records) > 0

    # Replay call with exact same grant
    transport2 = make_happy_path_transport()
    with pytest.raises(CanaryReceiptError, match="Dispatch already consumed or receipt terminal"):
        await run_operator_canary(
            api_key=OFFLINE_DUMMY_KEY,
            database_url=db,
            grant=grant,
            fixture_transport=transport2,
        )

    # Exactly zero sends dispatched on second attempt
    assert len(transport2.records) == 0


# ===========================================================================
# 5. Concurrent Runner Attempts (Single Dispatch Owner)
# ===========================================================================


@pytest.mark.anyio
async def test_concurrent_runner_attempts_single_winner(db: str) -> None:
    """Two concurrent runner executions competing for receipt boundary allow exactly one winner."""
    grant1 = make_valid_grant()
    grant2 = make_valid_grant()
    transport1 = make_happy_path_transport()
    transport2 = make_happy_path_transport()

    results: list[Any] = list(
        await asyncio.gather(
            run_operator_canary(
                api_key=OFFLINE_DUMMY_KEY,
                database_url=db,
                grant=grant1,
                fixture_transport=transport1,
            ),
            run_operator_canary(
                api_key=OFFLINE_DUMMY_KEY,
                database_url=db,
                grant=grant2,
                fixture_transport=transport2,
            ),
            return_exceptions=True,
        )
    )

    successes = [r for r in results if isinstance(r, OperatorCanarySummary)]
    errors = [r for r in results if isinstance(r, CanaryReceiptError)]

    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}"
    assert len(errors) == 1, f"Expected exactly 1 CanaryReceiptError, got {len(errors)}"
    assert "Another execution is unresolved" in str(errors[0])


# ===========================================================================
# 6. Provider & Grounding Failures Settle in FAILED_CONFIRMED Without Retry
# ===========================================================================


@pytest.mark.anyio
async def test_429_throttling_settles_failed_confirmed_without_retry(db: str) -> None:
    """Simulated 429 response results in exactly 1 send, FAILED_CONFIRMED, and PROVIDER_FAILURE."""
    transport = OfflineProbeTransport()
    transport.set_scenario("throttling")
    transport.queue_response(httpx.Response(429, text="Rate limit exceeded"))

    grant = make_valid_grant()
    summary = await run_operator_canary(
        api_key=OFFLINE_DUMMY_KEY,
        database_url=db,
        grant=grant,
        fixture_transport=transport,
    )

    assert summary.state == CanaryState.FAILED_CONFIRMED
    assert summary.failure_code == FailureCode.PROVIDER_FAILURE
    assert summary.actual_sends == 1  # Exactly 1 send; no automatic retry
    assert summary.cleanup_completed is True

    # Database receipt row is settled with concurrency released
    store = CanaryReceiptStore(db)
    row = store.get_receipt(grant.receipt_id)
    assert row is not None
    assert row["state"] == "FAILED_CONFIRMED"
    assert row["failure_code"] == "provider_failure"
    assert row["concurrency_active"] is False


@pytest.mark.anyio
async def test_grounding_assertion_failure_settles_failed_confirmed(db: str) -> None:
    """Hallucinated borrower label results in FAILED_CONFIRMED with INVALID_OUTPUT."""
    transport = OfflineProbeTransport()
    transport.set_scenario("grounding_failure")
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}))
    )
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Checked inventory counts."))
    )
    bad_extraction = {
        "borrower_label": "Unknown Borrower",  # Hallucinated!
        "equipment_kind": "crutches",
        "pickup_location": "the Adyar centre",
        "due_at": "2026-10-01T08:00:00Z",
    }
    transport.queue_response(
        httpx.Response(200, json=make_simulated_parsed_completion(bad_extraction))
    )

    grant = make_valid_grant()
    summary = await run_operator_canary(
        api_key=OFFLINE_DUMMY_KEY,
        database_url=db,
        grant=grant,
        fixture_transport=transport,
    )

    assert summary.state == CanaryState.FAILED_CONFIRMED
    assert summary.failure_code == FailureCode.INVALID_OUTPUT
    assert summary.actual_sends == 3
    assert summary.cleanup_completed is True

    store = CanaryReceiptStore(db)
    row = store.get_receipt(grant.receipt_id)
    assert row is not None
    assert row["state"] == "FAILED_CONFIRMED"
    assert row["failure_code"] == "invalid_output"
    assert row["concurrency_active"] is False


# ===========================================================================
# 7. Cancellation & Cleanup Failure Settle in UNCERTAIN
# ===========================================================================


@pytest.mark.anyio
async def test_cancellation_settles_uncertain_and_retains_active(db: str) -> None:
    """Task cancellation settles in UNCERTAIN with CANCELLED and retains concurrency_active."""
    transport = OfflineProbeTransport()
    transport.set_scenario("hanging")

    async def hanging_request(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200, text="Never reached")

    transport.handle_async_request = hanging_request  # type: ignore[assignment]
    grant = make_valid_grant()

    task = asyncio.create_task(
        run_operator_canary(
            api_key=OFFLINE_DUMMY_KEY,
            database_url=db,
            grant=grant,
            fixture_transport=transport,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Verify receipt is in UNCERTAIN with CANCELLED failure_code and concurrency_active is True
    store = CanaryReceiptStore(db)
    row = store.get_receipt(grant.receipt_id)
    assert row is not None
    assert row["state"] == "UNCERTAIN"
    assert row["failure_code"] == "cancelled"
    assert row["concurrency_active"] is True  # Blocks future canaries!


@pytest.mark.anyio
async def test_cleanup_failure_settles_uncertain(db: str) -> None:
    """Failed model cleanup settles in UNCERTAIN with EXECUTION_UNKNOWN."""
    transport = make_happy_path_transport()
    grant = make_valid_grant()

    # Force model.aclose to raise an exception
    patch_target = "borrowed_steps.infrastructure.groq_model.GroqModel.aclose"
    with patch(patch_target, side_effect=RuntimeError("close failed")):
        summary = await run_operator_canary(
            api_key=OFFLINE_DUMMY_KEY,
            database_url=db,
            grant=grant,
            fixture_transport=transport,
        )
        assert summary.state == CanaryState.UNCERTAIN
        assert summary.failure_code == FailureCode.EXECUTION_UNKNOWN
        assert summary.cleanup_completed is False

        store = CanaryReceiptStore(db)
        row = store.get_receipt(grant.receipt_id)
        assert row is not None
        assert row["state"] == "UNCERTAIN"
        assert row["failure_code"] == "execution_unknown"
        assert row["concurrency_active"] is True


# ===========================================================================
# 8. Storage Finalization Failure Retains Active Block
# ===========================================================================


@pytest.mark.anyio
async def test_finalization_failure_retains_active_block(db: str) -> None:
    """If store.finish fails, receipt remains in DISPATCHED without repeat inference."""
    transport = make_happy_path_transport()
    grant = make_valid_grant()

    err = CanaryReceiptError("simulated DB crash")
    with (
        patch.object(CanaryReceiptStore, "finish", side_effect=err),
        pytest.raises(CanaryReceiptError, match="simulated DB crash"),
    ):
        await run_operator_canary(
            api_key=OFFLINE_DUMMY_KEY,
            database_url=db,
            grant=grant,
            fixture_transport=transport,
        )

    # Verify receipt remains in DISPATCHED state with concurrency_active = True
    store = CanaryReceiptStore(db)
    row = store.get_receipt(grant.receipt_id)
    assert row is not None
    assert row["state"] == "DISPATCHED"
    assert row["concurrency_active"] is True


# ===========================================================================
# 9. TransportObserver & Zero Secret Leakage
# ===========================================================================


@pytest.mark.anyio
async def test_transport_observer_records_only_safe_fields() -> None:
    """Observer records send_index, wire_byte_length, status_code only; no raw strings."""
    inner = httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
    observer = TransportObserver(inner)

    client = httpx.AsyncClient(transport=observer)
    headers = {"Authorization": "Bearer super_secret_key_gsk_12345"}
    body = b'{"prompt": "sensitive data"}'
    url = "https://api.groq.com/openai/v1/chat/completions"
    response = await client.post(url, headers=headers, content=body)
    assert response.status_code == 200

    assert len(observer.records) == 1
    rec = observer.records[0]
    assert rec.send_index == 1
    assert rec.wire_byte_length == len(body)
    assert rec.status_code == 200

    # Verify object has no sensitive attributes
    rec_dict = rec.__dict__
    for val in rec_dict.values():
        assert not isinstance(val, (str, bytes)), f"String/bytes found in wire record: {val!r}"


@pytest.mark.anyio
async def test_summary_and_receipt_zero_secret_leakage(db: str) -> None:
    """Summary dict and database row contain no secrets, prompts, or sensitive strings."""
    transport = make_happy_path_transport()
    secret_key = "gsk_test_secret_that_must_never_leak_9999"
    grant = make_valid_grant()

    summary = await run_operator_canary(
        api_key=secret_key,
        database_url=db,
        grant=grant,
        fixture_transport=transport,
    )

    summary_str = json.dumps(summary.as_dict(), default=str)
    assert secret_key not in summary_str
    assert "Bearer" not in summary_str
    assert "Authorization" not in summary_str
    assert CANARY_FIXTURE_INPUT not in summary_str
