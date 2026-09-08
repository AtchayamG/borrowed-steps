"""Comprehensive offline tests for R10 diagnostic harness.

Verifies guards, ledger constraints, claim acquisition, logging verification,
input parsing and validation, safe log parsing, and offline end-to-end execution.
Uses only in-process fake HTTP/model transport and temporary directories; zero real
inference or network sockets.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
import sys
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Self

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import assistant_smoke  # noqa: E402
import r10_diagnostic  # noqa: E402

from borrowed_steps.application.grounding import FIELD_ORDER  # noqa: E402
from borrowed_steps.domain.errors import ValidationFailedError  # noqa: E402
from borrowed_steps.main import _configure_logging  # noqa: E402

CANONICAL_LEDGER = Path(__file__).resolve().parent.parent / "test-evidence" / "probe-ledger.jsonl"
CANONICAL_R8 = (
    Path(__file__).resolve().parent.parent / "test-evidence" / "assistant-live-proof-r8.json"
)


@pytest.fixture(autouse=True)
def frozen_diagnostic_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The historical input must not make offline tests expire during judging."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            fixed = cls(2026, 9, 8, 16, 0, tzinfo=UTC)
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(r10_diagnostic, "datetime", FrozenDateTime)


def test_retired_live_entry_points() -> None:
    assert r10_diagnostic.main() == 1
    for args in ([], ["server"], ["--ledger", "alternate.jsonl"]):
        result = subprocess.run(
            [sys.executable, str(_SCRIPTS_DIR / "r10_diagnostic.py"), *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode != 0
        assert "R10 is retired" in result.stderr


class FakeClient(assistant_smoke.Client):
    """In-process mock client avoiding network sockets."""

    def __init__(
        self,
        responses: dict[tuple[str, str], tuple[int, dict[str, Any]]] | None = None,
        diagnostic: dict[str, Any] | None = None,
        call_exception: Exception | None = None,
    ) -> None:
        super().__init__("http://127.0.0.1:8220")
        self.responses = responses or {}
        self.calls: list[tuple[str, str, Any]] = []
        self.last_diagnostic = diagnostic
        self.call_exception = call_exception

    def call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 180.0,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, path, payload))
        if self.call_exception and path == "/api/intake/interpret":
            raise self.call_exception
        if (method, path) in self.responses:
            return self.responses[(method, path)]
        return 404, {"error": "not found"}


def _valid_snapshot_payload() -> dict[str, Any]:
    return {
        "equipment": [{"id": "eq-1", "kind": "WHEELCHAIR", "status": "AVAILABLE"}],
        "requests": [],
        "loans": [],
        "events": [],
    }


def _setup_test_env(tmp_path: Path) -> dict[str, Path]:
    ledger = tmp_path / "probe-ledger.jsonl"
    raw_lines = CANONICAL_LEDGER.read_text(encoding="utf-8").splitlines()
    baseline_lines = [
        line for line in raw_lines if line.strip() and json.loads(line).get("attempt", 0) <= 12
    ]
    ledger.write_text("\n".join(baseline_lines) + "\n", encoding="utf-8")
    # Replay the history before R10, independent of later append-only live runs.
    assert assistant_smoke._consumed(ledger) == (17, 0, 13, [])

    r8 = tmp_path / "assistant-live-proof-r8.json"
    shutil.copyfile(CANONICAL_R8, r8)

    server_log = tmp_path / "r10-server.log"
    server_log.write_text(
        f"{r10_diagnostic.LOGGING_READINESS_MARKER}\n",
        encoding="utf-8",
    )

    claim = tmp_path / "r10-task.claim"
    evidence_dir = tmp_path / "evidence"

    return {
        "ledger": ledger,
        "r8": r8,
        "server_log": server_log,
        "claim": claim,
        "evidence_dir": evidence_dir,
    }


def test_authorization_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _setup_test_env(tmp_path)

    # Missing authorization
    monkeypatch.delenv(assistant_smoke.AUTHORIZATION_ENV, raising=False)
    ret = r10_diagnostic.main(
        server_log=env["server_log"],
        ledger=env["ledger"],
        claim_file=env["claim"],
        evidence_dir=env["evidence_dir"],
        r8_artifact=env["r8"],
        check_branch=False,
        check_src=False,
    )
    assert ret == 1
    assert not env["claim"].exists()

    # Wrong authorization
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, "BS-003-R8-AGY")
    ret = r10_diagnostic.main(
        server_log=env["server_log"],
        ledger=env["ledger"],
        claim_file=env["claim"],
        evidence_dir=env["evidence_dir"],
        r8_artifact=env["r8"],
        check_branch=False,
        check_src=False,
    )
    assert ret == 1
    assert not env["claim"].exists()


def test_base_url_loopback_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _setup_test_env(tmp_path)
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, r10_diagnostic.REQUIRED_AUTHORIZATION)

    ret = r10_diagnostic.main(
        base_url="http://external-host.com:8220",
        server_log=env["server_log"],
        ledger=env["ledger"],
        claim_file=env["claim"],
        evidence_dir=env["evidence_dir"],
        r8_artifact=env["r8"],
        check_branch=False,
        check_src=False,
    )
    assert ret == 1
    assert not env["claim"].exists()


def test_ledger_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _setup_test_env(tmp_path)
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, r10_diagnostic.REQUIRED_AUTHORIZATION)

    # Missing ledger
    missing_ledger = tmp_path / "nonexistent.jsonl"
    assert (
        r10_diagnostic.main(
            ledger=missing_ledger,
            server_log=env["server_log"],
            claim_file=env["claim"],
            evidence_dir=env["evidence_dir"],
            r8_artifact=env["r8"],
            check_branch=False,
            check_src=False,
        )
        == 1
    )

    # Changed ledger history: fewer attempts
    truncated_ledger = tmp_path / "truncated.jsonl"
    lines = env["ledger"].read_text(encoding="utf-8").splitlines()
    truncated_ledger.write_text("\n".join(lines[:-4]) + "\n", encoding="utf-8")
    assert (
        r10_diagnostic.main(
            ledger=truncated_ledger,
            server_log=env["server_log"],
            claim_file=env["claim"],
            evidence_dir=env["evidence_dir"],
            r8_artifact=env["r8"],
            check_branch=False,
            check_src=False,
        )
        == 1
    )

    # Ledger with prior R10 attempt
    r10_ledger = tmp_path / "r10_present.jsonl"
    r10_lines = [
        *lines,
        json.dumps(
            {
                "phase": "start",
                "attempt": 13,
                "task_id": "BS-003-R10-AGY",
                "label": "prior-attempt",
            }
        ),
    ]
    r10_ledger.write_text("\n".join(r10_lines) + "\n", encoding="utf-8")
    assert (
        r10_diagnostic.main(
            ledger=r10_ledger,
            server_log=env["server_log"],
            claim_file=env["claim"],
            evidence_dir=env["evidence_dir"],
            r8_artifact=env["r8"],
            check_branch=False,
            check_src=False,
        )
        == 1
    )


def test_claim_file_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _setup_test_env(tmp_path)
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, r10_diagnostic.REQUIRED_AUTHORIZATION)

    # Pre-existing claim file
    env["claim"].write_text("pre-existing claim", encoding="utf-8")
    assert (
        r10_diagnostic.main(
            ledger=env["ledger"],
            server_log=env["server_log"],
            claim_file=env["claim"],
            evidence_dir=env["evidence_dir"],
            r8_artifact=env["r8"],
            check_branch=False,
            check_src=False,
        )
        == 1
    )

    # acquire_claim raises ClaimRefusedError if file exists
    with pytest.raises(r10_diagnostic.ClaimRefusedError):
        r10_diagnostic.acquire_claim(env["claim"], {"task_id": "test"})


def test_server_logging_readiness_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _setup_test_env(tmp_path)
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, r10_diagnostic.REQUIRED_AUTHORIZATION)

    # Server log file missing
    missing_log = tmp_path / "missing_server.log"
    assert (
        r10_diagnostic.main(
            server_log=missing_log,
            ledger=env["ledger"],
            claim_file=env["claim"],
            evidence_dir=env["evidence_dir"],
            r8_artifact=env["r8"],
            check_branch=False,
            check_src=False,
        )
        == 1
    )

    # Server log file without marker
    empty_log = tmp_path / "empty_server.log"
    empty_log.write_text("some random server line\n", encoding="utf-8")
    assert (
        r10_diagnostic.main(
            server_log=empty_log,
            ledger=env["ledger"],
            claim_file=env["claim"],
            evidence_dir=env["evidence_dir"],
            r8_artifact=env["r8"],
            check_branch=False,
            check_src=False,
        )
        == 1
    )


def test_logging_configuration_real_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline test verifying real logging configuration produces the marker without inference."""
    monkeypatch.setenv("BS_LOG_LEVEL", "INFO")
    _configure_logging()

    logger = logging.getLogger("borrowed_steps.assistant")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(r10_diagnostic.LOGGING_READINESS_MARKER)
        output = stream.getvalue()
        assert r10_diagnostic.LOGGING_READINESS_MARKER in output
        assert "borrowed_steps.assistant" in output
    finally:
        logger.removeHandler(handler)


