"""Offline reproducible request-size and protocol probe for Groq admission.

This script intercepts real Strands and OpenAI SDK serialization using synthetic
inputs and an offline, fail-closed transport. It captures wire shapes, byte lengths,
character counts, and message structures across all 6 permitted sends without making
any live network or provider calls.

Zero spend, zero live inference, fail-closed on any live transport attempt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from strands import Agent, tool
from strands.types.agent import Limits
from strands.types.exceptions import ModelThrottledException

from borrowed_steps.application.interpreter import InventoryCount, InventoryReader
from borrowed_steps.application.use_cases import SEED_EQUIPMENT
from borrowed_steps.infrastructure.groq_model import (
    ALLOWED_HOST,
    ALLOWED_PATH,
    DEFAULT_MAX_SENDS,
    GROQ_BASE_URL,
    GROQ_MODEL_ID,
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
from borrowed_steps.interfaces.http.schemas import INTAKE_TEXT_MAX_LENGTH

__all__ = [
    "INTAKE_TEXT_MAX_LENGTH",
    "OFFLINE_DUMMY_KEY",
    "OfflineProbeTransport",
    "RealSeedInventoryReader",
    "WireSendRecord",
    "generate_measurements_summary",
    "generate_synthetic_ascii_intake",
    "generate_synthetic_unicode_intake",
    "main",
    "make_simulated_parsed_completion",
    "make_simulated_sse_text",
    "make_simulated_sse_tool_call",
    "run_all_probes",
    "run_probe_scenario_happy_path",
    "run_probe_scenario_six_send_sample",
]

_LOGGER = logging.getLogger("admission_probe")

# Synthetic mock key: explicit offline placeholder
OFFLINE_DUMMY_KEY: str = "gsk_mock_offline_admission_probe_token_00000"


# ===========================================================================
# 1. Synthetic Data Generators
# ===========================================================================


def generate_synthetic_ascii_intake(target_length: int = INTAKE_TEXT_MAX_LENGTH) -> str:
    """Generate deterministic maximum-length English ASCII loan request."""
    base = (
        "Borrower Anand Kumar urgently requests 1 adult folding wheelchair and 1 pair of "
        "aluminum crutches for temporary home recovery following ankle surgery at St Johns "
        "Community Hospital. Pickup location: St Johns Outpatient Desk, North Wing Room 104. "
        "Return scheduled before 2026-10-15T16:30:00Z. Emergency contact volunteer phone is "
        "555-0199. Additional clinical notes: Patient requires adjustable leg rests and "
        "standard rubber tips on both crutches. Verification checked by community loan volunteer. "
    )
    repeat_count = (target_length // len(base)) + 1
    return (base * repeat_count)[:target_length]


def generate_synthetic_unicode_intake(target_length: int = INTAKE_TEXT_MAX_LENGTH) -> str:
    """Generate deterministic maximum-length multilingual Unicode intake text.

    Contains English, Tamil, Devanagari, German umlauts, French accents, and Unicode
    symbols/emojis to evaluate UTF-8 multi-byte byte length inflation against tokenization.
    """
    base = (
        "Borrower செல்வி பிரியா ராமன் (Priya Raman / प्रिया रमण) requests "
        "wheelchair ♿ & crutches 🩼 from Adyar Community Center (அடையாறு மையம்). "
        "Bedarf: Gehhilfe & Rollstuhl für Rehabilitation. "
        "Adresse de ramassage: 14 Rue de l'Hôpital Général, Salle d'accueil. "
        "Return deadline: 2026-10-20T09:00:00Z. Contact: info@borrowed-steps.org. "
        "Status: Vérifié / சரிபார்க்கப்பட்டது / सत्यापित. "
    )
    repeat_count = (target_length // len(base)) + 1
    return (base * repeat_count)[:target_length]


class RealSeedInventoryReader(InventoryReader):
    """InventoryReader returning real seed equipment counts from SEED_EQUIPMENT."""

    def kind_state_counts(self) -> list[InventoryCount]:
        tally = Counter(
            (kind.value.lower(), state.value.lower()) for _label, kind, state in SEED_EQUIPMENT
        )
        return [
            InventoryCount(kind=kind, state=state, count=count)
            for (kind, state), count in sorted(tally.items())
        ]


# ===========================================================================
# 2. Wire Serialization Interceptor Transport (Fail-Closed)
# ===========================================================================


@dataclass
class WireSendRecord:
    """Detailed record of one intercepted wire request."""

    send_index: int
    scenario_name: str
    stage_label: str
    http_method: str
    target_url: str
    wire_byte_length: int
    character_count: int
    bytes_per_char_ratio: float
    payload_keys: list[str]
    model_id: str
    is_streaming: bool
    stream_options: dict[str, Any] | None
    messages_count: int
    messages_summary: list[dict[str, Any]]
    tools_count: int
    tools_summary: list[dict[str, Any]]
    response_format_type: str | None
    response_format_strict: bool | None
    response_format_schema_name: str | None
    response_format_bytes: int | None
    simulated_response_type: str
    simulated_response_label: str = "TEST_SIMULATION_ONLY"


class OfflineProbeTransport(httpx.AsyncBaseTransport):
    """Fail-closed HTTP transport that intercepts requests and serves mock responses.

    Strictly forbids any socket creation or real network egress. Validates that all
    requests target the pinned Groq chat completion URL.
    """

    def __init__(self) -> None:
        self._records: list[WireSendRecord] = []
        self._scripted_responses: list[httpx.Response] = []
        self._current_send: int = 0
        self._active_scenario: str = "default"
        self._active_stage: str = "unknown"

    def set_scenario(self, scenario_name: str) -> None:
        self._active_scenario = scenario_name

    def set_stage(self, stage_label: str) -> None:
        self._active_stage = stage_label

    def queue_response(self, response: httpx.Response) -> None:
        self._scripted_responses.append(response)

    def clear_responses(self) -> None:
        self._scripted_responses.clear()

    @property
    def records(self) -> list[WireSendRecord]:
        return self._records

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Enforce target URL sanity (fail-closed)
        if (
            request.url.scheme != "https"
            or request.url.host != ALLOWED_HOST
            or request.url.path != ALLOWED_PATH
        ):
            msg = f"LIVE EGRESS ATTEMPT REFUSED: {request.url}"
            raise RuntimeError(msg)

        self._current_send += 1
        raw_content = request.content
        wire_bytes = len(raw_content)
        decoded_text = raw_content.decode("utf-8")
        char_count = len(decoded_text)
        ratio = round(wire_bytes / char_count, 4) if char_count > 0 else 1.0

        payload = json.loads(decoded_text)
        keys = sorted(payload.keys())
        model_id = payload.get("model", "")
        is_stream = payload.get("stream", False)
        stream_opts = payload.get("stream_options")

        # Parse messages
        messages = payload.get("messages", [])
        msg_summary = []
        for idx, m in enumerate(messages):
            role = m.get("role", "unknown")
            content = m.get("content")
            if isinstance(content, str):
                c_len = len(content)
                c_bytes = len(content.encode("utf-8"))
            elif isinstance(content, list):
                c_str = json.dumps(content)
                c_len = len(c_str)
                c_bytes = len(c_str.encode("utf-8"))
            else:
                c_len = 0
                c_bytes = 0
            msg_summary.append(
                {
                    "index": idx,
                    "role": role,
                    "content_chars": c_len,
                    "content_bytes": c_bytes,
                    "has_tool_calls": "tool_calls" in m,
                    "has_tool_call_id": "tool_call_id" in m,
                }
            )

        # Parse tools
        tools = payload.get("tools", [])
        tools_summary = []
        for t in tools:
            fn = t.get("function", {})
            name = fn.get("name", "")
            desc = fn.get("description", "")
            params = fn.get("parameters", {})
            param_str = json.dumps(params)
            tools_summary.append(
                {
                    "name": name,
                    "description_chars": len(desc),
                    "parameters_schema_bytes": len(param_str.encode("utf-8")),
                }
            )

        # Parse response_format
        rf = payload.get("response_format")
        rf_type = None
        rf_strict = None
        rf_schema_name = None
        rf_bytes = None
        if rf and isinstance(rf, dict):
            rf_type = rf.get("type")
            js = rf.get("json_schema", {})
            rf_strict = js.get("strict")
            rf_schema_name = js.get("name")
            rf_bytes = len(json.dumps(js).encode("utf-8"))

        if not self._scripted_responses:
            msg = f"No scripted response queued for send #{self._current_send}"
            raise RuntimeError(msg)

        resp = self._scripted_responses.pop(0)

        record = WireSendRecord(
            send_index=self._current_send,
            scenario_name=self._active_scenario,
            stage_label=self._active_stage,
            http_method=request.method,
            target_url=str(request.url),
            wire_byte_length=wire_bytes,
            character_count=char_count,
            bytes_per_char_ratio=ratio,
            payload_keys=keys,
            model_id=model_id,
            is_streaming=is_stream,
            stream_options=stream_opts,
            messages_count=len(messages),
            messages_summary=msg_summary,
            tools_count=len(tools),
            tools_summary=tools_summary,
            response_format_type=rf_type,
            response_format_strict=rf_strict,
            response_format_schema_name=rf_schema_name,
            response_format_bytes=rf_bytes,
            simulated_response_type="sse_stream" if is_stream else "parsed_json",
        )
        self._records.append(record)
        return resp


# ===========================================================================
# 3. Response Builders (Clearly Labeled Simulated Output)
# ===========================================================================


def make_simulated_sse_tool_call(
    tool_name: str, arguments: dict[str, Any], call_id: str = "call_inv_1"
) -> str:
    """Build simulated SSE chunks proposing a tool call (TEST_SIMULATION_ONLY)."""
    c1 = {
        "id": "chatcmpl-sim-tool-call",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": GROQ_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
                "finish_reason": None,
            }
        ],
    }
    c2 = {
        "id": "chatcmpl-sim-tool-call",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": GROQ_MODEL_ID,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    return f"data: {json.dumps(c1)}\n\ndata: {json.dumps(c2)}\n\ndata: [DONE]\n\n"


def make_simulated_sse_text(text: str) -> str:
    """Build simulated SSE chunks returning conversational text (TEST_SIMULATION_ONLY)."""
    chunk = {
        "id": "chatcmpl-sim-text",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": GROQ_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


def make_simulated_parsed_completion(extracted_data: dict[str, Any]) -> dict[str, Any]:
    """Build simulated parsed chat completion payload (TEST_SIMULATION_ONLY)."""
    return {
        "id": "chatcmpl-sim-parsed-json",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": GROQ_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(extracted_data),
                    "refusal": None,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 850,
            "completion_tokens": 45,
            "total_tokens": 895,
        },
    }


# ===========================================================================
# 4. Probe Simulation Orchestrators
# ===========================================================================


async def run_probe_scenario_happy_path(
    transport: OfflineProbeTransport,
    intake_text: str,
    scenario_label: str,
) -> list[WireSendRecord]:
    """Run standard 3-send happy path (Stage 1 tool proposal, tool result, Stage 2 extraction)."""
    transport.set_scenario(scenario_label)
    transport.clear_responses()

    # Send 1 (Stage 1 Turn 1): proposes read_inventory
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}))
    )
    # Send 2 (Stage 1 Turn 2): tool result returned; model gives sentence
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Checked inventory counts."))
    )
    # Send 3 (Stage 2 Turn 1): extraction parse
    extraction_output = {
        "borrower_label": "Anand Kumar",
        "equipment_kind": "wheelchair",
        "pickup_location": "St Johns Outpatient Desk",
        "due_at": "2026-10-15T16:30:00Z",
    }
    transport.queue_response(
        httpx.Response(200, json=make_simulated_parsed_completion(extraction_output))
    )

    model = GroqModel(api_key=OFFLINE_DUMMY_KEY, max_sends=DEFAULT_MAX_SENDS, transport=transport)
    inv_reader = RealSeedInventoryReader()

    @tool(
        name=_TOOL_NAME,
        description=(
            "Counts of equipment in this room by kind and readiness state. "
            "Advisory only: it decides nothing and reserves nothing."
        ),
    )
    def read_inventory() -> dict[str, Any]:
        counts = [
            {"kind": r.kind, "state": r.state, "count": r.count}
            for r in inv_reader.kind_state_counts()
        ]
        return {"counts": counts}

    agent = Agent(
        model=model,
        tools=[read_inventory],
        system_prompt=_AGENT_SYSTEM_PROMPT,
        load_tools_from_directory=False,
    )

    transport.set_stage("stage_one_tool_proposal")
    stage1_prompt = _AGENT_USER_PROMPT.format(text=intake_text)
    await agent.invoke_async(stage1_prompt, limits=Limits(turns=4))

    transport.set_stage("stage_two_structured_extraction")
    stage2_prompt: Any = [
        {"role": "user", "content": [{"text": _EXTRACTION_USER_PROMPT.format(text=intake_text)}]}
    ]
    async for _ in model.structured_output(
        _Extraction,
        stage2_prompt,
        system_prompt=_EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA,
    ):
        pass

    return [r for r in transport.records if r.scenario_name == scenario_label]


async def run_probe_scenario_six_send_sample(
    transport: OfflineProbeTransport,
    intake_text: str,
) -> list[WireSendRecord]:
    """Run one sampled multi-turn scenario utilizing all 6 permitted sends and 7th refusal."""
    scenario_label = "six_send_sample"
    transport.set_scenario(scenario_label)
    transport.clear_responses()

    # Send 1 (Stage 1 Turn 1): Model forgets tool and returns conversational reply
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Hello, how can I assist you?"))
    )
    # Send 2 (Stage 1 Turn 2, Recovery): Model prompted with recovery, proposes read_inventory
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}, call_id="c1"))
    )
    # Send 3 (Stage 1 Turn 3): Tool result returned, model proposes second check
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_tool_call("read_inventory", {}, call_id="c2"))
    )
    # Send 4 (Stage 1 Turn 4): Tool result returned, model concludes Stage 1
    transport.queue_response(
        httpx.Response(200, text=make_simulated_sse_text("Inventory verified."))
    )
    # Send 5 (Stage 2 Send 1): Extraction attempted, receives simulated 429
    transport.queue_response(
        httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"message": "Rate limit"}})
    )
    # Send 6 (Stage 2 Send 2): Retry extraction succeeds
    extraction_output = {
        "borrower_label": "Anand Kumar",
        "equipment_kind": "wheelchair",
        "pickup_location": "St Johns Outpatient Desk",
        "due_at": "2026-10-15T16:30:00Z",
    }
    transport.queue_response(
        httpx.Response(200, json=make_simulated_parsed_completion(extraction_output))
    )

    model = GroqModel(api_key=OFFLINE_DUMMY_KEY, max_sends=DEFAULT_MAX_SENDS, transport=transport)
    inv_reader = RealSeedInventoryReader()

    @tool(
        name=_TOOL_NAME,
        description=(
            "Counts of equipment in this room by kind and readiness state. "
            "Advisory only: it decides nothing and reserves nothing."
        ),
    )
    def read_inventory() -> dict[str, Any]:
        counts = [
            {"kind": r.kind, "state": r.state, "count": r.count}
            for r in inv_reader.kind_state_counts()
        ]
        return {"counts": counts}

    agent = Agent(
        model=model,
        tools=[read_inventory],
        system_prompt=_AGENT_SYSTEM_PROMPT,
        load_tools_from_directory=False,
    )

    # Stage 1: Send 1 (no tool)
    transport.set_stage("stage_one_no_tool_reply")
    await agent.invoke_async(_AGENT_USER_PROMPT.format(text=intake_text))

    # Stage 1: Recovery Send 2 + Send 3 + Send 4
    transport.set_stage("stage_one_recovery_and_tool_loop")
    await agent.invoke_async(_RECOVERY_PROMPT, limits=Limits(turns=4))

    # Stage 2: Send 5 (fails with rate limit)
    transport.set_stage("stage_two_rate_limited_send")
    stage2_prompt: Any = [
        {"role": "user", "content": [{"text": _EXTRACTION_USER_PROMPT.format(text=intake_text)}]}
    ]
    try:
        async for _ in model.structured_output(
            _Extraction,
            stage2_prompt,
            system_prompt=_EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA,
        ):
            pass
    except ModelThrottledException:
        # Expected simulated 429; unrelated errors must fail the probe.
        pass
    else:
        raise AssertionError("Simulated 429 did not raise the expected throttling error")

    # Stage 2: Send 6 (succeeds, exhausts budget to 6/6)
    transport.set_stage("stage_two_retry_successful")
    async for _ in model.structured_output(
        _Extraction,
        stage2_prompt,
        system_prompt=_EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA,
    ):
        pass

    # Verification: 7th send MUST be refused by sticky budget
    seventh_refused = False
    try:
        async for _ in model.structured_output(
            _Extraction,
            stage2_prompt,
            system_prompt=_EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA,
        ):
            pass
    except GroqSendBudgetExceededError:
        seventh_refused = True

    if not seventh_refused:
        msg = "Sticky 6-send ceiling failed to refuse 7th send!"
        raise RuntimeError(msg)

    return [r for r in transport.records if r.scenario_name == scenario_label]


# ===========================================================================
# 5. Report Aggregator & CLI Main
# ===========================================================================


def generate_measurements_summary(records: list[WireSendRecord]) -> dict[str, Any]:
    """Produce structured, sanitized summary of all wire probe measurements."""
    scenarios: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        scenarios.setdefault(r.scenario_name, []).append(asdict(r))

    summary = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target_endpoint": f"https://{ALLOWED_HOST}{ALLOWED_PATH}",
        "pinned_model": GROQ_MODEL_ID,
        "max_sends_budget": DEFAULT_MAX_SENDS,
        "evidence_scope": "Sample wire bytes only; no token or worst-case bound",
        "response_usage": "Synthetic fixture constants; not measured token usage",
        "admission_implementation_authorized": False,
        "max_intake_chars_tested": INTAKE_TEXT_MAX_LENGTH,
        "scenarios_evaluated": list(scenarios.keys()),
        "scenarios": scenarios,
        "sample_wire_analysis": {
            "max_single_send_bytes": max(r.wire_byte_length for r in records),
            "min_single_send_bytes": min(r.wire_byte_length for r in records),
            "max_single_send_chars": max(r.character_count for r in records),
            "stage_two_extraction_schema_bytes": next(
                (r.response_format_bytes for r in records if r.response_format_bytes), None
            ),
            "ascii_vs_unicode_delta": {
                "ascii_happy_path_stage1_bytes": [
                    r.wire_byte_length for r in records if r.scenario_name == "happy_path_ascii"
                ],
                "unicode_happy_path_stage1_bytes": [
                    r.wire_byte_length for r in records if r.scenario_name == "happy_path_unicode"
                ],
            },
        },
    }
    return summary


async def run_all_probes(output_path: Path | None = None) -> dict[str, Any]:
    """Execute all probe scenarios offline and optionally write JSON evidence."""
    transport = OfflineProbeTransport()

    ascii_intake = generate_synthetic_ascii_intake(INTAKE_TEXT_MAX_LENGTH)
    unicode_intake = generate_synthetic_unicode_intake(INTAKE_TEXT_MAX_LENGTH)

    _LOGGER.info("Running Probe Scenario 1: Happy Path (2000-char ASCII)...")
    await run_probe_scenario_happy_path(transport, ascii_intake, "happy_path_ascii")

    _LOGGER.info("Running Probe Scenario 2: Happy Path (2000-char Multilingual Unicode)...")
    await run_probe_scenario_happy_path(transport, unicode_intake, "happy_path_unicode")

    _LOGGER.info(
        "Running Probe Scenario 3: Six-Send Sample (Recovery, Tool Loop, 429, 7th Refusal)..."
    )
    await run_probe_scenario_six_send_sample(transport, ascii_intake)

    summary = generate_measurements_summary(transport.records)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        _LOGGER.info("Wrote sanitized probe measurements to: %s", output_path)

    return summary


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Offline Groq Admission Protocol & Wire Size Probe"
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("services/agent/test-evidence/bs015/probe_measurements.json"),
        help="Path to write sanitized JSON measurements",
    )
    args = parser.parse_args()

    print("================================================================================")
    print("  BORROWED STEPS (M3): GROQ ADMISSION OFFLINE WIRE PROBE (BS-015)")
    print("================================================================================")
    print("  - Mode: STRICT OFFLINE (fail-closed transport, 0 network calls, 0 spend)")
    print(f"  - Target Model: {GROQ_MODEL_ID} at {GROQ_BASE_URL}")
    print(f"  - Max Intake Characters: {INTAKE_TEXT_MAX_LENGTH}")
    print(f"  - Max Sends Enforced: {DEFAULT_MAX_SENDS}")
    print("--------------------------------------------------------------------------------\n")

    summary = asyncio.run(run_all_probes(output_path=args.output))

    print("\nPROBE SUMMARY TABLE:")
    print("--------------------------------------------------------------------------------")
    print(
        f"{'Scenario':<22} | {'Send':<4} | {'Stage Label':<28} | {'Wire Bytes':<10} | {'Ratio B/C'}"
    )
    print("--------------------------------------------------------------------------------")
    for scen_name, records in summary["scenarios"].items():
        for r in records:
            print(
                f"{scen_name:<22} | {r['send_index']:<4} | {r['stage_label']:<28} | "
                f"{r['wire_byte_length']:<10} | {r['bytes_per_char_ratio']:.3f}"
            )
    print("--------------------------------------------------------------------------------")
    bounds = summary["sample_wire_analysis"]
    print(f"Max Single-Send Wire Bytes: {bounds['max_single_send_bytes']} B")
    print(f"Min Single-Send Wire Bytes: {bounds['min_single_send_bytes']} B")
    print(f"Stage 2 Strict Schema Size: {bounds['stage_two_extraction_schema_bytes']} B")
    print(f"Sanitized evidence saved to: {args.output}")
    print("================================================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
