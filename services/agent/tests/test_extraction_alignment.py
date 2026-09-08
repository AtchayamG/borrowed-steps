"""Focused tests for BS-003-R8-AGY extraction alignment and smoke proof harness.

Offline tests only. Zero real inference and zero sockets.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import assistant_smoke  # noqa: E402

from borrowed_steps.application.grounding import (  # noqa: E402
    ABSENT_CANDIDATE,
    ACCEPTED,
    AMBIGUOUS_KINDS,
    EVIDENCE_NOT_IN_SOURCE,
    ground_due_at,
    ground_equipment_kind,
)
from borrowed_steps.domain.models import EquipmentKind  # noqa: E402
from borrowed_steps.infrastructure.strands_interpreter import (  # noqa: E402
    _EXTRACTION_SYSTEM_PROMPT,
    _Extraction,
)
from test_assistant_smoke import FakeHttpClient  # noqa: E402


@pytest.fixture
def auth_r8(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        assistant_smoke.AUTHORIZATION_ENV,
        assistant_smoke.REQUIRED_AUTHORIZATION,
    )


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeHttpClient:
    client = FakeHttpClient()
    monkeypatch.setattr(assistant_smoke, "Client", lambda *args, **kwargs: client)
    return client


def test_extraction_system_prompt_and_schema_alignment() -> None:
    """R13 asks for four original source quotes, with required nullable keys."""
    prompt = _EXTRACTION_SYSTEM_PROMPT

    assert "original equipment phrase, not an enum" in prompt
    assert "walking frame" in prompt

    assert "Every non-null value must be an exact quote" in prompt

    # 3. Multiple distinct kinds / zero kinds rule
    assert "zero or multiple distinct kinds" in prompt

    # 4. Date rule: aware timestamps verbatim, Z or explicit offset
    assert "timezone-aware" in prompt
    assert "explicit offset" in prompt

    # Every source field remains required and nullable.
    schema = _Extraction.model_json_schema()
    props = schema["properties"]
    required = set(schema.get("required", []))
    assert len(props) == 4
    assert required == set(props.keys())
    for field in props.values():
        assert {part["type"] for part in field["anyOf"]} == {"string", "null"}

    # Equipment is an original phrase; normalization happens after source checks.
    eq_desc = props["equipment_kind"]["description"]
    assert "phrase" in eq_desc.lower()


def test_grounding_with_uppercase_candidate_and_lowercase_evidence() -> None:
    """Grounding accepts uppercase enum candidate with exact lowercase source phrase evidence."""
    text = (
        "Meena R needs a wheelchair, pickup at the Velachery equipment room, "
        "return by 2026-09-15T12:00:00Z."
    )
    result = ground_equipment_kind(EquipmentKind.WHEELCHAIR, "wheelchair", text)
    assert result.reason == ACCEPTED
    assert result.value is EquipmentKind.WHEELCHAIR


def test_grounding_with_walking_frame_synonym() -> None:
    """Grounding accepts WALKER enum candidate for 'walking frame' source phrase."""
    text = "Leela needs a walking frame and will return it tomorrow."
    result = ground_equipment_kind(EquipmentKind.WALKER, "walking frame", text)
    assert result.reason == ACCEPTED
    assert result.value is EquipmentKind.WALKER


def test_grounding_distinct_kinds_ambiguity_rejects() -> None:
    """Grounding returns AMBIGUOUS_KINDS when two distinct kinds appear in source."""
    text = "Could be a wheelchair or crutches for Divya, at the Velachery room."
    result_none = ground_equipment_kind(None, None, text)
    assert result_none.reason == AMBIGUOUS_KINDS
    assert result_none.value is None

    result_wc = ground_equipment_kind(EquipmentKind.WHEELCHAIR, "wheelchair", text)
    assert result_wc.reason == AMBIGUOUS_KINDS
    assert result_wc.value is None


def test_grounding_omitted_model_candidate_remains_null() -> None:
    """When the model omits a candidate, grounding returns ABSENT_CANDIDATE."""
    text = "Meena R needs a wheelchair, pickup at the Velachery equipment room."
    result = ground_equipment_kind(None, "wheelchair", text)
    assert result.reason == ABSENT_CANDIDATE
    assert result.value is None


def test_grounding_offset_due_at_normalizes_to_utc() -> None:
    """Grounding accepts explicit offset ISO timestamp and normalizes to UTC."""
    now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
    offset_ts = "2026-09-16T17:30:00+05:30"
    text = f"Kavin needs crutches, pickup at the room, return by {offset_ts}."
    result = ground_due_at(offset_ts, offset_ts, text, now)
    assert result.reason == ACCEPTED
    assert isinstance(result.value, datetime)
    assert result.value.tzinfo == UTC
    assert result.value.hour == 12
    assert result.value.minute == 0


def test_smoke_r8_fails_closed_when_authorization_token_not_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_client: FakeHttpClient
) -> None:
    """Only exact BS-003-R8-AGY authorization is accepted."""
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    # Wrong token
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, "BS-003-R3")
    assert assistant_smoke.main("http://127.0.0.1:8000", {"1"}, ledger=ledger) == 1
    assert len(fake_client.calls) == 0

    # Generic token
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, "authorized-test-token")
    assert assistant_smoke.main("http://127.0.0.1:8000", {"1"}, ledger=ledger) == 1
    assert len(fake_client.calls) == 0


def test_smoke_r8_fails_closed_on_prior_failed_r8_attempt(
    tmp_path: Path,
    auth_r8: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any failed R8 attempt in the ledger closes the R8 allocation permanently."""
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", tmp_path / "evidence")

    # Record a failed R8 attempt (attempt 12)
    entries = [
        {"phase": "start", "attempt": 12, "task_id": "BS-003-R8-AGY", "case": "1-explicit-fields"},
        {
            "phase": "end",
            "attempt": 12,
            "task_id": "BS-003-R8-AGY",
            "verdict": "FAIL",
            "case_failures": ["1-explicit-fields: HTTP 502"],
            "http_status": 502,
        },
    ]
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    result = assistant_smoke.main("http://127.0.0.1:8000", {"1"}, ledger=ledger)
    assert result == 1
    assert len(fake_client.calls) == 0, "must refuse to send requests after R8 failure"


