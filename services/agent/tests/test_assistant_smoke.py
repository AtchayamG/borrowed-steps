"""Offline regression tests for assistant_smoke.py fail-fast and safety controls.

Every test here uses local fakes only. Zero real inference and zero sockets.
"""

from __future__ import annotations

import email.message
import io
import json
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import assistant_smoke  # noqa: E402


class FakeResponse:
    """In-process HTTP response object for urllib opener."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes, url: str) -> None:
        self.status = status
        self.code = status
        self.msg = "OK"
        self.url = url
        self.headers = email.message.EmailMessage()
        for k, v in headers.items():
            self.headers[k] = v
        self._body = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        pass

    def info(self) -> email.message.EmailMessage:
        return self.headers

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class FakeOpenerHandler(urllib.request.BaseHandler):
    """In-process HTTP handler for the real assistant_smoke.Client."""

    def __init__(
        self,
        handler_fn: Callable[[urllib.request.Request], tuple[int, dict[str, str], bytes]],
    ) -> None:
        self.handler_fn = handler_fn

    def default_open(self, req: urllib.request.Request) -> FakeResponse:
        status, headers, body = self.handler_fn(req)
        return FakeResponse(status, headers, body, req.full_url)


class FakeHttpClient:
    """Mock client replacing assistant_smoke.Client for offline tests."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000") -> None:
        self.base_url = base_url
        self.calls: list[tuple[str, str, Any]] = []
        self.post_interpret_count = 0
        self.last_diagnostic: dict[str, Any] | None = None
        self.last_headers: dict[str, str] = {}
        # A complete snapshot body, as the real service returns. The proof
        # treats a body missing any of these as no baseline at all.
        self.snapshot_data: dict[str, Any] = {
            "equipment": [{"id": "1", "kind": "WHEELCHAIR"}],
            "requests": [],
            "loans": [],
            "events": [],
        }
        self.mutate_snapshot_on_interpret_count: int | None = None
        self.interpret_responses: list[tuple[int, dict[str, Any], dict[str, Any] | None]] = []
        self.snapshot_count = 0
        # Make the Nth GET /api/snapshot fail, or return a body with no baseline.
        self.snapshot_status_on_call: dict[int, int] = {}
        self.snapshot_invalid_on_call: set[int] = set()

    def call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 180.0,
    ) -> tuple[int, dict[str, Any]]:
        self.last_diagnostic = None
        self.last_headers = {}
        self.calls.append((method, path, payload))
        if path == "/api/health":
            return 200, {"agent_mode": "strands_ollama"}
        if path == "/api/workspaces":
            return 201, {"workspace_id": "ws-123"}
        if path == "/api/snapshot":
            self.snapshot_count += 1
            status = self.snapshot_status_on_call.get(self.snapshot_count, 200)
            if status != 200:
                return status, {"error": {"code": "INTERNAL_ERROR", "message": "unavailable"}}
            body = json.loads(json.dumps(self.snapshot_data))
            if self.snapshot_count in self.snapshot_invalid_on_call:
                body.pop("events", None)
            return 200, body
        if path == "/api/intake/interpret":
            self.post_interpret_count += 1
            if self.mutate_snapshot_on_interpret_count == self.post_interpret_count:
                self.snapshot_data["equipment"].append({"id": "mutated", "kind": "WALKER"})
            if self.interpret_responses:
                status, body, diag = self.interpret_responses.pop(0)
                self.last_diagnostic = diag
                return status, body
            # Default successful case 1 response
            self.last_diagnostic = {
                "correlation_id": f"test-corr-{self.post_interpret_count}",
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
            return 200, {
                "draft": {
                    "borrower_label": "Meena R",
                    "equipment_kind": "WHEELCHAIR",
                    "pickup_location": "the Velachery equipment room",
                    "due_at": "2026-09-15T12:00:37Z",
                },
                "missing_fields": [],
                "provenance": {
                    "framework": "strands",
                    "provider": "ollama",
                    "model": "llama3.2:3b",
                    "inventory_tool_calls": 1,
                    "completed_at": "2026-09-08T12:00:00Z",
                },
            }
        return 404, {"error": "not found"}


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeHttpClient:
    client = FakeHttpClient()
    monkeypatch.setattr(assistant_smoke, "Client", lambda *args, **kwargs: client)
    return client


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(assistant_smoke.AUTHORIZATION_ENV, assistant_smoke.REQUIRED_AUTHORIZATION)


def test_smoke_fails_closed_when_authorization_token_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_client: FakeHttpClient
) -> None:
    monkeypatch.delenv(assistant_smoke.AUTHORIZATION_ENV, raising=False)
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)
    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1"},
        ledger=ledger,
    )
    assert result == 1
    assert len(fake_client.calls) == 0, "no network calls sent when authorization is missing"


