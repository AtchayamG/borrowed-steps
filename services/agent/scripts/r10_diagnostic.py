"""R10 single diagnostic experiment harness and launcher.

Executes ONE controlled observation of the original R8 explicit input with
active INFO logging to establish actual server-side grounding reason codes.
Historic R8 cause remains UNKNOWN.

Zero new inference is permitted beyond this single controlled execution.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Ensure scripts directory is importable
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import assistant_smoke  # noqa: E402

from borrowed_steps.application.grounding import FIELD_ORDER, REASONS  # noqa: E402
from borrowed_steps.domain.rules import validate_due_at  # noqa: E402

TASK_ID = "BS-003-R10-AGY"
REQUIRED_AUTHORIZATION = "BS-003-R10-AGY"
CUMULATIVE_CEILING = 18
HISTORICAL_SPENT = 17
ATTEMPT_ID = 13
EXPECTED_BRANCH = "worker/agy/BS-003-R10"
EXPECTED_SRC_TREE = "f03cf44265a09cf8e819febf2594ad25a3ff880d"

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "test-evidence"
CANONICAL_LEDGER = EVIDENCE_DIR / "probe-ledger.jsonl"
R8_ARTIFACT = EVIDENCE_DIR / "assistant-live-proof-r8.json"
CLAIM_FILE = EVIDENCE_DIR / "r10-task.claim"
DIAGNOSTIC_JSON_FILENAME = "r10-diagnostic.json"
DIAGNOSTIC_NOTES_FILENAME = "r10-notes.md"
DEFAULT_BASE_URL = "http://127.0.0.1:8220"
DEFAULT_SERVER_LOG = EVIDENCE_DIR / "r10-server.log"

LOGGING_READINESS_MARKER = "LOGGING_READINESS_MARKER: R10 diagnostic logging pipeline verified"

GROUNDING_RE = re.compile(
    r"Assistant grounding:\s*(?P<fields>[^|\r\n]+)\s*\|\s*"
    r"model_requests=(?P<requests>\d+)\s+tool_calls=(?P<tools>\d+)"
)
TERMINAL_RE = re.compile(
    r"Assistant\s+(?P<corr_id>[a-zA-Z0-9_-]+)\s+(?P<outcome>\w+)\s+"
    r"at stage=(?P<stage>\w+)\s+reason=(?P<reason>\w+)\s*\|\s*"
    r"sends=(?P<sends>\d+)\s+tool_attempts=(?P<tool_attempts>\d+)\s+"
    r"tool_successes=(?P<tool_successes>\d+)\s+"
    r"recovery=(?P<recovery>\w+)\s+elapsed_ms=(?P<elapsed_ms>\d+)\s+"
    r"cleanup=(?P<cleanup>\w+)"
)


class ClaimRefusedError(Exception):
    """Raised when permanent task claim cannot be acquired."""


def acquire_claim(claim_path: Path, metadata: dict[str, Any]) -> None:
    """Acquire permanent exclusive task claim file."""
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(claim_path, "x", encoding="utf-8") as f:
            f.write(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    except FileExistsError as exc:
        raise ClaimRefusedError(
            f"Claim file {claim_path} already exists. "
            f"The {TASK_ID} allocation is permanently closed."
        ) from exc


def load_and_validate_r8_input(
    r8_artifact_path: Path, now_dt: datetime | None = None
) -> tuple[str, str]:
    """Read the original synthetic text from R8 artifact and validate due date."""
    if not r8_artifact_path.is_file():
        raise ValueError(f"R8 artifact {r8_artifact_path} does not exist.")
    try:
        data = json.loads(r8_artifact_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Failed to parse R8 artifact: {exc}") from exc

    cases = data.get("cases", [])
    if not cases:
        raise ValueError("R8 artifact contains no cases.")
    synthetic_text = str(cases[0].get("synthetic_text", ""))
    if not synthetic_text:
        raise ValueError("R8 artifact case 1 has no synthetic_text.")

    match = re.search(
        r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})\b",
        synthetic_text,
    )
    if not match:
        raise ValueError(f"Could not extract timestamp from synthetic_text: {synthetic_text!r}")
    ts_str = match.group(0)
    parsed_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if parsed_dt.tzinfo is None:
        raise ValueError(f"Timestamp in synthetic_text is naive: {ts_str}")

    current_dt = now_dt or datetime.now(UTC)
    validate_due_at(parsed_dt, current_dt)
    return synthetic_text, ts_str


def parse_diagnostic_logs(
    log_content: str,
    expected_correlation_id: str,
    expected_sends: int | None = None,
    expected_tool_successes: int | None = None,
) -> tuple[dict[str, str], dict[str, Any], list[str]]:
    """Parse and validate safe grounding and terminal log lines for this single call."""
    grounding_matches = list(GROUNDING_RE.finditer(log_content))
    if not grounding_matches:
        raise ValueError("No grounding log line found in server output.")
    if len(grounding_matches) > 1:
        count = len(grounding_matches)
        raise ValueError(f"Multiple ({count}) grounding log lines found; ambiguous.")

    g_match = grounding_matches[0]
    fields_raw = g_match.group("fields").strip()
    reason_codes: dict[str, str] = {}
    for part in fields_raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key not in FIELD_ORDER:
            raise ValueError(f"Grounding field {key!r} is not in FIELD_ORDER {FIELD_ORDER}.")
        if val not in REASONS:
            raise ValueError(f"Grounding reason code {val!r} is not in REASONS.")
        reason_codes[key] = val

    if set(reason_codes.keys()) != set(FIELD_ORDER):
        found = sorted(reason_codes.keys())
        raise ValueError(f"Grounding log line does not contain all four fields. Found: {found}")

    g_requests = int(g_match.group("requests"))
    g_tools = int(g_match.group("tools"))

    terminal_matches = list(TERMINAL_RE.finditer(log_content))
    corr_matches = [m for m in terminal_matches if m.group("corr_id") == expected_correlation_id]
    if not corr_matches:
        raise ValueError(
            f"No terminal log line found matching correlation_id {expected_correlation_id!r}."
        )
    if len(corr_matches) > 1:
        raise ValueError(
            f"Multiple terminal log lines found for correlation_id {expected_correlation_id!r}."
        )

    t_match = corr_matches[0]
    terminal_info = t_match.groupdict()
    t_sends = int(terminal_info["sends"])
    t_tools = int(terminal_info["tool_successes"])

    if expected_sends is not None and t_sends != expected_sends:
        raise ValueError(
            f"Terminal log sends={t_sends} does not match diagnostic sends={expected_sends}."
        )
    if expected_tool_successes is not None and t_tools != expected_tool_successes:
        raise ValueError(
            f"Terminal log tool_successes={t_tools} does not match "
            f"diagnostic tool_successes={expected_tool_successes}."
        )
    if g_requests != t_sends:
        raise ValueError(
            f"Grounding model_requests={g_requests} does not match terminal sends={t_sends}."
        )
    if g_tools != t_tools:
        raise ValueError(
            f"Grounding tool_calls={g_tools} does not match terminal tool_successes={t_tools}."
        )

    safe_excerpts = [g_match.group(0).strip(), t_match.group(0).strip()]
    return reason_codes, terminal_info, safe_excerpts


def write_diagnostic_artifacts(
    *,
    evidence_dir: Path,
    implementation_commit: str,
    synthetic_request: str,
    http_status: int,
    response: dict[str, Any],
    diagnostic: dict[str, Any] | None,
    reason_codes: dict[str, str],
    safe_excerpts: list[str],
    source_log_path: str,
    log_byte_window: tuple[int, int],
    snapshot_statuses: list[int],
    snapshot_unchanged: bool,
    diagnostic_verdict: str,
    functional_verdict: str,
) -> tuple[Path, Path]:
    """Write r10-diagnostic.json and r10-notes.md artifacts."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    diagnostic_data = {
        "task_id": TASK_ID,
        "attempt": ATTEMPT_ID,
        "implementation_commit": implementation_commit,
        "recorded_at": now_iso,
        "provider": assistant_smoke.EXPECTED_PROVIDER,
        "model": assistant_smoke.EXPECTED_MODEL,
        "synthetic_request": synthetic_request,
        "http_status": http_status,
        "response": response,
        "terminal_diagnostic": diagnostic,
        "reason_codes": reason_codes,
        "log_excerpts": safe_excerpts,
        "source_log_path": source_log_path,
        "log_byte_window": list(log_byte_window),
        "snapshots": {
            "statuses": snapshot_statuses,
            "unchanged": snapshot_unchanged,
        },
        "accounting": {
            "attempts_charged_before_this_run": HISTORICAL_SPENT,
            "invocations_this_run": 1,
            "cumulative_attempts_charged": CUMULATIVE_CEILING,
            "cumulative_ceiling": CUMULATIVE_CEILING,
            "remaining_allowance": 0,
        },
        "diagnostic_verdict": diagnostic_verdict,
        "functional_verdict": functional_verdict,
    }

    json_path = evidence_dir / DIAGNOSTIC_JSON_FILENAME
    json_path.write_text(json.dumps(diagnostic_data, indent=2, sort_keys=True), encoding="utf-8")

    reason_table = "\n".join(f"| `{k}` | `{v}` |" for k, v in sorted(reason_codes.items()))
    notes_content = (
        f"# BS-003-R10-AGY Diagnostic Notes\n\n"
        f"**Date**: {now_iso}\n"
        f"**Task ID**: {TASK_ID}\n"
        f"**Implementation Commit**: `{implementation_commit}`\n"
        f"**Provider / Model**: `{assistant_smoke.EXPECTED_PROVIDER}` / "
        f"`{assistant_smoke.EXPECTED_MODEL}`\n"
        f"**Diagnostic Verdict**: **`{diagnostic_verdict}`**\n"
        f"**Functional Verdict**: **`{functional_verdict}`**\n\n"
        f"---\n\n"
        f"## 1. Input and Observed Execution\n\n"
        f"- **Synthetic Request**: `{synthetic_request}`\n"
        f"- **HTTP Status**: {http_status}\n"
        f"- **Snapshots**: Unchanged ({snapshot_unchanged}), Statuses {snapshot_statuses}\n"
        f"- **Log Source**: `{source_log_path}` "
        f"(Bytes {log_byte_window[0]}..{log_byte_window[1]})\n\n"
        f"## 2. Server-Side Grounding Reason Codes\n\n"
        f"| Field | Reason Code |\n"
        f"|---|---|\n"
        f"{reason_table}\n\n"
        f"## 3. Safe Log Excerpts\n\n"
        f"```\n"
        f"{chr(10).join(safe_excerpts)}\n"
        f"```\n\n"
        f"## 4. Accounting and Permanent Closure\n\n"
        f"- Attempts charged prior to run: {HISTORICAL_SPENT}\n"
        f"- Invocations this run: 1\n"
        f"- Cumulative attempts charged: {CUMULATIVE_CEILING} of {CUMULATIVE_CEILING} ceiling\n"
        f"- Remaining task allowance: 0 (allocation permanently closed)\n\n"
        f"## 5. Scope and Historic Fact Qualification\n\n"
        f"Historic R8 cause remains **UNKNOWN**. This observation provides authentic attributable "
        f"reason\n"
        f"codes for this single new execution of the original explicit input under active INFO "
        f"logging.\n"
        f"It does not retrospectively establish what occurred during R8 Attempt 12.\n"
        f"M2A integration live proof remains **BLOCKED**.\n"
    )
    notes_path = evidence_dir / DIAGNOSTIC_NOTES_FILENAME
    notes_path.write_text(notes_content, encoding="utf-8")
    return json_path, notes_path


