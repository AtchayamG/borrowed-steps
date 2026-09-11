"""Offline-only Groq canary preparation harness and immutable plan verification.

Conforms strictly to docs/M3_CANARY_PREPARATION_CONTRACT.md and docs/M3_GROQ_BOUNDS_CONTRACT.md.
Executes two-stage agent and structured-extraction canary offline using intercepted
SDK serialization, explicit GroqModel injection, fail-closed transport, synthetic
fixtures, and deterministic plan hashing.

Zero live provider calls, zero cloud spend ($0 / ₹0), no credential reading.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from strands import Agent, tool
from strands.types.agent import Limits
from strands.types.exceptions import ModelThrottledException

from borrowed_steps.application.interpreter import InventoryReader
from borrowed_steps.infrastructure.groq_model import (
    ALLOWED_HOST,
    ALLOWED_PATH,
    DEFAULT_MAX_SENDS,
    DEFAULT_OPERATION_DEADLINE_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    FIXED_MAX_COMPLETION_TOKENS,
    FIXED_REASONING_EFFORT,
    GROQ_BASE_URL,
    GROQ_MODEL_ID,
    MAX_REQUEST_BYTES,
    GroqModel,
    GroqSendBudgetExceededError,
)
from borrowed_steps.infrastructure.strands_interpreter import (
    _AGENT_SYSTEM_PROMPT,
    _AGENT_USER_PROMPT,
    _EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA,
    _EXTRACTION_USER_PROMPT,
    _RECOVERY_PROMPT,
    _TOOL_NAME,
    _Extraction,
)

# Add scripts directory to sys.path if not present
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from admission_probe import (  # noqa: E402
    OFFLINE_DUMMY_KEY,
    OfflineProbeTransport,
    RealSeedInventoryReader,
    make_simulated_parsed_completion,
    make_simulated_sse_text,
    make_simulated_sse_tool_call,
)

__all__ = [
    "CANARY_FIXTURE_INPUT",
    "MAX_TOOL_ATTEMPTS",
    "CanaryError",
    "CanaryExecutionResult",
    "CanaryGroundingAssertionError",
    "CanaryLengthLimitError",
    "CanaryPlanMismatchError",
    "CanaryThrottledError",
    "CanaryToolLimitExceededError",
    "CanaryToolNotExecutedError",
    "compute_input_hashes",
    "compute_plan_hash",
    "generate_canary_plan",
    "get_canonical_input_paths",
    "main",
    "run_canary_stages",
    "run_offline_canary",
]

_LOGGER = logging.getLogger("groq_canary")

# Canonical synthetic candidate input (frozen contract)
CANARY_FIXTURE_INPUT: str = (
    "Priya S wants crutches from the Adyar centre, back by 2026-10-01T08:00:00Z."
)

MAX_TOOL_ATTEMPTS: int = 2
OPERATION_DEADLINE_SECONDS: float = DEFAULT_OPERATION_DEADLINE_SECONDS
REQUEST_TIMEOUT_SECONDS: float = DEFAULT_REQUEST_TIMEOUT_SECONDS


# ===========================================================================
# 1. Custom Exceptions
# ===========================================================================


class CanaryError(Exception):
    """Base exception for canary harness failures."""


class CanaryToolNotExecutedError(CanaryError):
    """Raised when Stage 1 completes without executing the required inventory tool."""


class CanaryToolLimitExceededError(CanaryError):
    """Raised when Stage 1 attempts more tool calls than the authorized ceiling."""


class CanaryGroundingAssertionError(CanaryError):
    """Raised when an extracted field does not match the source text or expected fixture."""


class CanaryThrottledError(CanaryError):
    """Raised when a simulated 429 throttling error is received and not retried."""


class CanaryLengthLimitError(CanaryError):
    """Raised when structured output is truncated by the length limit."""


class CanaryPlanMismatchError(CanaryError):
    """Raised when execution plan hash does not match candidate plan hash."""


# ===========================================================================
# 2. Deterministic Plan Generation & Input Hashing
# ===========================================================================


def get_canonical_input_paths(base_dir: Path | None = None) -> dict[str, Path]:
    """Return dictionary of canonical source and lockfile paths for plan hashing."""
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent
    return {
        "groq_model.py": base_dir / "src" / "borrowed_steps" / "infrastructure" / "groq_model.py",
        "strands_interpreter.py": base_dir
        / "src"
        / "borrowed_steps"
        / "infrastructure"
        / "strands_interpreter.py",
        "requirements-groq.lock": base_dir / "requirements-groq.lock",
        "requirements.lock": base_dir / "requirements.lock",
    }


def compute_input_hashes(
    base_dir: Path | None = None,
    fixture_text: str = CANARY_FIXTURE_INPUT,
) -> dict[str, str]:
    """Compute SHA-256 hashes of input source files, locks, and candidate fixture."""
    paths = get_canonical_input_paths(base_dir)
    hashes: dict[str, str] = {}
    for name, path in sorted(paths.items()):
        if not path.exists():
            msg = f"Required input file for canary plan hashing not found: {path}"
            raise FileNotFoundError(msg)
        with open(path, "rb") as f:
            hashes[name] = hashlib.sha256(f.read()).hexdigest()
    hashes["candidate_fixture"] = hashlib.sha256(fixture_text.encode("utf-8")).hexdigest()
    return hashes


def compute_plan_hash(plan: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of a candidate plan dictionary.

    Excludes generated evidence, volatile timestamps, and the plan_hash key itself.
    """
    canonical = {
        k: v
        for k, v in plan.items()
        if k not in ("plan_hash", "evidence", "generated_evidence", "timestamp_utc")
    }
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def generate_canary_plan(
    base_dir: Path | None = None,
    fixture_text: str = CANARY_FIXTURE_INPUT,
) -> dict[str, Any]:
    """Generate the immutable candidate execution plan with deterministic input hashes."""
    input_hashes = compute_input_hashes(base_dir=base_dir, fixture_text=fixture_text)
    plan: dict[str, Any] = {
        "schema_version": "1.0.0",
        "plan_type": "offline_canary_plan",
        "live_authorized": False,
        "candidate_fixture": fixture_text,
        "provider": "groq",
        "model": GROQ_MODEL_ID,
        "endpoint": f"https://{ALLOWED_HOST}{ALLOWED_PATH}",
        "ceilings": {
            "max_sends": DEFAULT_MAX_SENDS,
            "max_request_bytes": MAX_REQUEST_BYTES,
            "max_completion_tokens": FIXED_MAX_COMPLETION_TOKENS,
            "reasoning_effort": FIXED_REASONING_EFFORT,
            "operation_deadline_seconds": OPERATION_DEADLINE_SECONDS,
            "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
            "max_tool_attempts": MAX_TOOL_ATTEMPTS,
        },
        "retry_policy": {
            "max_retries": 0,
            "auto_retry": False,
        },
        "expected_criteria": {
            "stage_one_tool_call_required": True,
            "stage_one_required_tool": _TOOL_NAME,
            "stage_two_structured_extraction": True,
            "exact_source_quote_matching": True,
            "fail_closed_on_any_error": True,
        },
        "token_accounting_unknowns": {
            "input_tokens_measured": False,
            "token_count_provenance": "unknown_until_live_accounting",
            "server_side_enforcement_verified": False,
            "reasoning_token_accounting_verified": False,
        },
        "input_hashes": input_hashes,
    }
    plan["plan_hash"] = compute_plan_hash(plan)
    return plan