def test_smoke_fails_closed_when_ceiling_exceeds_approved_maximum(
    tmp_path: Path, auth_env: None, fake_client: FakeHttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)
    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1"},
        ledger=ledger,
        ceiling=25,  # exceeds CUMULATIVE_CEILING of 22
    )
    assert result == 1
    assert len(fake_client.calls) == 0, "no network calls sent when ceiling is raised"


def test_smoke_fails_closed_when_ledger_is_omitted(
    auth_env: None, fake_client: FakeHttpClient
) -> None:
    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1"},
        ledger=None,
    )
    assert result == 1
    assert len(fake_client.calls) == 0, "no network calls sent when ledger is omitted"


def test_smoke_fails_closed_when_ledger_has_incomplete_attempt(
    tmp_path: Path, auth_env: None, fake_client: FakeHttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)
    ledger.write_text(
        json.dumps({"phase": "start", "attempt": 1, "case": "1-explicit-fields"}) + "\n",
        encoding="utf-8",
    )
    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1"},
        ledger=ledger,
    )
    assert result == 1
    assert len(fake_client.calls) == 0, "no network calls sent when ledger history is incomplete"


def test_smoke_fails_closed_when_charged_plus_wanted_exceeds_ceiling(
    tmp_path: Path, auth_env: None, fake_client: FakeHttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)
    # Pre-ledger is 5. Add 11 completed attempts to reach 16
    lines = []
    for i in range(1, 12):
        lines.append(json.dumps({"phase": "start", "attempt": i}))
        lines.append(json.dumps({"phase": "end", "attempt": i}))
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1"},
        ledger=ledger,
        ceiling=16,
    )
    assert result == 1
    assert len(fake_client.calls) == 0, "no network calls sent when ceiling is exhausted"


def test_smoke_fail_fast_on_first_case_failure_verifies_exact_post_count_one(
    tmp_path: Path,
    auth_env: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", evidence_dir)
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    # Case 1 returns 502 Bad Gateway
    fake_client.interpret_responses = [(502, {"error": "bad gateway"}, None)]

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1", "2", "3"},
        ledger=ledger,
    )
    assert result == 1
    assert fake_client.post_interpret_count == 1, "stopped immediately after case 1 failure"

    # Verify attempt 1 was preserved in ledger before failing
    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2
    assert lines[0]["phase"] == "start"
    assert lines[0]["attempt"] == 1
    assert lines[1]["phase"] == "end"
    assert lines[1]["attempt"] == 1
    assert lines[1]["http_status"] == 502

    # Verify evidence file was written immediately
    evidence_file = evidence_dir / assistant_smoke.EVIDENCE_FILENAME
    assert evidence_file.exists()
    evidence_data = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert evidence_data["real_inference_invocations"] == 1
    assert any("HTTP 502" in f for f in evidence_data["failures"])