def test_r8_input_validation(tmp_path: Path) -> None:
    # Valid artifact
    text, ts = r10_diagnostic.load_and_validate_r8_input(CANONICAL_R8)
    assert "Meena R needs a wheelchair" in text
    assert ts == "2026-09-15T12:38:37Z"

    # Expired timestamp (in the past relative to now)
    future_sim_now = datetime(2026, 9, 20, 0, 0, 0, tzinfo=UTC)
    with pytest.raises(ValidationFailedError, match="due_at must be in the future"):
        r10_diagnostic.load_and_validate_r8_input(CANONICAL_R8, now_dt=future_sim_now)

    # Beyond 30 days
    past_sim_now = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
    with pytest.raises(ValidationFailedError, match="due_at must be within 30 days"):
        r10_diagnostic.load_and_validate_r8_input(CANONICAL_R8, now_dt=past_sim_now)

    # Malformed artifact
    bad_artifact = tmp_path / "bad.json"
    bad_artifact.write_text(json.dumps({"cases": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="contains no cases"):
        r10_diagnostic.load_and_validate_r8_input(bad_artifact)


def test_invalid_prerequisites_zero_interpret_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _setup_test_env(tmp_path)
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, r10_diagnostic.REQUIRED_AUTHORIZATION)

    # Health check fails
    fake_client = FakeClient(
        responses={
            ("GET", "/api/health"): (500, {"error": "down"}),
        }
    )
    ret = r10_diagnostic.main(
        server_log=env["server_log"],
        ledger=env["ledger"],
        claim_file=env["claim"],
        evidence_dir=env["evidence_dir"],
        r8_artifact=env["r8"],
        check_branch=False,
        check_src=False,
        client=fake_client,
        implementation_commit="test-commit",
    )
    assert ret == 1
    assert not env["claim"].exists()
    assert not any(call[1] == "/api/intake/interpret" for call in fake_client.calls)

    # Snapshot check fails
    fake_client_bad_snap = FakeClient(
        responses={
            ("GET", "/api/health"): (200, {"status": "ok", "agent_mode": "strands_ollama"}),
            ("POST", "/api/workspaces"): (201, {"id": "ws-1"}),
            ("GET", "/api/snapshot"): (500, {"error": "db error"}),
        }
    )
    ret2 = r10_diagnostic.main(
        server_log=env["server_log"],
        ledger=env["ledger"],
        claim_file=env["claim"],
        evidence_dir=env["evidence_dir"],
        r8_artifact=env["r8"],
        check_branch=False,
        check_src=False,
        client=fake_client_bad_snap,
        implementation_commit="test-commit",
    )
    assert ret2 == 1
    assert not env["claim"].exists()
    assert not any(call[1] == "/api/intake/interpret" for call in fake_client_bad_snap.calls)


def test_successful_diagnostic_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _setup_test_env(tmp_path)
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, r10_diagnostic.REQUIRED_AUTHORIZATION)

    corr_id = "test-corr-12345"
    diagnostic = {
        "correlation_id": corr_id,
        "sends": 1,
        "tool_attempts": 0,
        "tool_successes": 0,
        "outcome": "success",
        "stage": "extraction",
        "reason": "drafted",
        "recovery_used": False,
        "cleanup": "closed",
        "elapsed_ms": 120,
    }

    interpret_resp = {
        "draft": {
            "borrower_label": "Meena R",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "the Velachery equipment room",
            "due_at": "2026-09-15T12:38:37Z",
        },
        "missing_fields": [],
        "provenance": {
            "framework": "strands",
            "provider": "ollama",
            "model": "llama3.2:3b",
            "inventory_tool_calls": 0,
            "completed_at": "2026-09-08T17:00:00Z",
        },
    }

    def _sim_interpret() -> None:
        with open(env["server_log"], "a", encoding="utf-8") as f:
            f.write(
                "INFO:     borrowed_steps.assistant Assistant grounding: "
                "borrower_label=accepted, equipment_kind=accepted, "
                "pickup_location=accepted, due_at=accepted | "
                "model_requests=1 tool_calls=0\n"
            )
            f.write(
                f"INFO:     borrowed_steps.assistant Assistant {corr_id} success "
                "at stage=extraction reason=drafted | "
                "sends=1 tool_attempts=0 tool_successes=0 recovery=False "
                "elapsed_ms=120 cleanup=closed\n"
            )

    class SimulatingClient(FakeClient):
        def call(
            self,
            method: str,
            path: str,
            payload: dict[str, Any] | None = None,
            timeout: float = 180.0,
        ) -> tuple[int, dict[str, Any]]:
            if path == "/api/intake/interpret":
                _sim_interpret()
            return super().call(method, path, payload, timeout)

    client = SimulatingClient(
        responses={
            ("GET", "/api/health"): (200, {"status": "ok", "agent_mode": "strands_ollama"}),
            ("POST", "/api/workspaces"): (201, {"id": "ws-1"}),
            ("GET", "/api/snapshot"): (200, _valid_snapshot_payload()),
            ("POST", "/api/intake/interpret"): (200, interpret_resp),
        },
        diagnostic=diagnostic,
    )

    ret = r10_diagnostic.main(
        server_log=env["server_log"],
        ledger=env["ledger"],
        claim_file=env["claim"],
        evidence_dir=env["evidence_dir"],
        r8_artifact=env["r8"],
        check_branch=False,
        check_src=False,
        client=client,
        implementation_commit="commit-12345",
    )

    assert ret == 0
    assert env["claim"].exists()

    # Verify ledger entries
    charged, incomplete, next_att, errors = assistant_smoke._consumed(env["ledger"])
    assert not errors
    assert charged == 18
    assert incomplete == 0
    assert next_att == 14

    lines = [json.loads(line) for line in env["ledger"].read_text(encoding="utf-8").splitlines()]
    start_rec = lines[-2]
    end_rec = lines[-1]

    assert start_rec["phase"] == "start"
    assert start_rec["attempt"] == 13
    assert start_rec["task_id"] == "BS-003-R10-AGY"
    assert start_rec["implementation_commit"] == "commit-12345"

    assert end_rec["phase"] == "end"
    assert end_rec["attempt"] == 13
    assert end_rec["task_id"] == "BS-003-R10-AGY"
    assert end_rec["diagnostic_verdict"] == "DIAGNOSTIC_COMPLETE"
    assert end_rec["functional_verdict"] == "FIELDS_CORRECT"
    assert end_rec["reason_codes"] == {
        "borrower_label": "accepted",
        "equipment_kind": "accepted",
        "pickup_location": "accepted",
        "due_at": "accepted",
    }

    # Verify artifacts
    diag_json = env["evidence_dir"] / "r10-diagnostic.json"
    notes_md = env["evidence_dir"] / "r10-notes.md"
    assert diag_json.is_file()
    assert notes_md.is_file()

    diag_data = json.loads(diag_json.read_text(encoding="utf-8"))
    assert diag_data["diagnostic_verdict"] == "DIAGNOSTIC_COMPLETE"
    assert diag_data["accounting"]["cumulative_attempts_charged"] == 18
    assert diag_data["accounting"]["remaining_allowance"] == 0

    notes_text = notes_md.read_text(encoding="utf-8")
    assert "DIAGNOSTIC_COMPLETE" in notes_text
    assert "UNKNOWN" in notes_text  # Historic qualification