def test_smoke_r8_fails_closed_on_incomplete_r8_attempt(
    tmp_path: Path,
    auth_r8: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An aborted/incomplete R8 attempt in the ledger closes the R8 allocation permanently."""
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", tmp_path / "evidence")

    # Record a start with no end for R8 attempt 12
    entries = [
        {"phase": "start", "attempt": 12, "task_id": "BS-003-R8-AGY", "case": "1-explicit-fields"},
    ]
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    result = assistant_smoke.main("http://127.0.0.1:8000", {"1"}, ledger=ledger)
    assert result == 1
    assert len(fake_client.calls) == 0, "must refuse to send requests after R8 incomplete attempt"


def test_smoke_r8_preserves_historic_1_to_11_failures_without_rejection(
    tmp_path: Path,
    auth_r8: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historic attempts 1-11 failures (R2 502s, R3 missing kinds) do NOT trigger R8 closure."""
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", tmp_path / "evidence")

    # Historic ledger lines: legacy R2 attempts with 502s, R3 attempts 9, 10, 11
    entries: list[dict[str, Any]] = [
        {"http_status": 200, "label": "R2 probe 1"},
        {"http_status": 502, "label": "R2 probe 2"},  # 502 in R2
        {"phase": "start", "attempt": 9, "label": "r3-pass-1"},
        {"phase": "end", "attempt": 9, "label": "r3-pass-1", "http_status": 200},
        {"phase": "start", "attempt": 10, "label": "r3-pass-1"},
        {"phase": "end", "attempt": 10, "label": "r3-pass-1", "http_status": 200},
        {"phase": "start", "attempt": 11, "label": "r3-pass-1"},
        {"phase": "end", "attempt": 11, "label": "r3-pass-1", "http_status": 200},
    ]
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    errors = assistant_smoke._check_r8_history(ledger)
    assert errors == [], "historic attempts 1-11 must not be flagged as R8 failures"


def test_smoke_r8_cumulative_ceiling_22_and_budget_checks(
    tmp_path: Path,
    auth_r8: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cumulative ceiling is fixed at 22; exceeding it fails closed."""
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    # 1. Arbitrarily raised ceiling is rejected
    assert (
        assistant_smoke.main(
            "http://127.0.0.1:8000",
            {"1"},
            ledger=ledger,
            ceiling=23,
        )
        == 1
    )

    # 2. Cumulative budget check: 5 pre-ledger + 12 completed = 17 charged.
    # If 6 cases are wanted: 17 + 6 = 23 > 22 -> rejected
    lines = []
    for i in range(1, 12):
        lines.append(json.dumps({"phase": "start", "attempt": i}))
        lines.append(json.dumps({"phase": "end", "attempt": i}))
    # Attempt 12 is an R8 attempt with a structurally valid successful end so
    # _check_r8_history accepts it, allowing execution to reach the budget check.
    lines.append(
        json.dumps(
            {
                "phase": "start",
                "attempt": 12,
                "task_id": assistant_smoke.TASK_ID,
                "label": "r8-test",
            }
        )
    )
    lines.append(
        json.dumps(
            {
                "phase": "end",
                "attempt": 12,
                "task_id": assistant_smoke.TASK_ID,
                "label": "r8-test",
                "http_status": 200,
                "verdict": "PASS",
                "case_failures": [],
            }
        )
    )
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    capsys.readouterr()  # Clear previous output
    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1", "2", "3", "4", "5", "6"},
        ledger=ledger,
        ceiling=22,
    )
    assert result == 1
    assert len(fake_client.calls) == 0, "must reject when charged + wanted > 22"
    captured = capsys.readouterr()
    assert "taking the total to 23 past the ceiling of 22" in captured.out
    assert "The allowance is never restarted" in captured.out


def test_smoke_r8_full_six_cases_offline_fake(
    tmp_path: Path,
    auth_r8: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All six sequential cases pass end-to-end with fake HTTP responses."""
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", evidence_dir)
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)
    monkeypatch.setattr(assistant_smoke, "PRE_LEDGER_ATTEMPTS", 0)

    # Freeze dynamic timestamps in test fixture so separate calls cannot cross a minute
    cases = assistant_smoke._cases()
    monkeypatch.setattr(assistant_smoke, "_cases", lambda: cases)
    assert len(cases) == 6

    responses: list[tuple[int, dict[str, Any], dict[str, Any] | None]] = []
    for idx, case in enumerate(cases, start=1):
        expect = case["expect"]
        diag = {
            "correlation_id": f"c-r8-{idx}",
            "stage": assistant_smoke.TERMINAL_SUCCESS_STAGE,
            "outcome": "success",
            "reason": "ok",
            "sends": 2,
            "tool_attempts": 1,
            "tool_successes": 1,
            "recovery_used": False,
            "cleanup": "closed",
            "elapsed_ms": 1200,
        }
        resp = {
            "draft": {
                "borrower_label": expect["borrower_label"][0],
                "equipment_kind": expect["equipment_kind"][0],
                "pickup_location": expect["pickup_location"][0],
                "due_at": expect["due_at"][0],
            },
            "missing_fields": expect["missing_fields"],
            "provenance": {
                "framework": assistant_smoke.EXPECTED_FRAMEWORK,
                "provider": assistant_smoke.EXPECTED_PROVIDER,
                "model": assistant_smoke.EXPECTED_MODEL,
                "inventory_tool_calls": 1,
                "completed_at": "2026-09-08T18:00:00Z",
            },
        }
        responses.append((200, resp, diag))

    fake_client.interpret_responses = responses

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1", "2", "3", "4", "5", "6"},
        ledger=ledger,
        implementation_commit="abc123commit",
    )
    assert result == 0
    assert fake_client.post_interpret_count == 6

    # Verify evidence file
    evidence_file = evidence_dir / assistant_smoke.EVIDENCE_FILENAME
    assert evidence_file.exists()
    evidence_data = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert evidence_data["task_id"] == "BS-003-R8-AGY"
    assert evidence_data["implementation_commit"] == "abc123commit"
    assert evidence_data["verdict"] == "PASS"
    assert evidence_data["failures"] == []
    assert evidence_data["stopped_at_case"] is None
    assert evidence_data["real_inference_invocations"] == 6
    assert len(evidence_data["cases"]) == 6

    # Verify ledger entries
    ledger_lines = [
        json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(ledger_lines) == 12  # 6 starts, 6 ends
    for entry in ledger_lines:
        assert entry["task_id"] == "BS-003-R8-AGY"
        assert entry["implementation_commit"] == "abc123commit"
        if entry["phase"] == "end":
            assert entry["verdict"] == "PASS"
            assert entry["case_failures"] == []


def test_smoke_r8_fail_fast_aborts_on_case_failure(
    tmp_path: Path,
    auth_r8: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail fast on case 2 failure: stops immediately, cases 3-6 never sent."""
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", evidence_dir)
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)
    monkeypatch.setattr(assistant_smoke, "PRE_LEDGER_ATTEMPTS", 0)

    cases = assistant_smoke._cases()

    # Case 1 succeeds
    c1_expect = cases[0]["expect"]
    c1_resp = {
        "draft": {
            "borrower_label": c1_expect["borrower_label"][0],
            "equipment_kind": c1_expect["equipment_kind"][0],
            "pickup_location": c1_expect["pickup_location"][0],
            "due_at": c1_expect["due_at"][0],
        },
        "missing_fields": c1_expect["missing_fields"],
        "provenance": {
            "framework": assistant_smoke.EXPECTED_FRAMEWORK,
            "provider": assistant_smoke.EXPECTED_PROVIDER,
            "model": assistant_smoke.EXPECTED_MODEL,
            "inventory_tool_calls": 1,
            "completed_at": "2026-09-08T18:00:00Z",
        },
    }
    c1_diag = {
        "correlation_id": "c1",
        "stage": assistant_smoke.TERMINAL_SUCCESS_STAGE,
        "outcome": "success",
        "reason": "ok",
        "sends": 2,
        "tool_attempts": 1,
        "tool_successes": 1,
        "recovery_used": False,
        "cleanup": "closed",
        "elapsed_ms": 1000,
    }

    # Case 2 returns 502
    fake_client.interpret_responses = [
        (200, c1_resp, c1_diag),
        (502, {"error": "bad gateway"}, None),
    ]

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1", "2", "3", "4", "5", "6"},
        ledger=ledger,
        implementation_commit="failtestcommit",
    )
    assert result == 1
    assert fake_client.post_interpret_count == 2, "stops on case 2 failure; cases 3-6 never called"

    evidence_file = evidence_dir / assistant_smoke.EVIDENCE_FILENAME
    assert evidence_file.exists()
    evidence_data = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert evidence_data["verdict"] == "BLOCKED"
    assert evidence_data["stopped_at_case"] == "2-relative-date"
    assert evidence_data["real_inference_invocations"] == 2
    assert len(evidence_data["failures"]) > 0


def test_smoke_rejects_empty_case_selection_before_client_creation(
    tmp_path: Path,
    auth_r8: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty case selection is rejected with exit code 1 before any client/network calls."""
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        set(),
        ledger=ledger,
    )
    assert result == assistant_smoke.EXIT_FAIL
    assert len(fake_client.calls) == 0, "must make zero client calls on empty selection"
    captured = capsys.readouterr()
    assert "no cases selected" in captured.out


def test_smoke_rejects_unknown_case_selection_before_client_creation(
    tmp_path: Path,
    auth_r8: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown case selections are rejected before any client/network calls."""
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"7", "unknown"},
        ledger=ledger,
    )
    assert result == assistant_smoke.EXIT_FAIL
    assert len(fake_client.calls) == 0, "must make zero client calls on unknown selection"
    captured = capsys.readouterr()
    assert "unknown case(s) selected" in captured.out


def test_smoke_proper_subset_finishes_partial_nonzero(
    tmp_path: Path,
    auth_r8: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A passing proper subset must finish PARTIAL with nonzero exit code.

    It must never print full proof passed.
    """
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", evidence_dir)
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)
    monkeypatch.setattr(assistant_smoke, "PRE_LEDGER_ATTEMPTS", 0)

    cases = assistant_smoke._cases()
    monkeypatch.setattr(assistant_smoke, "_cases", lambda: cases)

    # Provide valid responses for cases 1 and 2
    responses: list[tuple[int, dict[str, Any], dict[str, Any] | None]] = []
    for idx in (1, 2):
        c = cases[idx - 1]
        expect = c["expect"]
        diag = {
            "correlation_id": f"c-partial-{idx}",
            "stage": assistant_smoke.TERMINAL_SUCCESS_STAGE,
            "outcome": "success",
            "reason": "ok",
            "sends": 2,
            "tool_attempts": 1,
            "tool_successes": 1,
            "recovery_used": False,
            "cleanup": "closed",
            "elapsed_ms": 1000,
        }
        resp = {
            "draft": {
                "borrower_label": expect["borrower_label"][0],
                "equipment_kind": expect["equipment_kind"][0],
                "pickup_location": expect["pickup_location"][0],
                "due_at": expect["due_at"][0],
            },
            "missing_fields": expect["missing_fields"],
            "provenance": {
                "framework": assistant_smoke.EXPECTED_FRAMEWORK,
                "provider": assistant_smoke.EXPECTED_PROVIDER,
                "model": assistant_smoke.EXPECTED_MODEL,
                "inventory_tool_calls": 1,
                "completed_at": "2026-09-08T18:00:00Z",
            },
        }
        responses.append((200, resp, diag))

    fake_client.interpret_responses = responses

    capsys.readouterr()
    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1", "2"},
        ledger=ledger,
        implementation_commit="partialtestcommit",
    )
    assert result == assistant_smoke.EXIT_PARTIAL  # exit code 2
    assert fake_client.post_interpret_count == 2

    # Verify evidence file has PARTIAL verdict, not PASS
    evidence_file = evidence_dir / assistant_smoke.EVIDENCE_FILENAME
    assert evidence_file.exists()
    evidence_data = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert evidence_data["verdict"] == "PARTIAL"
    assert evidence_data["stopped_at_case"] is None
    assert evidence_data["real_inference_invocations"] == 2
    assert len(evidence_data["failures"]) == 0

    captured = capsys.readouterr()
    assert "assistant live proof passed" not in captured.out
    assert "assistant partial run (2 of 6 cases completed)" in captured.out