def test_smoke_fail_fast_on_second_case_failure_verifies_exact_post_count_two(
    tmp_path: Path,
    auth_env: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", evidence_dir)
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    diag_ok = {
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

    # Extract dynamic explicit due timestamp for Case 1
    case1_due = assistant_smoke._cases()[0]["expect"]["due_at"][0]

    # Case 1 passes
    resp_case1 = {
        "draft": {
            "borrower_label": "Meena R",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "the Velachery equipment room",
            "due_at": case1_due,
        },
        "missing_fields": [],
        "provenance": {
            "framework": "strands",
            "provider": "ollama",
            "model": "llama3.2:3b",
            "inventory_tool_calls": 1,
            "completed_at": "2026-09-08T12:00:00Z",
        },
    }
    # Case 2 fails: equipment_kind is None instead of WALKER (model omission)
    resp_case2 = {
        "draft": {
            "borrower_label": "Arun",
            "equipment_kind": None,  # should be WALKER
            "pickup_location": None,
            "due_at": None,
        },
        "missing_fields": ["equipment_kind", "pickup_location", "due_at"],
        "provenance": {
            "framework": "strands",
            "provider": "ollama",
            "model": "llama3.2:3b",
            "inventory_tool_calls": 1,
            "completed_at": "2026-09-08T12:00:05Z",
        },
    }

    fake_client.interpret_responses = [
        (200, resp_case1, diag_ok),
        (200, resp_case2, diag_ok),
    ]

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1", "2", "3"},
        ledger=ledger,
    )
    assert result == 1
    assert fake_client.post_interpret_count == 2, (
        "stopped immediately after case 2 failure; case 3 never called"
    )

    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 4
    assert lines[2]["phase"] == "start"
    assert lines[2]["attempt"] == 2
    assert lines[3]["phase"] == "end"
    assert lines[3]["attempt"] == 2

    evidence_file = evidence_dir / assistant_smoke.EVIDENCE_FILENAME
    assert evidence_file.exists()
    evidence_data = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert evidence_data["real_inference_invocations"] == 2
    assert any("equipment_kind=None" in f for f in evidence_data["failures"])


def test_smoke_fail_fast_on_snapshot_mutation(
    tmp_path: Path,
    auth_env: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", evidence_dir)
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    # Mutate snapshot during case 1
    fake_client.mutate_snapshot_on_interpret_count = 1

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1", "2", "3"},
        ledger=ledger,
    )
    assert result == 1
    assert fake_client.post_interpret_count == 1, "stopped immediately on snapshot change"

    evidence_file = evidence_dir / assistant_smoke.EVIDENCE_FILENAME
    assert evidence_file.exists()
    evidence_data = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert any("business snapshot changed" in f for f in evidence_data["failures"])


def test_smoke_fail_fast_on_missing_or_invalid_diagnostic(
    tmp_path: Path,
    auth_env: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", evidence_dir)
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    case1_due = assistant_smoke._cases()[0]["expect"]["due_at"][0]
    resp_case1 = {
        "draft": {
            "borrower_label": "Meena R",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "the Velachery equipment room",
            "due_at": case1_due,
        },
        "missing_fields": [],
        "provenance": {
            "framework": "strands",
            "provider": "ollama",
            "model": "llama3.2:3b",
            "inventory_tool_calls": 1,
            "completed_at": "2026-09-08T12:00:00Z",
        },
    }

    # Missing diagnostic header
    fake_client.interpret_responses = [(200, resp_case1, None)]

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1", "2"},
        ledger=ledger,
    )
    assert result == 1
    assert fake_client.post_interpret_count == 1
    evidence_file = evidence_dir / assistant_smoke.EVIDENCE_FILENAME
    evidence_data = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert any("missing X-Assistant-Diagnostic header" in f for f in evidence_data["failures"])


def test_check_case_exact_assertions() -> None:
    cases = assistant_smoke._cases()
    case1 = cases[0]
    case2 = cases[1]
    due_explicit = case1["expect"]["due_at"][0]

    valid_diag = {
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

    valid_case1_payload = {
        "draft": {
            "borrower_label": "Meena R",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "the Velachery equipment room",
            "due_at": due_explicit,
        },
        "missing_fields": [],
        "provenance": {
            "framework": "strands",
            "provider": "ollama",
            "model": "llama3.2:3b",
            "inventory_tool_calls": 1,
            "completed_at": "2026-09-08T12:00:00Z",
        },
    }

    # Valid case 1 passes
    assert (
        assistant_smoke._check_case(
            case1,
            200,
            valid_case1_payload,
            snapshot_unchanged=True,
            diagnostic=valid_diag,
        )
        == []
    )

    # R3 defect: equipment_kind null in case 1 fails
    bad_case1_payload = json.loads(json.dumps(valid_case1_payload))
    bad_case1_payload["draft"]["equipment_kind"] = None
    bad_case1_payload["missing_fields"] = ["equipment_kind"]
    failures = assistant_smoke._check_case(
        case1,
        200,
        bad_case1_payload,
        snapshot_unchanged=True,
        diagnostic=valid_diag,
    )
    assert any("equipment_kind=None" in f for f in failures)
    assert any("missing_fields" in f for f in failures)

    # Case 2 relative date: missing pickup and WALKER kind
    valid_case2_payload = {
        "draft": {
            "borrower_label": "Arun",
            "equipment_kind": "WALKER",
            "pickup_location": None,
            "due_at": None,
        },
        "missing_fields": ["pickup_location", "due_at"],
        "provenance": {
            "framework": "strands",
            "provider": "ollama",
            "model": "llama3.2:3b",
            "inventory_tool_calls": 1,
            "completed_at": "2026-09-08T12:00:00Z",
        },
    }
    assert (
        assistant_smoke._check_case(
            case2,
            200,
            valid_case2_payload,
            snapshot_unchanged=True,
            diagnostic=valid_diag,
        )
        == []
    )

    # Case 2 with pickup_location populated fails
    bad_case2 = json.loads(json.dumps(valid_case2_payload))
    bad_case2["draft"]["pickup_location"] = "someplace"
    assert any(
        "pickup_location='someplace'" in f
        for f in assistant_smoke._check_case(
            case2,
            200,
            bad_case2,
            snapshot_unchanged=True,
            diagnostic=valid_diag,
        )
    )

    # Diagnostic with outcome != success fails
    failed_diag = dict(valid_diag, outcome="failed")
    assert any(
        "outcome='failed'" in f
        for f in assistant_smoke._check_case(
            case1,
            200,
            valid_case1_payload,
            snapshot_unchanged=True,
            diagnostic=failed_diag,
        )
    )


def test_smoke_full_success_offline_fake(
    tmp_path: Path,
    auth_env: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", evidence_dir)
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    # Freeze dynamic timestamps in test fixture so separate calls cannot cross a minute
    cases = assistant_smoke._cases()
    monkeypatch.setattr(assistant_smoke, "_cases", lambda: cases)
    assert len(cases) == 6

    diag_base = {
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

    payloads = []
    for idx, c in enumerate(cases, start=1):
        expect = c["expect"]
        payloads.append(
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
                        "framework": "strands",
                        "provider": "ollama",
                        "model": "llama3.2:3b",
                        "inventory_tool_calls": 1,
                        "completed_at": f"2026-09-08T12:00:{idx * 5:02d}Z",
                    },
                },
                dict(diag_base, correlation_id=f"c_ok_{idx}"),
            )
        )
    fake_client.interpret_responses = list(payloads)

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1", "2", "3", "4", "5", "6"},
        ledger=ledger,
    )
    assert result == assistant_smoke.EXIT_PASS
    assert fake_client.post_interpret_count == 6
    evidence_file = evidence_dir / assistant_smoke.EVIDENCE_FILENAME
    assert evidence_file.exists()
    evidence_data = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert evidence_data["real_inference_invocations"] == 6
    assert len(evidence_data["failures"]) == 0
    assert evidence_data["verdict"] == "PASS"
    assert evidence_data["business_snapshot_unchanged"] is True


