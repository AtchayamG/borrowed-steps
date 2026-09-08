"""A case whose pre-snapshot failed must not reach the model or the ledger.

BS-003-R5 fetched the pre-case snapshot, charged the attempt, sent the
interpretation, and only afterwards noticed the snapshot had failed. By then the
allowance was spent on a case that could never have proved anything: with no
trustworthy "before" there is nothing for the "after" to be compared against.

Local fakes only. Zero inference, zero sockets.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import assistant_smoke  # noqa: E402

from test_assistant_smoke import FakeHttpClient  # noqa: E402


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeHttpClient:
    client = FakeHttpClient()
    monkeypatch.setattr(assistant_smoke, "Client", lambda *args, **kwargs: client)
    return client


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary ledger and evidence directory.

    Both are redirected: ``main`` writes an evidence file on every exit path,
    and the real one under ``test-evidence`` is a historical artifact that no
    test may overwrite.
    """
    path = tmp_path / "ledger.jsonl"
    path.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", path)
    monkeypatch.setattr(assistant_smoke, "PRE_LEDGER_ATTEMPTS", 0)
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, assistant_smoke.REQUIRED_AUTHORIZATION)
    return path


def _records(ledger: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _starts(ledger: Path) -> list[dict[str, Any]]:
    return [record for record in _records(ledger) if record.get("phase") == "start"]


def test_a_failed_pre_snapshot_before_case_one_sends_nothing(
    fake_client: FakeHttpClient, ledger: Path
) -> None:
    """Nothing is charged and nothing is asked of the model."""
    # Calls: 1 = health has no snapshot; the first GET /api/snapshot is case one's.
    fake_client.snapshot_status_on_call = {1: 500}

    result = assistant_smoke.main("http://127.0.0.1:8000", {"1", "2", "3"}, ledger=ledger)

    assert result == 1
    assert fake_client.post_interpret_count == 0, "an interpretation was requested anyway"
    assert _starts(ledger) == [], "an attempt was charged for a case never attempted"


def test_a_failed_pre_snapshot_before_a_later_case_preserves_the_earlier_charge(
    fake_client: FakeHttpClient, ledger: Path
) -> None:
    """Case one still counts; case two is neither charged nor sent.

    Case one uses snapshots 1 (before) and 2 (after), so case two's pre-snapshot
    is the third.
    """
    fake_client.snapshot_status_on_call = {3: 503}

    result = assistant_smoke.main("http://127.0.0.1:8000", {"1", "2", "3"}, ledger=ledger)

    assert result == 1
    assert fake_client.post_interpret_count == 1, "case two reached the model"

    starts = _starts(ledger)
    assert len(starts) == 1, "case two was charged despite never being sent"
    assert starts[0]["case"] == "1-explicit-fields", "the earlier charge was not preserved"

    ends = [record for record in _records(ledger) if record.get("phase") == "end"]
    assert len(ends) == 1, "case one's completion record was lost"


def test_a_pre_snapshot_that_is_200_but_has_no_baseline_also_stops(
    fake_client: FakeHttpClient, ledger: Path
) -> None:
    """A 200 is not enough: a body with no collections is not a baseline."""
    fake_client.snapshot_invalid_on_call = {1}

    result = assistant_smoke.main("http://127.0.0.1:8000", {"1"}, ledger=ledger)

    assert result == 1
    assert fake_client.post_interpret_count == 0
    assert _starts(ledger) == []


def test_the_stop_is_recorded_in_the_evidence_file(
    fake_client: FakeHttpClient, ledger: Path, tmp_path: Path
) -> None:
    """A run that stopped early still says so, and says where."""
    fake_client.snapshot_status_on_call = {1: 500}

    assert assistant_smoke.main("http://127.0.0.1:8000", {"1"}, ledger=ledger) == 1

    evidence_file = tmp_path / "evidence" / assistant_smoke.EVIDENCE_FILENAME
    evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert evidence["stopped_at_case"] == "1-explicit-fields"
    assert evidence["real_inference_invocations"] == 0
    assert any("pre-case snapshot" in failure for failure in evidence["failures"])


def test_healthy_snapshots_still_let_a_passing_case_run(
    fake_client: FakeHttpClient, ledger: Path
) -> None:
    """The gate must not stand in the way of an ordinary run.

    The response is built from case one's own expectations, so the run passes
    for the right reason and the only thing under test is that the prerequisite
    check let it through.
    """
    case = assistant_smoke._cases()[0]
    expect = case["expect"]
    fake_client.interpret_responses = [
        (
            200,
            {
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
                    "completed_at": "2026-09-08T12:00:00Z",
                },
            },
            {
                "correlation_id": "prereq-ok-1",
                "stage": assistant_smoke.TERMINAL_SUCCESS_STAGE,
                "outcome": "success",
                "reason": "ok",
                "sends": 3,
                "tool_attempts": 1,
                "tool_successes": 1,
                "recovery_used": False,
                "cleanup": "closed",
                "elapsed_ms": 1000,
            },
        )
    ]

    result = assistant_smoke.main("http://127.0.0.1:8000", {"1"}, ledger=ledger)

    assert result == assistant_smoke.EXIT_PARTIAL, (
        "a healthy run was blocked by the prerequisite check"
    )
    assert fake_client.post_interpret_count == 1
    assert len(_starts(ledger)) == 1