def main(
    base_url: str = DEFAULT_BASE_URL,
    server_log: Path | None = None,
    ledger: Path | None = None,
    claim_file: Path | None = None,
    evidence_dir: Path | None = None,
    r8_artifact: Path | None = None,
    implementation_commit: str | None = None,
    check_branch: bool = True,
    check_src: bool = True,
    client: assistant_smoke.Client | None = None,
    now_dt: datetime | None = None,
) -> int:
    """Replay the retired task with an injected offline client only."""
    if client is None:
        print("R10 is retired; its single-call allocation is exhausted.")
        return 1
    # 1. Authorization guard
    auth = os.environ.get(assistant_smoke.AUTHORIZATION_ENV)
    if auth != REQUIRED_AUTHORIZATION:
        print(
            f"FAIL {assistant_smoke.AUTHORIZATION_ENV}={auth!r} is invalid. "
            f"Exact authorization {REQUIRED_AUTHORIZATION!r} required."
        )
        return 1

    # 2. Branch guard
    if check_branch:
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(_SCRIPTS_DIR),
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if branch != EXPECTED_BRANCH:
                print(f"FAIL current branch {branch!r} is not expected {EXPECTED_BRANCH!r}.")
                return 1
        except Exception as exc:
            print(f"FAIL checking git branch: {exc}")
            return 1

    # 3. Base URL loopback guard
    parsed = urlparse(base_url)
    hostname = parsed.hostname or ""
    if hostname not in ("127.0.0.1", "localhost"):
        print(f"FAIL base_url {base_url!r} must be loopback (127.0.0.1 or localhost).")
        return 1

    # 4. Source tree pinned check
    if check_src:
        try:
            src_tree = subprocess.check_output(
                ["git", "rev-parse", "HEAD:services/agent/src"],
                cwd=str(_SCRIPTS_DIR),
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if src_tree != EXPECTED_SRC_TREE:
                print(
                    f"FAIL services/agent/src tree {src_tree} "
                    f"does not match expected {EXPECTED_SRC_TREE}."
                )
                return 1
            diff = subprocess.check_output(
                ["git", "status", "--porcelain", "--", "services/agent/src"],
                cwd=str(_SCRIPTS_DIR),
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if diff:
                print(f"FAIL uncommitted changes in services/agent/src:\n{diff}")
                return 1
        except Exception as exc:
            print(f"FAIL checking source tree: {exc}")
            return 1

    target_ledger = ledger or CANONICAL_LEDGER
    target_claim = claim_file or CLAIM_FILE
    target_evidence_dir = evidence_dir or EVIDENCE_DIR
    target_r8_artifact = r8_artifact or R8_ARTIFACT
    target_server_log = server_log or DEFAULT_SERVER_LOG

    # 5. Check canonical ledger path and baseline consumed history
    if not target_ledger.is_file():
        print(f"FAIL ledger {target_ledger} does not exist.")
        return 1

    charged, incomplete, next_attempt, errors = assistant_smoke._consumed(target_ledger)
    if errors:
        print(f"FAIL ledger errors: {'; '.join(errors)}")
        return 1
    if charged != HISTORICAL_SPENT:
        print(f"FAIL ledger charged={charged}, expected exactly {HISTORICAL_SPENT}.")
        return 1
    if incomplete != 0:
        print(f"FAIL ledger incomplete={incomplete}, expected 0.")
        return 1
    if next_attempt != ATTEMPT_ID:
        print(f"FAIL ledger next_attempt={next_attempt}, expected exactly {ATTEMPT_ID}.")
        return 1

    # Verify no previous R10 records exist in ledger
    try:
        lines = target_ledger.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("task_id") == TASK_ID or entry.get("attempt", 0) >= ATTEMPT_ID:
                print(
                    f"FAIL line {idx} already records R10 history. Allocation permanently closed."
                )
                return 1
    except Exception as exc:
        print(f"FAIL reading ledger: {exc}")
        return 1

    # 6. Check permanent claim file
    if target_claim.exists():
        print(f"FAIL claim file {target_claim} already exists. Allocation permanently closed.")
        return 1

    # 7. Read and validate original R8 synthetic text
    try:
        synthetic_text, due_timestamp = load_and_validate_r8_input(
            target_r8_artifact, now_dt=now_dt
        )
    except Exception as exc:
        print(f"FAIL validating R8 synthetic input: {exc}")
        return 1
    print(f"ok   input validated: due_at {due_timestamp}")

    # 8. Verify server logging readiness marker
    if not target_server_log.is_file():
        print(f"FAIL server log file {target_server_log} does not exist.")
        return 1
    try:
        initial_log_content = target_server_log.read_text(encoding="utf-8")
        if LOGGING_READINESS_MARKER not in initial_log_content:
            print(
                "FAIL server log does not contain logging readiness marker: "
                f"{LOGGING_READINESS_MARKER!r}."
            )
            return 1
    except Exception as exc:
        print(f"FAIL reading server log: {exc}")
        return 1
    print("ok   logging readiness marker verified in server log")

    commit_hash = assistant_smoke._get_implementation_commit(implementation_commit)
    print(f"ok   implementation commit: {commit_hash}")

    active_client = client or assistant_smoke.Client(base_url)

    # 9. Health prerequisite
    try:
        status, health = active_client.call("GET", "/api/health")
    except Exception as exc:
        print(f"FAIL health check exception: {exc}")
        return 1
    if status != 200 or health.get("agent_mode") != "strands_ollama":
        print(f"FAIL health check failed: {status} {health}")
        return 1
    print(f"ok   health: {health}")

    # 10. Workspace prerequisite
    try:
        status, _ = active_client.call("POST", "/api/workspaces", {})
    except Exception as exc:
        print(f"FAIL workspace creation exception: {exc}")
        return 1
    if status != 201:
        print(f"FAIL workspace creation: {status}")
        return 1

    # 11. Pre-case snapshot prerequisite
    try:
        status_before, before_snapshot = active_client.call("GET", "/api/snapshot")
    except Exception as exc:
        print(f"FAIL pre-case snapshot prerequisite exception: {exc}")
        return 1
    problem = assistant_smoke._snapshot_problem(status_before, before_snapshot)
    if problem is not None:
        print(f"FAIL pre-case snapshot prerequisite failed: {problem}")
        return 1

    # 12. Acquire permanent exclusive claim file
    try:
        acquire_claim(
            target_claim,
            {
                "task_id": TASK_ID,
                "attempt": ATTEMPT_ID,
                "pid": os.getpid(),
                "claimed_at": assistant_smoke._now(),
                "implementation_commit": commit_hash,
            },
        )
    except ClaimRefusedError as exc:
        print(f"FAIL {exc}")
        return 1
    print(f"ok   acquired exclusive task claim: {target_claim}")

    # 13. Record byte offset and start attempt in canonical ledger
    start_byte_offset = target_server_log.stat().st_size

    assistant_smoke._append_ledger(
        target_ledger,
        {
            "phase": "start",
            "attempt": ATTEMPT_ID,
            "task_id": TASK_ID,
            "implementation_commit": commit_hash,
            "case": "1-explicit-fields",
            "input": synthetic_text,
            "synthetic_text": synthetic_text,
            "label": "r10-diagnostic-call",
            "at": assistant_smoke._now(),
            "purpose": "R10 diagnostic single-call observation with INFO logging",
            "note": "written before sending request; allowance consumed",
        },
    )

    # 14. Dispatch exactly ONE interpretation request
    started = time.monotonic()
    status = 0
    payload: dict[str, Any] = {}
    diagnostic: dict[str, Any] | None = None
    try:
        status, payload = active_client.call(
            "POST", "/api/intake/interpret", {"text": synthetic_text}
        )
        diagnostic = active_client.last_diagnostic
    except Exception as exc:
        print(f"WARN interpretation request exception: {exc}")
        payload = {"error": str(exc)}
        diagnostic = None
    elapsed = round(time.monotonic() - started, 2)

    # 15. Post-case snapshot
    status_after = 0
    after_snapshot: dict[str, Any] = {}
    try:
        status_after, after_snapshot = active_client.call("GET", "/api/snapshot")
    except Exception as exc:
        print(f"WARN post-case snapshot exception: {exc}")
    snapshot_unchanged = (
        status_before == 200 and status_after == 200 and before_snapshot == after_snapshot
    )
    end_byte_offset = target_server_log.stat().st_size

    print(f"\n--- Diagnostic call completed ({elapsed}s, HTTP {status}) ---")
    print(json.dumps(payload, indent=2, sort_keys=True))

    # 16. Parse diagnostic logs
    reason_codes: dict[str, str] = {}
    safe_excerpts: list[str] = []
    diagnostic_verdict = "BLOCKED"
    functional_verdict = "FIELDS_INCORRECT"

    try:
        with open(target_server_log, "rb") as f:
            f.seek(start_byte_offset)
            new_log_bytes = f.read()
        new_log_text = new_log_bytes.decode("utf-8", errors="replace")

        corr_id = diagnostic.get("correlation_id", "") if isinstance(diagnostic, dict) else ""
        sends = diagnostic.get("sends") if isinstance(diagnostic, dict) else None
        tool_succ = diagnostic.get("tool_successes") if isinstance(diagnostic, dict) else None

        if status == 200 and corr_id:
            reason_codes, _term_info, safe_excerpts = parse_diagnostic_logs(
                new_log_text,
                expected_correlation_id=corr_id,
                expected_sends=sends,
                expected_tool_successes=tool_succ,
            )
            diagnostic_verdict = "DIAGNOSTIC_COMPLETE"

            draft = payload.get("draft", {}) if isinstance(payload, dict) else {}
            if (
                draft.get("borrower_label") == "Meena R"
                and draft.get("equipment_kind") == "WHEELCHAIR"
                and draft.get("due_at") == due_timestamp
            ):
                functional_verdict = "FIELDS_CORRECT"
        elif status == 200 and not corr_id:
            diagnostic_verdict = "BLOCKED"
            reason_codes = dict.fromkeys(FIELD_ORDER, "UNKNOWN")
            safe_excerpts = ["Diagnostic correlation_id missing from response"]
        else:
            diagnostic_verdict = "TERMINAL_FAILURE"
            reason_codes = dict.fromkeys(FIELD_ORDER, "NOT_REACHED")
            safe_excerpts = [f"HTTP {status} returned without successful extraction"]
    except Exception as exc:
        print(f"WARN log parsing failed: {exc}")
        diagnostic_verdict = "BLOCKED"
        reason_codes = dict.fromkeys(FIELD_ORDER, "UNKNOWN")
        safe_excerpts = [f"Log parsing error: {exc}"]

    # 17. Record end attempt in ledger
    assistant_smoke._append_ledger(
        target_ledger,
        {
            "phase": "end",
            "attempt": ATTEMPT_ID,
            "task_id": TASK_ID,
            "implementation_commit": commit_hash,
            "case": "1-explicit-fields",
            "synthetic_text": synthetic_text,
            "label": "r10-diagnostic-call",
            "http_status": status,
            "seconds": elapsed,
            "diagnostic": diagnostic,
            "at": assistant_smoke._now(),
            "diagnostic_verdict": diagnostic_verdict,
            "functional_verdict": functional_verdict,
            "reason_codes": reason_codes,
            "snapshot_statuses": [status_before, status_after],
            "snapshot_unchanged": snapshot_unchanged,
        },
    )

    # 18. Write artifacts
    json_path, notes_path = write_diagnostic_artifacts(
        evidence_dir=target_evidence_dir,
        implementation_commit=commit_hash,
        synthetic_request=synthetic_text,
        http_status=status,
        response=payload,
        diagnostic=diagnostic,
        reason_codes=reason_codes,
        safe_excerpts=safe_excerpts,
        source_log_path=str(target_server_log.resolve()),
        log_byte_window=(start_byte_offset, end_byte_offset),
        snapshot_statuses=[status_before, status_after],
        snapshot_unchanged=snapshot_unchanged,
        diagnostic_verdict=diagnostic_verdict,
        functional_verdict=functional_verdict,
    )

    print(f"\nArtifacts written:\n  - {json_path}\n  - {notes_path}")
    print(f"Diagnostic verdict: {diagnostic_verdict}")
    print(f"Functional verdict: {functional_verdict}")
    print(f"Reason codes: {reason_codes}")

    return 0 if diagnostic_verdict == "DIAGNOSTIC_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit("R10 is retired; its single-call allocation is exhausted.")