def test_smoke_fails_closed_when_ledger_is_not_canonical(
    tmp_path: Path, auth_env: None, fake_client: FakeHttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = tmp_path / "canonical.jsonl"
    canonical.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", canonical)

    alternate = tmp_path / "alternate.jsonl"
    alternate.touch()

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1"},
        ledger=alternate,
    )
    assert result == 1
    assert len(fake_client.calls) == 0, "no network calls sent when ledger is not canonical"


def test_smoke_fails_closed_when_canonical_ledger_does_not_exist(
    tmp_path: Path, auth_env: None, fake_client: FakeHttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "never_created.jsonl"
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", missing)

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1"},
        ledger=missing,
    )
    assert result == 1
    assert len(fake_client.calls) == 0, "no network calls sent when canonical ledger is missing"


@pytest.mark.parametrize(
    "bad_content",
    [
        "not json\n",
        '{"invalid": \n',
        json.dumps({"phase": "start", "attempt": 1})
        + "\n"
        + json.dumps({"phase": "start", "attempt": 1})
        + "\n",
        json.dumps({"phase": "start", "attempt": 1})
        + "\n"
        + json.dumps({"phase": "end", "attempt": 1})
        + "\n"
        + json.dumps({"phase": "end", "attempt": 1})
        + "\n",
        json.dumps({"phase": "end", "attempt": 1}) + "\n",
        json.dumps({"phase": "start", "attempt": "not-an-int"}) + "\n",
    ],
)
def test_smoke_fails_closed_when_ledger_history_is_corrupt_or_duplicate(
    tmp_path: Path,
    auth_env: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
    bad_content: str,
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(bad_content, encoding="utf-8")
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1"},
        ledger=ledger,
    )
    assert result == 1
    assert len(fake_client.calls) == 0, "no network calls sent when ledger history is invalid"