def test_smoke_real_canonical_r8_ledger_refusal_sentinel_client(
    auth_r8: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real canonical R8 ledger refuses further runs before any Client is constructed."""

    class SentinelClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError(
                "SentinelClient must never be constructed when canonical R8 ledger is closed!"
            )

    monkeypatch.setattr(assistant_smoke, "Client", SentinelClient)

    # Use the real canonical ledger directly (read-only verification)
    canonical = assistant_smoke.CANONICAL_LEDGER
    assert canonical.is_file(), "canonical ledger must exist on disk"

    capsys.readouterr()
    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1", "2", "3", "4", "5", "6"},
        ledger=canonical,
    )
    assert result == assistant_smoke.EXIT_FAIL
    captured = capsys.readouterr()
    assert "The BS-003-R8-AGY allocation is permanently closed" in captured.out


def test_illustrative_grounding_replay_omitted_vs_rejected_candidates() -> None:
    """ILLUSTRATIVE ONLY - NOT EVIDENCE OF HISTORIC R8 CAUSE.

    Demonstrates that server-side grounding produces null draft fields through
    distinct pathways:
    1. Candidate omission by model (reason=absent_candidate);
    2. Evidence mismatch / rejection by grounding (reason=evidence_not_in_source);
    3. Ambiguous source kinds (reason=ambiguous_kinds).
    A post-grounding null draft alone cannot establish which pathway occurred
    without server-side grounding reason codes.
    """
    text = (
        "Meena R needs a wheelchair, pickup at the Velachery equipment room, "
        "return by 2026-09-15T12:00:00Z."
    )

    # Pathway 1: Model omits candidate (candidate is None)
    res_omitted = ground_equipment_kind(None, "wheelchair", text)
    assert res_omitted.value is None
    assert res_omitted.reason == ABSENT_CANDIDATE

    # Pathway 2: Model emits candidate with fabricated/unmatched evidence
    res_rejected = ground_equipment_kind(EquipmentKind.WHEELCHAIR, "hospital bed", text)
    assert res_rejected.value is None
    assert res_rejected.reason == EVIDENCE_NOT_IN_SOURCE

    # Both pathways yield post-grounding null, illustrating the ambiguity:
    assert res_omitted.value is None
    assert res_rejected.value is None