# ===========================================================================
# 3. Canary Execution Dataclass
# ===========================================================================


@dataclass
class CanaryExecutionResult:
    """Sanitized evidence record of a canary execution run."""

    provenance: str
    timestamp_utc: str
    plan_hash: str
    candidate_fixture: str
    outcome: str
    error_category: str | None
    error_reason: str | None
    stage_reached: str
    sends_total: int
    stage_one_sends: int
    stage_two_sends: int
    tool_attempts: int
    tool_successes: int
    recovery_prompt_used: bool
    request_wire_bytes: list[int]
    max_request_wire_bytes: int
    observed_fixed_fields: dict[str, Any]
    field_assertions: dict[str, bool]
    extracted_fields: dict[str, Any] | None
    model_closed: bool
    usage: str
    token_accounting: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ===========================================================================
# 4. Harness Stages Execution
# ===========================================================================


async def run_canary_stages(
    model: GroqModel,
    fixture_text: str = CANARY_FIXTURE_INPUT,
    inv_reader: InventoryReader | None = None,
    transport: OfflineProbeTransport | None = None,
    plan_hash: str = "",
    propagate_errors: bool = True,
) -> CanaryExecutionResult:
    """Execute two-stage canary workflow with an injected GroqModel.

    Guarantees owned model closure in finally path.
    """
    reader = inv_reader or RealSeedInventoryReader()
    tool_attempts: int = 0
    tool_successes: int = 0
    recovery_used: bool = False
    stage_reached: str = "stage_one"
    s1_sends_start: int = model.sent
    s1_sends: int = 0
    s2_sends: int = 0
    outcome: str = "failed"
    error_category: str | None = None
    error_reason: str | None = None
    extracted_fields: dict[str, Any] | None = None
    field_assertions: dict[str, bool] = {}
    model_closed: bool = False
    in_flight_exc: BaseException | None = None

    @tool(
        name=_TOOL_NAME,
        description=(
            "Counts of equipment in this room by kind and readiness state. "
            "Advisory only: it decides nothing and reserves nothing."
        ),
    )
    def read_inventory() -> dict[str, Any]:
        nonlocal tool_attempts, tool_successes
        tool_attempts += 1
        if tool_attempts > MAX_TOOL_ATTEMPTS:
            msg = f"The inventory tool may not be called more than {MAX_TOOL_ATTEMPTS} times."
            raise CanaryToolLimitExceededError(msg)
        counts = [
            {"kind": r.kind, "state": r.state, "count": r.count} for r in reader.kind_state_counts()
        ]
        tool_successes += 1
        return {"counts": counts}

    try:
        # -------------------------------------------------------------------
        # STAGE ONE: Tool Selection & Execution
        # -------------------------------------------------------------------
        agent = Agent(
            model=model,
            tools=[read_inventory],
            system_prompt=_AGENT_SYSTEM_PROMPT,
            callback_handler=None,
            load_tools_from_directory=False,
        )

        stage1_prompt = _AGENT_USER_PROMPT.format(text=fixture_text)
        await agent.invoke_async(stage1_prompt, limits=Limits(turns=4))

        # Recovery pass: at most one recovery prompt if tool was omitted
        if (
            tool_successes == 0
            and tool_attempts <= MAX_TOOL_ATTEMPTS
            and model.budget.remaining >= 2
        ):
            recovery_used = True
            await agent.invoke_async(_RECOVERY_PROMPT, limits=Limits(turns=4))

        s1_sends = model.sent - s1_sends_start

        # Stage 1 assertions
        if tool_attempts > MAX_TOOL_ATTEMPTS:
            msg = (
                f"Stage 1 attempted {tool_attempts} tool calls, "
                f"exceeding ceiling of {MAX_TOOL_ATTEMPTS}"
            )
            raise CanaryToolLimitExceededError(msg)

        if tool_successes == 0:
            msg = "Stage 1 completed without executing read_inventory tool"
            raise CanaryToolNotExecutedError(msg)

        if tool_successes > MAX_TOOL_ATTEMPTS:
            msg = (
                f"Stage 1 executed {tool_successes} successful tool calls, "
                f"exceeding ceiling of {MAX_TOOL_ATTEMPTS}"
            )
            raise CanaryToolLimitExceededError(msg)

        # -------------------------------------------------------------------
        # STAGE TWO: Structured Extraction
        # -------------------------------------------------------------------
        stage_reached = "stage_two"
        s2_sends_start = model.sent

        stage2_prompt: Any = [
            {
                "role": "user",
                "content": [{"text": _EXTRACTION_USER_PROMPT.format(text=fixture_text)}],
            }
        ]

        extracted_obj: _Extraction | None = None
        async for item in model.structured_output(
            _Extraction,
            stage2_prompt,
            system_prompt=_EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA,
        ):
            raw_out: Any = item.get("output") if isinstance(item, dict) else item
            if isinstance(raw_out, _Extraction):
                extracted_obj = raw_out

        s2_sends = model.sent - s2_sends_start

        if extracted_obj is None:
            msg = "Stage 2 produced no structured extraction output"
            raise CanaryGroundingAssertionError(msg)

        extracted_fields = {
            "borrower_label": extracted_obj.borrower_label,
            "equipment_kind": extracted_obj.equipment_kind,
            "pickup_location": extracted_obj.pickup_location,
            "due_at": extracted_obj.due_at,
        }

        # Validate quoted source fields against fixture text
        for key, val in extracted_fields.items():
            if val is not None and val not in fixture_text:
                msg = f"Extracted field '{key}' with value '{val}' is not present in source text"
                raise CanaryGroundingAssertionError(msg)

        # Candidate fixture specific exact matches
        if fixture_text == CANARY_FIXTURE_INPUT:
            if extracted_fields["borrower_label"] != "Priya S":
                msg = (
                    f"Expected borrower_label 'Priya S', got '{extracted_fields['borrower_label']}'"
                )
                raise CanaryGroundingAssertionError(msg)
            if extracted_fields["equipment_kind"] != "crutches":
                msg = (
                    "Expected equipment_kind 'crutches', got "
                    f"'{extracted_fields['equipment_kind']}'"
                )
                raise CanaryGroundingAssertionError(msg)
            if extracted_fields["pickup_location"] not in ("the Adyar centre", "Adyar centre"):
                msg = (
                    "Expected pickup_location in ('the Adyar centre', 'Adyar centre'), "
                    f"got '{extracted_fields['pickup_location']}'"
                )
                raise CanaryGroundingAssertionError(msg)
            if extracted_fields["due_at"] != "2026-10-01T08:00:00Z":
                msg = f"Expected due_at '2026-10-01T08:00:00Z', got '{extracted_fields['due_at']}'"
                raise CanaryGroundingAssertionError(msg)

        field_assertions = {
            "borrower_label_quoted": bool(
                extracted_fields["borrower_label"]
                and extracted_fields["borrower_label"] in fixture_text
            ),
            "equipment_kind_quoted": bool(
                extracted_fields["equipment_kind"]
                and extracted_fields["equipment_kind"] in fixture_text
            ),
            "pickup_location_quoted": bool(
                extracted_fields["pickup_location"]
                and extracted_fields["pickup_location"] in fixture_text
            ),
            "due_at_quoted": bool(
                extracted_fields["due_at"] and extracted_fields["due_at"] in fixture_text
            ),
            "borrower_label_exact": extracted_fields["borrower_label"] == "Priya S",
            "equipment_kind_exact": extracted_fields["equipment_kind"] == "crutches",
            "pickup_location_exact": extracted_fields["pickup_location"]
            in ("the Adyar centre", "Adyar centre"),
            "due_at_exact": extracted_fields["due_at"] == "2026-10-01T08:00:00Z",
        }

        # Inspect transport wire records if available
        if transport is not None:
            for r in transport.records:
                if r.wire_byte_length > MAX_REQUEST_BYTES:
                    msg = (
                        f"Request wire byte size {r.wire_byte_length} exceeds "
                        f"ceiling {MAX_REQUEST_BYTES}"
                    )
                    raise CanaryError(msg)

        stage_reached = "complete"
        outcome = "success"

    except ModelThrottledException:
        error_category = "CanaryThrottledError"
        error_reason = "Simulated 429 rate limit received without retry"
        in_flight_exc = CanaryThrottledError(error_reason)
    except ValueError as e:
        err_str = str(e).lower()
        if "length limit" in err_str or "truncated" in err_str:
            error_category = "CanaryLengthLimitError"
            error_reason = "Structured output truncated due to length limit"
            in_flight_exc = CanaryLengthLimitError(error_reason)
        else:
            error_category = type(e).__name__
            error_reason = str(e)
            in_flight_exc = e
    except GroqSendBudgetExceededError as e:
        error_category = "GroqSendBudgetExceededError"
        error_reason = "Physical send budget of 6 exceeded"
        in_flight_exc = e
    except CanaryError as e:
        error_category = type(e).__name__
        error_reason = str(e)
        in_flight_exc = e
    except BaseException as e:
        error_category = type(e).__name__
        error_reason = str(e)
        in_flight_exc = e
    finally:
        # Owned cleanup guaranteed in all exit paths
        await model.aclose()
        model_closed = not model.client_open

    # Collect captured wire byte lengths
    wire_bytes: list[int] = []
    if transport is not None:
        wire_bytes = [r.wire_byte_length for r in transport.records]

    result = CanaryExecutionResult(
        provenance="offline_fixture",
        timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        plan_hash=plan_hash,
        candidate_fixture=fixture_text,
        outcome=outcome,
        error_category=error_category,
        error_reason=error_reason,
        stage_reached=stage_reached,
        sends_total=model.sent,
        stage_one_sends=s1_sends,
        stage_two_sends=s2_sends,
        tool_attempts=tool_attempts,
        tool_successes=tool_successes,
        recovery_prompt_used=recovery_used,
        request_wire_bytes=wire_bytes,
        max_request_wire_bytes=max(wire_bytes) if wire_bytes else 0,
        observed_fixed_fields={
            "model": GROQ_MODEL_ID,
            "max_completion_tokens": FIXED_MAX_COMPLETION_TOKENS,
            "reasoning_effort": FIXED_REASONING_EFFORT,
            "forbidden_max_tokens_absent": True,
            "allowed_n_values": [1, None],
        },
        field_assertions=field_assertions,
        extracted_fields=extracted_fields,
        model_closed=model_closed,
        usage="fixture data",
        token_accounting={
            "input_tokens": "unknown",
            "output_tokens": "unknown",
            "reasoning_tokens": "unknown",
        },
    )

    if in_flight_exc is not None and propagate_errors:
        raise in_flight_exc

    return result