def test_one_attempt_recorded_even_on_call_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _setup_test_env(tmp_path)
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, r10_diagnostic.REQUIRED_AUTHORIZATION)

    client = FakeClient(
        responses={
            ("GET", "/api/health"): (200, {"status": "ok", "agent_mode": "strands_ollama"}),
            ("POST", "/api/workspaces"): (201, {"id": "ws-1"}),
            ("GET", "/api/snapshot"): (200, _valid_snapshot_payload()),
        },
        call_exception=TimeoutError("Connection timed out after 180s"),
    )

    ret = r10_diagnostic.main(
        server_log=env["server_log"],
        ledger=env["ledger"],
        claim_file=env["claim"],
        evidence_dir=env["evidence_dir"],
        r8_artifact=env["r8"],
        check_branch=False,
        check_src=False,
        client=client,
        implementation_commit="commit-timeout",
    )

    assert ret == 1
    assert env["claim"].exists()

    # Attempt 13 is charged and recorded in ledger
    charged, _incomplete, next_att, errors = assistant_smoke._consumed(env["ledger"])
    assert not errors
    assert charged == 18
    assert next_att == 14

    lines = [json.loads(line) for line in env["ledger"].read_text(encoding="utf-8").splitlines()]
    start_rec = lines[-2]
    end_rec = lines[-1]
    assert start_rec["phase"] == "start"
    assert end_rec["phase"] == "end"
    assert end_rec["diagnostic_verdict"] == "TERMINAL_FAILURE"
    assert end_rec["reason_codes"] == dict.fromkeys(FIELD_ORDER, "NOT_REACHED")


