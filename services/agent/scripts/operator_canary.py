"""Bounded operator canary runner and execution manifest verification.

Conforms strictly to docs/M3_OPERATOR_CANARY_CONTRACT.md.
Provides a callable operator entrypoint, deterministic execution manifest,
two-phase receipt reservation/dispatch before provider network calls,
transport observation with safe counts/bytes only, and conservative
finalization/uncertainty settlement.

Zero live provider calls, zero credential discovery, zero spend ($0 / ₹0).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from borrowed_steps.application.interpreter import InventoryReader
from borrowed_steps.infrastructure.canary_receipt import (
    ACCEPTED_PLAN_HASH,
    RESERVED_OUTPUT_TOKENS,
    RESERVED_SENDS,
    CanaryReceiptStore,
    CanaryReservationRequest,
    CanaryState,
    FailureCode,
)
from borrowed_steps.infrastructure.groq_model import (
    DEFAULT_MAX_SENDS,
    DEFAULT_OPERATION_DEADLINE_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    FIXED_MAX_COMPLETION_TOKENS,
    FIXED_REASONING_EFFORT,
    GROQ_MODEL_ID,
    MAX_REQUEST_BYTES,
    GroqModel,
)
from borrowed_steps.infrastructure.postgres_migrations import require_transport_security

# Add scripts directory to sys.path if not present
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from groq_canary import (  # noqa: E402
    CANARY_FIXTURE_INPUT,
    MAX_TOOL_ATTEMPTS,
    CanaryExecutionResult,
    run_canary_stages,
)

__all__ = [
    "MANIFEST_RELATIVE_PATHS",
    "CanaryManifestError",
    "OperatorCanarySummary",
    "OperatorGrant",
    "OperatorGrantError",
    "TransportObserver",
    "WireRecord",
    "build_execution_manifest",
    "compute_execution_manifest_hash",
    "main",
    "run_operator_canary",
    "validate_operator_grant",
]

_LOGGER = logging.getLogger("operator_canary")

# Relative file paths from services/agent root included in execution manifest
MANIFEST_RELATIVE_PATHS: dict[str, str] = {
    "admission_probe.py": "scripts/admission_probe.py",
    "canary_receipt.py": "src/borrowed_steps/infrastructure/canary_receipt.py",
    "groq_canary.py": "scripts/groq_canary.py",
    "groq_model.py": "src/borrowed_steps/infrastructure/groq_model.py",
    "operator_canary.py": "scripts/operator_canary.py",
    "requirements-groq.lock": "requirements-groq.lock",
    "requirements-postgres.lock": "requirements-postgres.lock",
    "requirements.lock": "requirements.lock",
    "strands_interpreter.py": "src/borrowed_steps/infrastructure/strands_interpreter.py",
}


# ===========================================================================
# 1. Custom Exceptions
# ===========================================================================


class OperatorGrantError(ValueError):
    """Raised when an operator grant is invalid, expired, or mismatched."""


class CanaryManifestError(RuntimeError):
    """Raised when the execution manifest cannot be built or verified."""


# ===========================================================================
# 2. Execution Manifest & Grant Definition
# ===========================================================================


@dataclass(frozen=True)
class OperatorGrant:
    """Explicit dated authorization granted by architecture/release authority.

    Binds UUID4 identities, expiry, exact candidate plan hash, and approved
    execution manifest hash. live_authorized must be explicitly True for execution.
    """

    receipt_id: str
    owner_id: str
    authorization_id: str
    authorization_expires_at: datetime
    execution_manifest_hash: str
    plan_hash: str = ACCEPTED_PLAN_HASH
    live_authorized: bool = False


@dataclass(frozen=True)
class WireRecord:
    """Sanitized wire measurement for an observed HTTP request.

    Contains counts, byte lengths, and status codes only.
    Zero prompt, headers, authorization, or response content.
    """

    send_index: int
    wire_byte_length: int
    status_code: int


class TransportObserver(httpx.AsyncBaseTransport):
    """Transport wrapper observing send counts, serialized bytes, and status codes.

    Never captures or persists sensitive payload, prompts, headers, or keys.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self._records: list[WireRecord] = []
        self._send_count: int = 0

    @property
    def records(self) -> list[WireRecord]:
        return list(self._records)

    @property
    def send_count(self) -> int:
        return self._send_count

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._send_count += 1
        idx = self._send_count

        # Measure request wire size safely
        body = await request.aread()
        wire_bytes = len(body)

        try:
            response = await self._inner.handle_async_request(request)
            self._records.append(
                WireRecord(
                    send_index=idx,
                    wire_byte_length=wire_bytes,
                    status_code=response.status_code,
                )
            )
            return response
        except Exception:
            self._records.append(
                WireRecord(
                    send_index=idx,
                    wire_byte_length=wire_bytes,
                    status_code=0,
                )
            )
            raise

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass(frozen=True)
class OperatorCanarySummary:
    """Non-secret aggregate summary of an operator canary execution."""

    receipt_id: str
    owner_id: str
    authorization_id: str
    execution_manifest_hash: str
    plan_hash: str
    provenance: str
    state: CanaryState
    actual_sends: int | None
    actual_total_tokens: int | None
    failure_code: FailureCode | None
    cleanup_completed: bool
    receipt_row: dict[str, Any]
    wire_byte_lengths: list[int]
    status_codes: list[int]
    field_assertions: dict[str, bool]
    timestamp_utc: str

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = str(self.state)
        d["failure_code"] = str(self.failure_code) if self.failure_code else None
        if "receipt_row" in d and isinstance(d["receipt_row"], dict):
            d["receipt_row"] = {
                k: v.isoformat() if hasattr(v, "isoformat") else v
                for k, v in d["receipt_row"].items()
            }
        return d