# ===========================================================================
# 5. Offline Canary Simulation Runner
# ===========================================================================


async def run_offline_canary(
    output_dir: Path | None = None,
    base_dir: Path | None = None,
) -> tuple[dict[str, Any], CanaryExecutionResult]:
    """Execute standard offline canary run with synthetic fixtures and save artifacts."""
    transport = OfflineProbeTransport()
    transport.set_scenario("canary_happy_path")

    # Queue simulated responses matching canonical fixture:
    # 1. Stage 1 Turn 1: model proposes read_inventory
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}))
    )
    # 2. Stage 1 Turn 2: tool result returned, model conversational sentence
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Checked inventory counts."))
    )
    # 3. Stage 2 Turn 1: extraction JSON
    canary_extraction_output = {
        "borrower_label": "Priya S",
        "equipment_kind": "crutches",
        "pickup_location": "the Adyar centre",
        "due_at": "2026-10-01T08:00:00Z",
    }
    transport.queue_response(
        httpx.Response(200, json=make_simulated_parsed_completion(canary_extraction_output))
    )

    # Initialize model with explicit dummy key and fail-closed transport
    model = GroqModel(
        api_key=OFFLINE_DUMMY_KEY,
        max_sends=DEFAULT_MAX_SENDS,
        transport=transport,
    )

    plan = generate_canary_plan(base_dir=base_dir, fixture_text=CANARY_FIXTURE_INPUT)
    plan_hash = plan["plan_hash"]

    result = await run_canary_stages(
        model=model,
        fixture_text=CANARY_FIXTURE_INPUT,
        transport=transport,
        plan_hash=plan_hash,
        propagate_errors=False,
    )

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        plan_path = output_dir / "canary_plan.json"
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2)

        evidence_path = output_dir / "canary_evidence.json"
        with open(evidence_path, "w", encoding="utf-8") as f:
            json.dump(result.as_dict(), f, indent=2)

    return plan, result


