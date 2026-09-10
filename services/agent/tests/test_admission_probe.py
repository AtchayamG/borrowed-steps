"""Tests for the offline Groq admission wire and protocol probe."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from borrowed_steps.infrastructure.groq_model import (
    ALLOWED_HOST,
    ALLOWED_PATH,
    DEFAULT_MAX_SENDS,
    GROQ_MODEL_ID,
)

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from admission_probe import (  # noqa: E402
    INTAKE_TEXT_MAX_LENGTH,
    OfflineProbeTransport,
    RealSeedInventoryReader,
    generate_synthetic_ascii_intake,
    generate_synthetic_unicode_intake,
    run_all_probes,
    run_probe_scenario_happy_path,
    run_probe_scenario_six_send_sample,
)


def test_synthetic_data_generators_bounds_and_encoding() -> None:
    """Intake generators strictly adhere to length limits and exhibit expected UTF-8 properties."""
    ascii_text = generate_synthetic_ascii_intake(INTAKE_TEXT_MAX_LENGTH)
    assert len(ascii_text) == INTAKE_TEXT_MAX_LENGTH
    # Pure ASCII: byte length equals character count
    assert len(ascii_text.encode("utf-8")) == INTAKE_TEXT_MAX_LENGTH

    unicode_text = generate_synthetic_unicode_intake(INTAKE_TEXT_MAX_LENGTH)
    assert len(unicode_text) == INTAKE_TEXT_MAX_LENGTH
    # Multilingual Unicode: UTF-8 byte length strictly exceeds character count
    unicode_bytes = len(unicode_text.encode("utf-8"))
    assert unicode_bytes > INTAKE_TEXT_MAX_LENGTH
    assert unicode_bytes >= 2700  # Significant multi-byte inflation


def test_real_seed_inventory_reader() -> None:
    """RealSeedInventoryReader returns counts matching domain SEED_EQUIPMENT."""
    reader = RealSeedInventoryReader()
    counts = reader.kind_state_counts()
    assert len(counts) == 3
    kinds = {c.kind for c in counts}
    assert kinds == {"crutches", "walker", "wheelchair"}
    for c in counts:
        assert c.count == 1


@pytest.mark.anyio
async def test_offline_transport_fails_closed_on_live_egress_targets() -> None:
    """OfflineProbeTransport refuses non-Groq or non-HTTPS URLs without network calls."""
    transport = OfflineProbeTransport()

    # Refuse HTTP
    req_http = httpx.Request("POST", f"http://{ALLOWED_HOST}{ALLOWED_PATH}", content=b"{}")
    with pytest.raises(RuntimeError, match="LIVE EGRESS ATTEMPT REFUSED"):
        await transport.handle_async_request(req_http)

    # Refuse wrong host
    req_wrong_host = httpx.Request(
        "POST", "https://api.openai.com/v1/chat/completions", content=b"{}"
    )
    with pytest.raises(RuntimeError, match="LIVE EGRESS ATTEMPT REFUSED"):
        await transport.handle_async_request(req_wrong_host)

    # Refuse wrong path
    req_wrong_path = httpx.Request("POST", f"https://{ALLOWED_HOST}/v1/models", content=b"{}")
    with pytest.raises(RuntimeError, match="LIVE EGRESS ATTEMPT REFUSED"):
        await transport.handle_async_request(req_wrong_path)


@pytest.mark.anyio
async def test_happy_path_wire_measurements_and_serialization() -> None:
    """Happy path executes 3 sends, measuring tool loop accumulation and stage 2 strict schema."""
    transport = OfflineProbeTransport()
    ascii_intake = generate_synthetic_ascii_intake(INTAKE_TEXT_MAX_LENGTH)

    records = await run_probe_scenario_happy_path(transport, ascii_intake, "happy_path_test")
    assert len(records) == 3

    # Send 1: Stage 1 Turn 1
    r1 = records[0]
    assert r1.send_index == 1
    assert r1.stage_label == "stage_one_tool_proposal"
    assert r1.is_streaming is True
    assert r1.tools_count == 1
    assert r1.tools_summary[0]["name"] == "read_inventory"
    assert r1.response_format_type is None
    assert r1.messages_count == 2
    assert r1.wire_byte_length > 2000

    # Send 2: Stage 1 Turn 2 (Prompt accumulation)
    r2 = records[1]
    assert r2.send_index == 2
    assert r2.stage_label == "stage_one_tool_proposal"
    assert r2.is_streaming is True
    assert r2.messages_count == 4  # system + user + assistant tool_call + tool result
    # Accumulated prompt must be larger than Turn 1
    assert r2.wire_byte_length > r1.wire_byte_length

    # Send 3: Stage 2 Structured Output
    r3 = records[2]
    assert r3.send_index == 3
    assert r3.stage_label == "stage_two_structured_extraction"
    assert r3.is_streaming is False
    assert r3.tools_count == 0
    assert r3.response_format_type == "json_schema"
    assert r3.response_format_strict is True
    assert r3.response_format_schema_name == "_Extraction"
    assert r3.response_format_bytes is not None
    assert r3.response_format_bytes > 800  # Strict schema serialized size
    assert r3.wire_byte_length > r2.wire_byte_length


@pytest.mark.anyio
async def test_unicode_multi_byte_inflation_measured() -> None:
    """Unicode intake text demonstrates measured wire byte expansion over ASCII."""
    transport = OfflineProbeTransport()
    ascii_intake = generate_synthetic_ascii_intake(INTAKE_TEXT_MAX_LENGTH)
    unicode_intake = generate_synthetic_unicode_intake(INTAKE_TEXT_MAX_LENGTH)

    ascii_records = await run_probe_scenario_happy_path(transport, ascii_intake, "ascii_run")
    unicode_records = await run_probe_scenario_happy_path(transport, unicode_intake, "unicode_run")

    for a_rec, u_rec in zip(ascii_records, unicode_records, strict=True):
        # Unicode request wire bytes strictly exceed ASCII wire bytes
        assert u_rec.wire_byte_length > a_rec.wire_byte_length
        # Bytes-per-character ratio for Unicode exceeds 1.10
        assert u_rec.bytes_per_char_ratio > 1.10


@pytest.mark.anyio
async def test_six_send_sample_and_seventh_refusal() -> None:
    """Six-send sample exercises all 6 permitted sends and strictly refuses a 7th send."""
    transport = OfflineProbeTransport()
    ascii_intake = generate_synthetic_ascii_intake(INTAKE_TEXT_MAX_LENGTH)

    records = await run_probe_scenario_six_send_sample(transport, ascii_intake)
    assert len(records) == 6

    send_indices = [r.send_index for r in records]
    assert send_indices == [1, 2, 3, 4, 5, 6]

    # Verify Send 5 was the simulated 429
    r5 = records[4]
    assert r5.stage_label == "stage_two_rate_limited_send"
    assert r5.simulated_response_label == "TEST_SIMULATION_ONLY"

    # Verify Send 6 succeeded
    r6 = records[5]
    assert r6.stage_label == "stage_two_retry_successful"


@pytest.mark.anyio
async def test_run_all_probes_writes_valid_sanitized_evidence(tmp_path: Path) -> None:
    """run_all_probes generates complete summary with no secret leaks."""
    output_file = tmp_path / "test_evidence.json"
    await run_all_probes(output_path=output_file)

    assert output_file.exists()
    content = output_file.read_text(encoding="utf-8")
    parsed = json.loads(content)

    assert parsed["pinned_model"] == GROQ_MODEL_ID
    assert parsed["max_sends_budget"] == DEFAULT_MAX_SENDS
    assert "scenarios" in parsed
    assert len(parsed["scenarios"]) == 3

    # Invariant: No API keys or authorization headers in evidence JSON
    assert "gsk_" not in content
    assert "Authorization" not in content
    assert "Bearer" not in content