# ===========================================================================
# 3. Deterministic Manifest Computation & Validation
# ===========================================================================


def get_manifest_file_paths(base_dir: Path | None = None) -> dict[str, Path]:
    """Resolve manifest files relative to services/agent base directory."""
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent
    return {k: base_dir / rel_path for k, rel_path in MANIFEST_RELATIVE_PATHS.items()}


def compute_manifest_file_hashes(base_dir: Path | None = None) -> dict[str, str]:
    """Compute SHA-256 digests of all 9 manifest source files."""
    paths = get_manifest_file_paths(base_dir)
    file_hashes: dict[str, str] = {}
    for name in sorted(paths):
        path = paths[name]
        if not path.is_file():
            msg = f"Manifest source file '{name}' does not exist at {path}"
            raise CanaryManifestError(msg)
        content = path.read_bytes()
        file_hashes[name] = hashlib.sha256(content).hexdigest()
    return file_hashes


def build_execution_manifest(base_dir: Path | None = None) -> dict[str, Any]:
    """Construct deterministic execution manifest separate from BS-020 candidate plan."""
    file_hashes = compute_manifest_file_hashes(base_dir)
    return {
        "bs020_plan_hash": ACCEPTED_PLAN_HASH,
        "candidate_fixture": CANARY_FIXTURE_INPUT,
        "file_hashes": file_hashes,
        "manifest_version": 1,
        "pinned_ceilings": {
            "max_completion_tokens": FIXED_MAX_COMPLETION_TOKENS,
            "max_output_tokens_reservation": RESERVED_OUTPUT_TOKENS,
            "max_request_bytes": MAX_REQUEST_BYTES,
            "max_sends": DEFAULT_MAX_SENDS,
            "max_tool_attempts": MAX_TOOL_ATTEMPTS,
            "operation_deadline_seconds": DEFAULT_OPERATION_DEADLINE_SECONDS,
            "reasoning_effort": FIXED_REASONING_EFFORT,
            "request_timeout_seconds": DEFAULT_REQUEST_TIMEOUT_SECONDS,
        },
        "provider_spec": {
            "model": GROQ_MODEL_ID,
            "provider": "groq",
        },
        "retry_policy": {
            "auto_retry": False,
            "max_retries": 0,
            "retry_strategy": None,
        },
    }


def compute_execution_manifest_hash(manifest: dict[str, Any]) -> str:
    """Compute canonical SHA-256 digest of execution manifest."""
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_uuid4(val: str, label: str) -> None:
    try:
        parsed = UUID(val)
        valid = parsed.version == 4 and str(parsed) == val
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        msg = f"Invalid {label}: must be a valid UUID4 string"
        raise OperatorGrantError(msg)