def test_parse_diagnostic_logs_validation() -> None:
    # Valid lines
    log_content = (
        "INFO:     borrowed_steps.assistant Assistant grounding: "
        "borrower_label=accepted, equipment_kind=accepted, "
        "pickup_location=accepted, due_at=accepted | "
        "model_requests=1 tool_calls=0\n"
        "INFO:     borrowed_steps.assistant Assistant c123 success "
        "at stage=extraction reason=drafted | "
        "sends=1 tool_attempts=0 tool_successes=0 recovery=False "
        "elapsed_ms=90 cleanup=closed\n"
    )
    reasons, term, excerpts = r10_diagnostic.parse_diagnostic_logs(
        log_content, expected_correlation_id="c123", expected_sends=1, expected_tool_successes=0
    )
    assert reasons["borrower_label"] == "accepted"
    assert term["corr_id"] == "c123"
    assert len(excerpts) == 2

    # Missing grounding line
    with pytest.raises(ValueError, match="No grounding log line found"):
        r10_diagnostic.parse_diagnostic_logs("just some logs\n", expected_correlation_id="c123")

    # Ambiguous multiple grounding lines
    with pytest.raises(ValueError, match=r"Multiple .* grounding log lines found"):
        r10_diagnostic.parse_diagnostic_logs(
            log_content + log_content, expected_correlation_id="c123"
        )

    # Unknown field name
    bad_field_log = (
        "INFO:     Assistant grounding: unknown_field=accepted, equipment_kind=accepted, "
        "pickup_location=accepted, due_at=accepted | model_requests=1 tool_calls=0\n"
    )
    with pytest.raises(ValueError, match="not in FIELD_ORDER"):
        r10_diagnostic.parse_diagnostic_logs(bad_field_log, expected_correlation_id="c123")

    # Unknown reason code
    bad_reason_log = (
        "INFO:     Assistant grounding: borrower_label=bad_reason, equipment_kind=accepted, "
        "pickup_location=accepted, due_at=accepted | model_requests=1 tool_calls=0\n"
    )
    with pytest.raises(ValueError, match="not in REASONS"):
        r10_diagnostic.parse_diagnostic_logs(bad_reason_log, expected_correlation_id="c123")

    # Uncorrelated terminal line
    with pytest.raises(ValueError, match="No terminal log line found matching correlation_id"):
        r10_diagnostic.parse_diagnostic_logs(log_content, expected_correlation_id="other_corr_id")