def test_smoke_real_client_captures_diagnostic_before_snapshot_resets_header(
    tmp_path: Path,
    auth_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", evidence_dir)
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    case1_due = assistant_smoke._cases()[0]["expect"]["due_at"][0]
    diag = {
        "correlation_id": "real-client-corr-1",
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
    interpret_body = {
        "draft": {
            "borrower_label": "Meena R",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "the Velachery equipment room",
            "due_at": case1_due,
        },
        "missing_fields": [],
        "provenance": {
            "framework": "strands",
            "provider": "ollama",
            "model": "llama3.2:3b",
            "inventory_tool_calls": 1,
            "completed_at": "2026-09-08T12:00:00Z",
        },
    }

    call_counts: dict[str, int] = {}

    def router(req: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
        url = req.full_url
        if "/api/health" in url:
            call_counts["health"] = call_counts.get("health", 0) + 1
            return (
                200,
                {"Content-Type": "application/json"},
                json.dumps({"agent_mode": "strands_ollama"}).encode(),
            )
        if "/api/workspaces" in url:
            call_counts["workspaces"] = call_counts.get("workspaces", 0) + 1
            return (
                201,
                {"Content-Type": "application/json"},
                json.dumps({"workspace_id": "ws-1"}).encode(),
            )
        if "/api/snapshot" in url:
            call_counts["snapshot"] = call_counts.get("snapshot", 0) + 1
            # Snapshot returns NO diagnostic header! The body is the real
            # snapshot shape: the proof requires a usable baseline before it
            # will send anything, so a placeholder body would stop the run for
            # a reason that has nothing to do with what this test is about.
            return (
                200,
                {"Content-Type": "application/json"},
                json.dumps({"equipment": [], "requests": [], "loans": [], "events": []}).encode(),
            )
        if "/api/intake/interpret" in url:
            call_counts["interpret"] = call_counts.get("interpret", 0) + 1
            return (
                200,
                {
                    "Content-Type": "application/json",
                    "X-Assistant-Diagnostic": json.dumps(diag),
                },
                json.dumps(interpret_body).encode(),
            )
        return 404, {"Content-Type": "application/json"}, b'{"error": "not found"}'

    real_client = assistant_smoke.Client("http://127.0.0.1:8000")
    real_client.opener = urllib.request.build_opener(FakeOpenerHandler(router))

    # Verify directly that Client.call clears last_diagnostic on snapshot call!
    status, _ = real_client.call("POST", "/api/intake/interpret", {"text": "dummy"})
    assert status == 200
    assert real_client.last_diagnostic == diag, "real Client captures X-Assistant-Diagnostic header"

    status, _ = real_client.call("GET", "/api/snapshot")
    assert status == 200
    assert real_client.__dict__["last_diagnostic"] is None, (
        "real Client resets last_diagnostic on snapshot call"
    )

    # Now run assistant_smoke.main using this real Client (patched into Client constructor)
    monkeypatch.setattr(assistant_smoke, "Client", lambda *args, **kwargs: real_client)

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1"},
        ledger=ledger,
    )
    assert result == assistant_smoke.EXIT_PARTIAL, "single case completes as PARTIAL run"
    assert call_counts["interpret"] == 2  # 1 direct + 1 in main
    assert call_counts["snapshot"] >= 3  # 1 direct + 2 in main (before + after)

    evidence_file = evidence_dir / assistant_smoke.EVIDENCE_FILENAME
    evidence_data = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert evidence_data["real_inference_invocations"] == 1
    assert evidence_data["verdict"] == "PARTIAL"
    assert len(evidence_data["failures"]) == 0


def test_smoke_fails_when_snapshot_returns_http_error_even_if_bodies_identical(
    tmp_path: Path,
    auth_env: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", evidence_dir)
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    # Fake client snapshot call returns 500 on all calls (identical bodies)
    original_call = fake_client.call

    def fake_snapshot_call(
        method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 180.0
    ) -> tuple[int, dict[str, Any]]:
        if path == "/api/snapshot":
            return 500, {"error": "internal snapshot failure"}
        return original_call(method, path, payload, timeout)

    monkeypatch.setattr(fake_client, "call", fake_snapshot_call)

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1", "2"},
        ledger=ledger,
    )
    assert result == 1
    # BS-003-R6 replacement. This test previously asserted post_interpret_count
    # == 1: the proof sent the interpretation and only then noticed the snapshot
    # had failed. The failing snapshot here is the pre-case one, so the case now
    # stops before anything is sent. The invariant the test exists for — a
    # snapshot HTTP error fails the proof even when the bodies match — is
    # unchanged and is asserted below; only the point at which it is caught has
    # moved earlier, which is the fix.
    assert fake_client.post_interpret_count == 0, "nothing may be sent without a baseline"

    evidence_file = evidence_dir / assistant_smoke.EVIDENCE_FILENAME
    evidence_data = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert any("pre-case snapshot returned HTTP 500" in f for f in evidence_data["failures"])
    assert evidence_data["stopped_at_case"] == "1-explicit-fields"
    # Nothing completed, so read-only behaviour was never established either way.
    assert evidence_data["business_snapshot_unchanged"] is None


def test_check_case_rejects_bool_and_out_of_bounds_counters() -> None:
    case = assistant_smoke._cases()[0]
    case1_due = case["expect"]["due_at"][0]
    valid_provenance = {
        "framework": "strands",
        "provider": "ollama",
        "model": "llama3.2:3b",
        "inventory_tool_calls": 1,
        "completed_at": "2026-09-08T12:00:00Z",
    }
    valid_payload: dict[str, Any] = {
        "draft": {
            "borrower_label": "Meena R",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "the Velachery equipment room",
            "due_at": case1_due,
        },
        "missing_fields": [],
        "provenance": valid_provenance,
    }
    valid_diag = {
        "correlation_id": "valid-corr-1",
        "stage": assistant_smoke.TERMINAL_SUCCESS_STAGE,
        "outcome": "success",
        "reason": "ok",
        "sends": 2,
        "tool_attempts": 1,
        "tool_successes": 1,
        "recovery_used": False,
        "cleanup": "closed",
        "elapsed_ms": 500,
    }

    # Baseline passes
    assert (
        assistant_smoke._check_case(
            case, 200, valid_payload, snapshot_unchanged=True, diagnostic=valid_diag
        )
        == []
    )

    # sends=True (bool rejected as int)
    bad_diag = dict(valid_diag, sends=True)
    failures = assistant_smoke._check_case(
        case, 200, valid_payload, snapshot_unchanged=True, diagnostic=bad_diag
    )
    assert any("sends=True" in f for f in failures)

    # sends=7 (exceeds max 6)
    bad_diag = dict(valid_diag, sends=7)
    failures = assistant_smoke._check_case(
        case, 200, valid_payload, snapshot_unchanged=True, diagnostic=bad_diag
    )
    assert any("sends=7" in f for f in failures)

    # tool_attempts=True (bool rejected)
    bad_diag = dict(valid_diag, tool_attempts=True)
    failures = assistant_smoke._check_case(
        case, 200, valid_payload, snapshot_unchanged=True, diagnostic=bad_diag
    )
    assert any("tool_attempts=True" in f for f in failures)

    # tool_attempts=3 (exceeds max 2)
    bad_diag = dict(valid_diag, tool_attempts=3)
    failures = assistant_smoke._check_case(
        case, 200, valid_payload, snapshot_unchanged=True, diagnostic=bad_diag
    )
    assert any("tool_attempts=3" in f for f in failures)

    # tool_successes=True (bool rejected)
    bad_diag = dict(valid_diag, tool_successes=True)
    failures = assistant_smoke._check_case(
        case, 200, valid_payload, snapshot_unchanged=True, diagnostic=bad_diag
    )
    assert any("tool_successes=True" in f for f in failures)

    # tool_successes > tool_attempts
    bad_diag = dict(valid_diag, tool_attempts=0, tool_successes=1)
    failures = assistant_smoke._check_case(
        case, 200, valid_payload, snapshot_unchanged=True, diagnostic=bad_diag
    )
    assert any("exceeds tool_attempts" in f for f in failures)

    # tool_successes mismatch with provenance inventory_tool_calls
    bad_diag = dict(valid_diag, tool_attempts=2, tool_successes=2)
    failures = assistant_smoke._check_case(
        case, 200, valid_payload, snapshot_unchanged=True, diagnostic=bad_diag
    )
    assert any("does not match provenance inventory_tool_calls" in f for f in failures)

    # recovery_used=1 (int not bool)
    bad_diag = dict(valid_diag, recovery_used=1)
    failures = assistant_smoke._check_case(
        case, 200, valid_payload, snapshot_unchanged=True, diagnostic=bad_diag
    )
    assert any("recovery_used=1" in f for f in failures)

    # elapsed_ms=-1 (negative)
    bad_diag = dict(valid_diag, elapsed_ms=-1)
    failures = assistant_smoke._check_case(
        case, 200, valid_payload, snapshot_unchanged=True, diagnostic=bad_diag
    )
    assert any("elapsed_ms=-1" in f for f in failures)

    # correlation_id empty
    bad_diag = dict(valid_diag, correlation_id="   ")
    failures = assistant_smoke._check_case(
        case, 200, valid_payload, snapshot_unchanged=True, diagnostic=bad_diag
    )
    assert any("correlation_id is empty" in f for f in failures)

    # duplicate correlation_id
    seen = {"valid-corr-1"}
    failures = assistant_smoke._check_case(
        case,
        200,
        valid_payload,
        snapshot_unchanged=True,
        diagnostic=valid_diag,
        seen_correlation_ids=seen,
    )
    assert any("duplicate/replayed" in f for f in failures)

    # invalid calendar date in completed_at (Feb 30)
    bad_payload = dict(
        valid_payload,
        provenance=dict(valid_provenance, completed_at="2026-02-30T12:00:00Z"),
    )
    failures = assistant_smoke._check_case(
        case, 200, bad_payload, snapshot_unchanged=True, diagnostic=valid_diag
    )
    assert any(
        "completed_at='2026-02-30T12:00:00Z' is not a valid UTC ISO timestamp" in f
        for f in failures
    )

    # invalid due_at date string
    bad_payload = dict(
        valid_payload,
        draft=dict(valid_payload["draft"], due_at="2026-13-45T12:00:00Z"),
    )
    failures = assistant_smoke._check_case(
        case, 200, bad_payload, snapshot_unchanged=True, diagnostic=valid_diag
    )
    assert any(
        "due_at='2026-13-45T12:00:00Z' is not a valid UTC ISO timestamp" in f for f in failures
    )


def test_smoke_fails_fast_on_replayed_correlation_id_and_aborts_next_case(
    tmp_path: Path,
    auth_env: None,
    fake_client: FakeHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(assistant_smoke, "EVIDENCE_DIR", evidence_dir)
    ledger = tmp_path / "ledger.jsonl"
    ledger.touch()
    monkeypatch.setattr(assistant_smoke, "CANONICAL_LEDGER", ledger)

    case1_due = assistant_smoke._cases()[0]["expect"]["due_at"][0]
    replayed_diag = {
        "correlation_id": "duplicate-id",
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

    # Case 1 and Case 2 both return identical correlation_id
    payloads = [
        (
            200,
            {
                "draft": {
                    "borrower_label": "Meena R",
                    "equipment_kind": "WHEELCHAIR",
                    "pickup_location": "the Velachery equipment room",
                    "due_at": case1_due,
                },
                "missing_fields": [],
                "provenance": {
                    "framework": "strands",
                    "provider": "ollama",
                    "model": "llama3.2:3b",
                    "inventory_tool_calls": 1,
                    "completed_at": "2026-09-08T12:00:00Z",
                },
            },
            replayed_diag,
        ),
        (
            200,
            {
                "draft": {
                    "borrower_label": "Arun",
                    "equipment_kind": "WALKER",
                    "pickup_location": None,
                    "due_at": None,
                },
                "missing_fields": ["pickup_location", "due_at"],
                "provenance": {
                    "framework": "strands",
                    "provider": "ollama",
                    "model": "llama3.2:3b",
                    "inventory_tool_calls": 1,
                    "completed_at": "2026-09-08T12:00:05Z",
                },
            },
            replayed_diag,
        ),
    ]
    fake_client.interpret_responses = list(payloads)

    result = assistant_smoke.main(
        "http://127.0.0.1:8000",
        {"1", "2", "3"},
        ledger=ledger,
    )
    assert result == 1
    assert fake_client.post_interpret_count == 2, "stops on case 2; case 3 never called"

    evidence_file = evidence_dir / assistant_smoke.EVIDENCE_FILENAME
    evidence_data = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert any(
        "duplicate/replayed diagnostic correlation_id: 'duplicate-id'" in f
        for f in evidence_data["failures"]
    )