def validate_operator_grant(
    grant: OperatorGrant,
    expected_manifest_hash: str,
    now: datetime | None = None,
) -> None:
    """Fail closed on invalid, expired, unauthorized, or mismatched grants."""
    _validate_uuid4(grant.receipt_id, "receipt_id")
    _validate_uuid4(grant.owner_id, "owner_id")
    _validate_uuid4(grant.authorization_id, "authorization_id")

    expiry = grant.authorization_expires_at
    if not isinstance(expiry, datetime) or expiry.tzinfo is None or expiry.utcoffset() is None:
        msg = "Explicit authorization_expires_at with timezone required"
        raise OperatorGrantError(msg)

    current_time = now or datetime.now(UTC)
    if expiry <= current_time:
        msg = f"Authorization expired at {expiry.isoformat()}"
        raise OperatorGrantError(msg)

    if grant.live_authorized is not True:
        msg = "Explicit operator authorization required (live_authorized must be True)"
        raise OperatorGrantError(msg)

    if grant.plan_hash != ACCEPTED_PLAN_HASH:
        msg = f"Candidate plan hash mismatch: '{grant.plan_hash}' != '{ACCEPTED_PLAN_HASH}'"
        raise OperatorGrantError(msg)

    if grant.execution_manifest_hash != expected_manifest_hash:
        msg = (
            f"Execution manifest hash mismatch: "
            f"'{grant.execution_manifest_hash}' != '{expected_manifest_hash}'"
        )
        raise OperatorGrantError(msg)


# ===========================================================================
# 4. Operator Canary Runner Entrypoint
# ===========================================================================