def test_separate_diagnostic_vs_functional_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that when grounding reasons are extracted but fields are incorrect,

    diagnostic_verdict is DIAGNOSTIC_COMPLETE while functional_verdict is FIELDS_INCORRECT.
    """
    env = _setup_test_env(tmp_path)
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, r10_diagnostic.REQUIRED_AUTHORIZATION)

    corr_id = "test-corr-incomplete-draft"
    diagnostic = {
        "correlation_id": corr_id,
        "sends": 1,
        "tool_attempts": 0,
        "tool_successes": 0,
        "outcome": "success",
        "stage": "extraction",
        "reason": "drafted",
        "recovery_used": False,
        "cleanup": "closed",
        "elapsed_ms": 150,
    }

    # Model omitted borrower_label
    interpret_resp = {
        "draft": {
            "borrower_label": None,
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "the Velachery equipment room",
            "due_at": "2026-09-15T12:38:37Z",
        },
        "missing_fields": ["borrower_label"],
    }

    def _sim_interpret() -> None:
        with open(env["server_log"], "a", encoding="utf-8") as f:
            f.write(
                "INFO:     borrowed_steps.assistant Assistant grounding: "
                "borrower_label=absent_candidate, equipment_kind=accepted, "
                "pickup_location=accepted, due_at=accepted | "
                "model_requests=1 tool_calls=0\n"
            )
            f.write(
                f"INFO:     borrowed_steps.assistant Assistant {corr_id} success "
                "at stage=extraction reason=drafted | "
                "sends=1 tool_attempts=0 tool_successes=0 recovery=False "
                "elapsed_ms=150 cleanup=closed\n"
            )

    class SimulatingClient(FakeClient):
        def call(
            self,
            method: str,
            path: str,
            payload: dict[str, Any] | None = None,
            timeout: float = 180.0,
        ) -> tuple[int, dict[str, Any]]:
            if path == "/api/intake/interpret":
                _sim_interpret()
            return super().call(method, path, payload, timeout)

    client = SimulatingClient(
        responses={
            ("GET", "/api/health"): (200, {"status": "ok", "agent_mode": "strands_ollama"}),
            ("POST", "/api/workspaces"): (201, {"id": "ws-1"}),
            ("GET", "/api/snapshot"): (200, _valid_snapshot_payload()),
            ("POST", "/api/intake/interpret"): (200, interpret_resp),
        },
        diagnostic=diagnostic,
    )

    ret = r10_diagnostic.main(
        server_log=env["server_log"],
        ledger=env["ledger"],
        claim_file=env["claim"],
        evidence_dir=env["evidence_dir"],
        r8_artifact=env["r8"],
        check_branch=False,
        check_src=False,
        client=client,
        implementation_commit="commit-imperfect",
    )

    # Diagnostic succeeded in capturing reason codes, so exits 0
    assert ret == 0
    lines = [json.loads(line) for line in env["ledger"].read_text(encoding="utf-8").splitlines()]
    end_rec = lines[-1]
    assert end_rec["diagnostic_verdict"] == "DIAGNOSTIC_COMPLETE"
    assert end_rec["functional_verdict"] == "FIELDS_INCORRECT"
    assert end_rec["reason_codes"]["borrower_label"] == "absent_candidate"