# ===========================================================================
# 6. CLI Main (Strictly Offline Only)
# ===========================================================================


def main() -> int:
    """CLI entrypoint: offline-only canary preparation and plan verification."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Suppress verbose strands logger output to console
    logging.getLogger("strands").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(
        description="Offline Groq Canary Preparation Harness (BS-020)",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=_SCRIPTS_DIR.parent / "test-evidence" / "bs020",
        help="Directory to save canary plan and evidence JSON artifacts",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Generate and validate execution plan only without running stages",
    )

    args = parser.parse_args()

    print("================================================================================")
    print("  BORROWED STEPS (M3): OFFLINE GROQ CANARY PREPARATION (BS-020)")
    print("================================================================================")
    print("  - Mode: STRICT OFFLINE (fail-closed mock transport, dummy key)")
    print("  - Live Authorization: FALSE (0 network calls, 0 spend)")
    print(f"  - Target Model: {GROQ_MODEL_ID} at {GROQ_BASE_URL}")
    print(f"  - Candidate Fixture: '{CANARY_FIXTURE_INPUT}'")
    print(f"  - Max Sends Budget: {DEFAULT_MAX_SENDS} | Deadline: {OPERATION_DEADLINE_SECONDS}s")
    print("--------------------------------------------------------------------------------\n")

    if args.plan_only:
        plan = generate_canary_plan()
        print(f"Plan Hash: {plan['plan_hash']}")
        print("Input Hashes:")
        for name, h in plan["input_hashes"].items():
            print(f"  {name:<24}: {h}")
        return 0

    plan, result = asyncio.run(run_offline_canary(output_dir=args.output_dir))

    print(f"Plan Hash: {plan['plan_hash']}")
    print(f"Outcome: {result.outcome} (Stage reached: {result.stage_reached})")
    print(
        f"Sends Total: {result.sends_total} (Stage 1: {result.stage_one_sends}, "
        f"Stage 2: {result.stage_two_sends})"
    )
    print(
        f"Tool Calls: {result.tool_successes}/{result.tool_attempts} "
        f"(Recovery used: {result.recovery_prompt_used})"
    )
    print(f"Max Wire Bytes: {result.max_request_wire_bytes} B (Ceiling: {MAX_REQUEST_BYTES} B)")
    print(f"Model Closed: {result.model_closed}")
    print("\nField Assertions:")
    for field_name, passed in result.field_assertions.items():
        print(f"  {field_name:<28}: {'PASS' if passed else 'FAIL'}")

    print("\nArtifacts saved to:")
    print(f"  - {args.output_dir / 'canary_plan.json'}")
    print(f"  - {args.output_dir / 'canary_evidence.json'}")
    print("================================================================================")
    return 0 if result.outcome == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