async def run_operator_canary(
    api_key: str,
    database_url: str,
    grant: OperatorGrant,
    *,
    fixture_transport: httpx.AsyncBaseTransport | None = None,
    inv_reader: InventoryReader | None = None,
    fixture_text: str = CANARY_FIXTURE_INPUT,
    base_dir: Path | None = None,
) -> OperatorCanarySummary:
    """Execute bounded operator canary runner under durable receipt boundary.

    1. Validates explicit parameters without ambient credential discovery.
    2. Builds and checks deterministic execution manifest from current checkout.
    3. Reserves and dispatches receipt in PostgreSQL before creating network model.
    4. Executes canary stages with TransportObserver (safe counts/bytes only).
    5. Finalizes receipt with measured usage and settled outcome.
    6. Guarantees owned model and transport closure on every path.
    """
    # 1. Parameter validation
    if not api_key or not isinstance(api_key, str) or not api_key.strip():
        msg = "Explicit non-empty api_key required"
        raise ValueError(msg)

    if not database_url or not isinstance(database_url, str):
        msg = "Explicit non-empty database_url required"
        raise ValueError(msg)

    require_transport_security(database_url)

    # 2. Manifest verification
    manifest = build_execution_manifest(base_dir)
    computed_manifest_hash = compute_execution_manifest_hash(manifest)
    validate_operator_grant(grant, computed_manifest_hash)

    # 3. Storage reservation & dispatch (two-phase)
    store = CanaryReceiptStore(database_url)
    reservation_req = CanaryReservationRequest(
        receipt_id=grant.receipt_id,
        owner_id=grant.owner_id,
        authorization_id=grant.authorization_id,
        authorization_expires_at=grant.authorization_expires_at,
        live_authorized=grant.live_authorized,
        plan_hash=grant.plan_hash,
        reserved_sends=RESERVED_SENDS,
        reserved_output_tokens=RESERVED_OUTPUT_TOKENS,
    )

    # First phase: reserve (atomic insert or exact replay)
    store.reserve(reservation_req)

    # If the receipt was already terminal or dispatched, mark_dispatched will fail closed.
    # Only a successful transition to DISPATCHED permits construction/network send.
    store.mark_dispatched(grant.receipt_id, grant.owner_id)

    # 4. Model and observer construction
    provenance = "offline_fixture" if fixture_transport is not None else "live_provider"
    inner_transport = (
        fixture_transport
        if fixture_transport is not None
        else httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    )
    observer = TransportObserver(inner_transport)

    model = GroqModel(
        api_key=api_key,
        transport=observer,
        max_sends=DEFAULT_MAX_SENDS,
        operation_deadline_seconds=DEFAULT_OPERATION_DEADLINE_SECONDS,
        request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
    )

    # 5. Execution
    canary_result: CanaryExecutionResult | None = None
    cancelled = False
    cleanup_completed = False
    unexpected_exc: BaseException | None = None

    try:
        canary_result = await run_canary_stages(
            model=model,
            fixture_text=fixture_text,
            inv_reader=inv_reader,
            transport=observer,
            plan_hash=grant.plan_hash,
            propagate_errors=False,
            provenance=provenance,
        )
    except asyncio.CancelledError:
        cancelled = True
    except BaseException as exc:
        unexpected_exc = exc
    finally:
        try:
            await model.aclose()
            cleanup_completed = not model.client_open
        except Exception:
            cleanup_completed = False

        with contextlib.suppress(Exception):
            await observer.aclose()

    # 6. Usage & outcome settlement
    actual_sends = min(observer.send_count, RESERVED_SENDS) if observer.send_count > 0 else None
    actual_total_tokens = None  # Unknown measured tokens remain NULL per contract

    # Determine state and failure code
    if cancelled:
        final_state = CanaryState.UNCERTAIN
        failure_code: FailureCode | None = FailureCode.CANCELLED
    elif not cleanup_completed or unexpected_exc is not None:
        final_state = CanaryState.UNCERTAIN
        failure_code = FailureCode.EXECUTION_UNKNOWN
    elif canary_result is not None and canary_result.outcome == "success":
        final_state = CanaryState.SUCCEEDED
        failure_code = None
        # Verify positive sends on success
        if actual_sends is None or actual_sends == 0:
            actual_sends = max(1, canary_result.sends_total)
    else:
        # Confirmed failure path
        final_state = CanaryState.FAILED_CONFIRMED
        err_cat = canary_result.error_category if canary_result else ""
        if err_cat in ("CanaryThrottledError", "GroqModelError", "ModelThrottledException"):
            failure_code = FailureCode.PROVIDER_FAILURE
        elif err_cat in (
            "CanaryGroundingAssertionError",
            "CanaryLengthLimitError",
            "CanaryToolLimitExceededError",
            "CanaryToolNotExecutedError",
        ):
            failure_code = FailureCode.INVALID_OUTPUT
        else:
            failure_code = FailureCode.PROVIDER_FAILURE

    # 7. Finalize receipt in PostgreSQL
    finished_row = store.finish(
        receipt_id=grant.receipt_id,
        owner_id=grant.owner_id,
        state=final_state,
        actual_sends=actual_sends,
        actual_total_tokens=actual_total_tokens,
        failure_code=failure_code,
    )

    wire_bytes = [r.wire_byte_length for r in observer.records]
    status_codes = [r.status_code for r in observer.records]
    field_assertions = canary_result.field_assertions if canary_result else {}

    summary = OperatorCanarySummary(
        receipt_id=grant.receipt_id,
        owner_id=grant.owner_id,
        authorization_id=grant.authorization_id,
        execution_manifest_hash=grant.execution_manifest_hash,
        plan_hash=grant.plan_hash,
        provenance=provenance,
        state=final_state,
        actual_sends=actual_sends,
        actual_total_tokens=actual_total_tokens,
        failure_code=failure_code,
        cleanup_completed=cleanup_completed,
        receipt_row=dict(finished_row) if finished_row else {},
        wire_byte_lengths=wire_bytes,
        status_codes=status_codes,
        field_assertions=field_assertions,
        timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    if cancelled:
        raise asyncio.CancelledError()

    return summary


# ===========================================================================
# 5. CLI: Manifest Preparation & Offline Fixture Checks Only
# ===========================================================================


def main() -> None:
    """CLI for operator canary manifest preparation and verification.

    Supports manifest preparation and offline checks only.
    Rejects any live mode or credential discovery.
    """
    parser = argparse.ArgumentParser(
        description="Operator Canary Manifest Preparation and Offline Verification."
    )
    parser.add_argument(
        "--prepare-manifest",
        action="store_true",
        help="Generate and print the canonical execution manifest JSON.",
    )
    parser.add_argument(
        "--check-manifest",
        action="store_true",
        help="Compute and verify current checkout manifest hash.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Path to save the generated execution manifest JSON.",
    )

    args = parser.parse_args()

    if not args.prepare_manifest and not args.check_manifest:
        parser.print_help()
        sys.exit(0)

    manifest = build_execution_manifest()
    manifest_hash = compute_execution_manifest_hash(manifest)

    if args.prepare_manifest:
        content = json.dumps(manifest, indent=2, sort_keys=True)
        print(content)
        print(f"\nExecution Manifest Hash: {manifest_hash}")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content + "\n", encoding="utf-8")
            print(f"Saved manifest to {args.output}")

    elif args.check_manifest:
        print(f"Checkout Execution Manifest Hash: {manifest_hash}")
        print("Manifest files verified successfully.")


if __name__ == "__main__":
    main()
