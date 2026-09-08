"""Bounded REAL local inference proof for the M2A intake assistant.

This is the only script that talks to a model. Ordinary pytest never does.

It drives a running server over HTTP with ``BS_ASSISTANT_ENABLED=true`` and
proves, from the outside, that:

* a real Strands agent ran against the installed local Ollama model;
* it actually executed the server-bound ``read_inventory`` tool, and provenance
  reports the real count rather than a fabricated one;
* explicitly stated fields survive grounding;
* a relative or missing date comes back null for a human to clarify;
* the business snapshot is byte-identical before and after, so interpretation
  wrote nothing.

Usage::

    python scripts/assistant_smoke.py http://127.0.0.1:8000 [--cases 1,2,3]
        [--ledger test-evidence/probe-ledger.jsonl --label r3-pass-1]

Real interpretations are strictly budgeted, so every attempt is charged to an
append-only ledger *before* the request is sent and the running total is checked
against a cumulative ceiling. An attempt that never returns has still been
charged. The ledger is never restarted.

Nothing here downloads a model, contacts a remote provider or spends anything.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

TASK_ID = "BS-003-R8-AGY"
REQUIRED_AUTHORIZATION = "BS-003-R8-AGY"
REQUIRED_CASES = frozenset(str(i) for i in range(1, 7))
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_PARTIAL = 2
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "test-evidence"
EVIDENCE_FILENAME = "assistant-live-proof-r8.json"
# Every real interpretation ever made for this feature, across BS-003 (5),
# BS-003-R2 (8), BS-003-R3 (3), and the new R8 allocation (6).
# Historical 16 attempts remain spent; ceiling is 22.
CUMULATIVE_CEILING = 22
HISTORICAL_SPENT = 16
# BS-003's five probes were made before the ledger existed, so no line records
# them. They were still spent, and the ceiling covers them, so the running total
# starts here rather than at zero. Counting only ledger lines would understate
# the true total by five and quietly hand back allowance that was already used.
PRE_LEDGER_ATTEMPTS = 5
# The one ledger this proof may use. A different file would start a second
# history and quietly hand back allowance that has already been spent.
CANONICAL_LEDGER = EVIDENCE_DIR / "probe-ledger.jsonl"
# Set by a Codex task that authorises a further real run. BS-003-R8 authorises
# at most six new localhost calls with exact token BS-003-R8-AGY.
AUTHORIZATION_ENV = "BS_LIVE_PROOF_AUTHORIZATION"

EXPECTED_FRAMEWORK = "strands"
EXPECTED_PROVIDER = "ollama"
EXPECTED_MODEL = "llama3.2:3b"
EXPECTED_PROVENANCE_KEYS = (
    "framework",
    "provider",
    "model",
    "inventory_tool_calls",
    "completed_at",
)
MAX_TOOL_CALLS = 2
# The stage a successful interpretation actually ends in. The adapter sets
# stage as it goes — inventory, then inventory_recovery if it was needed, then
# extraction — and never rewrites it afterwards, so extraction is the terminal
# value of a run that produced a draft. BS-003-R5 required "complete", which the
# adapter has never set: every real success would have failed this proof, and
# only the hand-written fake responses in the offline tests said otherwise.
# These names are the adapter's own diagnostic vocabulary, not a public schema.
TERMINAL_SUCCESS_STAGE = "extraction"
# The collections a before/after comparison relies on. A snapshot without them
# is not a baseline, so a case cannot proceed on one.
REQUIRED_SNAPSHOT_KEYS = ("equipment", "requests", "loans", "events")
REQUIRED_DIAGNOSTIC_KEYS = (
    "correlation_id",
    "stage",
    "outcome",
    "reason",
    "sends",
    "tool_attempts",
    "tool_successes",
    "recovery_used",
    "cleanup",
    "elapsed_ms",
)


class Client:
    """Minimal cookie-aware JSON client."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self.last_diagnostic: dict[str, Any] | None = None
        self.last_headers: dict[str, str] = {}

    def call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 180.0,
    ) -> tuple[int, dict[str, Any]]:
        self.last_diagnostic = None
        self.last_headers = {}
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                self._extract_headers(response.headers)
                return int(response.status), dict(json.loads(response.read() or b"{}"))
        except urllib.error.HTTPError as error:
            self._extract_headers(error.headers)
            return int(error.code), dict(json.loads(error.read() or b"{}"))

    def _extract_headers(self, headers: object) -> None:
        if headers is None or not hasattr(headers, "items"):
            return
        self.last_headers = {str(k).lower(): str(v) for k, v in headers.items()}
        diag_header = self.last_headers.get("x-assistant-diagnostic")
        if diag_header:
            try:
                parsed = json.loads(diag_header)
                if isinstance(parsed, dict):
                    self.last_diagnostic = parsed
            except Exception:
                self.last_diagnostic = None


def _snapshot_problem(status: int, payload: dict[str, Any]) -> str | None:
    """Why this snapshot cannot serve as a read-only baseline, if it cannot.

    Returns None when it can. A non-200, or a body missing the collections the
    comparison relies on, means there is no baseline — not that the baseline is
    empty.
    """
    if status != 200:
        return f"the required pre-case snapshot returned HTTP {status}"
    for key in REQUIRED_SNAPSHOT_KEYS:
        if key not in payload:
            return f"the required pre-case snapshot has no {key!r}"
    return None


def _due_in(days: int) -> str:
    """A future timezone-aware timestamp with deliberately non-zero seconds."""
    moment = (datetime.now(UTC) + timedelta(days=days)).replace(second=37, microsecond=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _due_in_offset(days: int, *, hours: int = 5, minutes: int = 30) -> tuple[str, str]:
    """A future timezone-aware timestamp with an explicit offset, and its UTC representation."""
    target_utc = (datetime.now(UTC) + timedelta(days=days)).replace(second=37, microsecond=0)
    tz = timezone(timedelta(hours=hours, minutes=minutes))
    target_tz = target_utc.astimezone(tz)
    offset_str = target_tz.strftime(f"%Y-%m-%dT%H:%M:%S+{hours:02d}:{minutes:02d}")
    utc_str = target_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return offset_str, utc_str


def _cases() -> list[dict[str, Any]]:
    """Synthetic intake texts and the exact result each one must produce.

    No real person, contact detail or health fact appears in any of them.

    ``expect`` states the whole draft, not just the field the case is named
    after. A case that returns the right null for the wrong reason — because the
    model omitted everything — is a failure of what the case exists to show, and
    only stating every field catches that. ``pickup_location`` lists the
    verbatim spans of its own message that name the place; grounding accepts any
    exact copy of one, and both are equally correct answers.
    """
    explicit_due_1 = _due_in(7)
    offset_due_4, normalized_utc_4 = _due_in_offset(8, hours=5, minutes=30)
    return [
        {
            "id": "1-explicit-fields",
            "intent": "every field stated outright, including a full ISO timestamp",
            "text": (
                f"Meena R needs a wheelchair, pickup at the Velachery equipment room, "
                f"return by {explicit_due_1}."
            ),
            "expect": {
                "borrower_label": ["Meena R"],
                "equipment_kind": ["WHEELCHAIR"],
                "pickup_location": ["the Velachery equipment room", "Velachery equipment room"],
                "due_at": [explicit_due_1],
                "missing_fields": [],
            },
        },
        {
            "id": "2-relative-date",
            "intent": "relative date and no location: both null, the stated kind kept",
            "text": "Arun needs a walker, he will return it next Tuesday.",
            "expect": {
                "borrower_label": ["Arun"],
                "equipment_kind": ["WALKER"],
                "pickup_location": [None],
                "due_at": [None],
                "missing_fields": ["pickup_location", "due_at"],
            },
        },
        {
            "id": "3-ambiguous-kind",
            "intent": "two kinds named: equipment_kind null, everything else kept",
            "text": "Could be a wheelchair or crutches for Divya, at the Velachery room.",
            "expect": {
                "borrower_label": ["Divya"],
                "equipment_kind": [None],
                "pickup_location": ["the Velachery room", "Velachery room"],
                "due_at": [None],
                "missing_fields": ["equipment_kind", "due_at"],
            },
        },
        {
            "id": "4-offset-date-crutches",
            "intent": (
                "crutches and aware timestamp with +05:30 offset: "
                "CRUTCHES kept, date normalized to whole-second UTC"
            ),
            "text": (
                f"Kavin needs crutches, pickup at the Tambaram equipment room, "
                f"return by {offset_due_4}."
            ),
            "expect": {
                "borrower_label": ["Kavin"],
                "equipment_kind": ["CRUTCHES"],
                "pickup_location": ["the Tambaram equipment room", "Tambaram equipment room"],
                "due_at": [normalized_utc_4],
                "missing_fields": [],
            },
        },
        {
            "id": "5-synonym-walking-frame",
            "intent": "walking frame synonym for WALKER, relative date and no location null",
            "text": "Leela needs a walking frame and will return it tomorrow.",
            "expect": {
                "borrower_label": ["Leela"],
                "equipment_kind": ["WALKER"],
                "pickup_location": [None],
                "due_at": [None],
                "missing_fields": ["pickup_location", "due_at"],
            },
        },
        {
            "id": "6-distinct-kind-ambiguity",
            "intent": "distinct kinds walker or crutches named: equipment_kind null, location kept",
            "text": "A walker or crutches for Ravi, pickup at the Madurai equipment room.",
            "expect": {
                "borrower_label": ["Ravi"],
                "equipment_kind": [None],
                "pickup_location": ["the Madurai equipment room", "Madurai equipment room"],
                "due_at": [None],
                "missing_fields": ["equipment_kind", "due_at"],
            },
        },
    ]


def _check_case(
    case: dict[str, Any],
    status: int,
    payload: dict[str, Any],
    *,
    snapshot_unchanged: bool,
    diagnostic: dict[str, Any] | None = None,
    seen_correlation_ids: set[str] | None = None,
    snapshot_statuses: tuple[int, int] = (200, 200),
) -> list[str]:
    """Everything one case must satisfy. Pure, so it is tested offline.

    Returns every failure found in this case. The caller stops at the first
    case that returns any.
    """
    failures: list[str] = []
    name = case["id"]
    if status != 200:
        return [f"{name}: HTTP {status}"]

    if snapshot_statuses[0] != 200:
        failures.append(f"{name}: pre-request snapshot failed with HTTP {snapshot_statuses[0]}")
    if snapshot_statuses[1] != 200:
        failures.append(f"{name}: post-request snapshot failed with HTTP {snapshot_statuses[1]}")
    if snapshot_statuses == (200, 200) and not snapshot_unchanged:
        failures.append(f"{name}: the business snapshot changed during this interpretation")

    provenance = payload.get("provenance", {})
    if set(provenance) != set(EXPECTED_PROVENANCE_KEYS):
        failures.append(f"{name}: provenance keys {sorted(provenance)}")
    if provenance.get("framework") != EXPECTED_FRAMEWORK:
        failures.append(f"{name}: framework={provenance.get('framework')!r}")
    if provenance.get("provider") != EXPECTED_PROVIDER:
        failures.append(f"{name}: provider={provenance.get('provider')!r}")
    if provenance.get("model") != EXPECTED_MODEL:
        failures.append(f"{name}: model={provenance.get('model')!r}")
    tool_calls = provenance.get("inventory_tool_calls")
    if type(tool_calls) is not int or not (1 <= tool_calls <= MAX_TOOL_CALLS):
        failures.append(f"{name}: inventory_tool_calls={tool_calls!r}")
    completed_at = provenance.get("completed_at")
    if not isinstance(completed_at, str) or not completed_at.endswith("Z"):
        failures.append(f"{name}: completed_at={completed_at!r} not normalized ISO timestamp")
    else:
        try:
            datetime.strptime(completed_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            failures.append(
                f"{name}: completed_at={completed_at!r} is not a valid UTC ISO timestamp"
            )

    expect = case["expect"]
    draft = payload.get("draft", {})
    for field in ("borrower_label", "equipment_kind", "pickup_location", "due_at"):
        allowed = expect[field]
        actual = draft.get(field)
        if actual not in allowed:
            failures.append(f"{name}: {field}={actual!r}, expected one of {allowed!r}")

    due_at = draft.get("due_at")
    if due_at is not None:
        if not isinstance(due_at, str) or not due_at.endswith("Z"):
            failures.append(f"{name}: due_at={due_at!r} not normalized UTC ISO timestamp")
        else:
            try:
                datetime.strptime(due_at, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                failures.append(f"{name}: due_at={due_at!r} is not a valid UTC ISO timestamp")

    missing = payload.get("missing_fields")
    if missing != expect["missing_fields"]:
        failures.append(
            f"{name}: missing_fields={missing!r}, expected {expect['missing_fields']!r}"
        )

    if diagnostic is None:
        failures.append(f"{name}: missing X-Assistant-Diagnostic header")
    else:
        missing_diag_keys = [k for k in REQUIRED_DIAGNOSTIC_KEYS if k not in diagnostic]
        if missing_diag_keys:
            failures.append(f"{name}: missing diagnostic keys {missing_diag_keys}")
        if diagnostic.get("outcome") != "success":
            failures.append(f"{name}: diagnostic outcome={diagnostic.get('outcome')!r}")
        if diagnostic.get("cleanup") != "closed":
            failures.append(f"{name}: diagnostic cleanup={diagnostic.get('cleanup')!r}")
        if diagnostic.get("stage") != TERMINAL_SUCCESS_STAGE:
            failures.append(
                f"{name}: diagnostic stage={diagnostic.get('stage')!r}, "
                f"expected {TERMINAL_SUCCESS_STAGE!r}"
            )

        corr_id = diagnostic.get("correlation_id")
        if not isinstance(corr_id, str) or not corr_id.strip():
            failures.append(
                f"{name}: diagnostic correlation_id is empty or not a string: {corr_id!r}"
            )
        elif seen_correlation_ids is not None:
            if corr_id in seen_correlation_ids:
                failures.append(
                    f"{name}: duplicate/replayed diagnostic correlation_id: {corr_id!r}"
                )
            else:
                seen_correlation_ids.add(corr_id)

        sends = diagnostic.get("sends")
        if type(sends) is not int or not (1 <= sends <= 6):
            failures.append(
                f"{name}: diagnostic sends={sends!r} must be an integer between 1 and 6"
            )

        tool_attempts = diagnostic.get("tool_attempts")
        if type(tool_attempts) is not int or not (0 <= tool_attempts <= MAX_TOOL_CALLS):
            failures.append(
                f"{name}: diagnostic tool_attempts={tool_attempts!r} "
                f"must be an integer between 0 and {MAX_TOOL_CALLS}"
            )

        tool_successes = diagnostic.get("tool_successes")
        if type(tool_successes) is not int or not (0 <= tool_successes <= MAX_TOOL_CALLS):
            failures.append(
                f"{name}: diagnostic tool_successes={tool_successes!r} "
                f"must be an integer between 0 and {MAX_TOOL_CALLS}"
            )
        elif type(tool_attempts) is int and tool_successes > tool_attempts:
            failures.append(
                f"{name}: diagnostic tool_successes={tool_successes!r} "
                f"exceeds tool_attempts={tool_attempts!r}"
            )

        prov_tool_calls = provenance.get("inventory_tool_calls")
        if tool_successes != prov_tool_calls:
            failures.append(
                f"{name}: diagnostic tool_successes={tool_successes!r} "
                f"does not match provenance inventory_tool_calls={prov_tool_calls!r}"
            )

        recovery_used = diagnostic.get("recovery_used")
        if type(recovery_used) is not bool:
            failures.append(f"{name}: diagnostic recovery_used={recovery_used!r} must be a bool")

        elapsed_ms = diagnostic.get("elapsed_ms")
        if type(elapsed_ms) is not int or elapsed_ms < 0:
            failures.append(
                f"{name}: diagnostic elapsed_ms={elapsed_ms!r} must be a non-negative integer"
            )

    return failures


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_ledger(path: Path, payload: dict[str, Any]) -> None:
    """Append one line and force it to disk. Never rewrites earlier entries.

    The flush and fsync matter: a ``start`` record only proves an attempt was
    made if it survives the process dying between sending the request and
    reading the reply.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _get_implementation_commit(override: str | None = None) -> str:
    """Resolve the git commit hash for the frozen implementation."""
    if override and override.strip():
        return override.strip()
    env_commit = os.environ.get("BS_IMPLEMENTATION_COMMIT")
    if env_commit and env_commit.strip():
        return env_commit.strip()
    try:
        import subprocess

        output = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parent),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return output.strip()
    except Exception:
        return "unknown"


def _check_r8_history(path: Path) -> list[str]:
    """Check whether any R8 attempt in the ledger was failed, aborted, or incomplete.

    Historic attempts 1-11 (R2 and R3) are preserved and never rejected as R8 failures.
    Any failed, aborted, or incomplete R8 attempt closes the R8 allocation permanently.
    """
    if not path.is_file():
        return []

    r8_starts: dict[int, dict[str, Any]] = {}
    r8_ends: dict[int, dict[str, Any]] = {}
    errors: list[str] = []

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return [f"failed to read ledger for R8 history: {exc}"]

    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue

        attempt = entry.get("attempt")
        task_id = entry.get("task_id")
        label = str(entry.get("label", "")).lower()
        is_r8 = task_id == TASK_ID or (isinstance(attempt, int) and attempt >= 12) or "r8" in label

        if not is_r8:
            continue

        phase = entry.get("phase")
        if phase == "start" and isinstance(attempt, int):
            r8_starts[attempt] = entry
        elif phase == "end" and isinstance(attempt, int):
            r8_ends[attempt] = entry
            verdict = entry.get("verdict")
            case_failures = entry.get("case_failures")
            http_status = entry.get("http_status")
            if verdict in ("FAIL", "BLOCKED") or bool(case_failures) or http_status != 200:
                errors.append(
                    f"R8 attempt {attempt} ended with failure (verdict={verdict!r}, "
                    f"http_status={http_status}, failures={case_failures})"
                )

    for attempt in sorted(r8_starts.keys()):
        if attempt not in r8_ends:
            errors.append(f"R8 attempt {attempt} started but never completed (aborted)")

    return errors


def _ledger_start(
    path: Path,
    *,
    attempt: int,
    case: dict[str, Any],
    label: str,
    implementation_commit: str = "unknown",
) -> None:
    """Record that an attempt is about to be sent, *before* sending it.

    Allowance is consumed at this moment, not when a reply arrives. An attempt
    with a start and no matching end was still spent; counting only completions
    would silently under-report the budget.
    """
    _append_ledger(
        path,
        {
            "phase": "start",
            "attempt": attempt,
            "task_id": TASK_ID,
            "implementation_commit": implementation_commit,
            "case": case["id"],
            "intent": case["intent"],
            "synthetic_text": case["text"],
            "label": label,
            "at": _now(),
            "note": (
                "written before the request was sent; this attempt has consumed "
                "allowance whatever happens next"
            ),
        },
    )


def _ledger_end(
    path: Path,
    *,
    attempt: int,
    record: dict[str, Any],
    label: str,
    implementation_commit: str = "unknown",
) -> None:
    """Record how an attempt that had already been charged actually finished."""
    payload = dict(record)
    payload["phase"] = "end"
    payload["attempt"] = attempt
    payload["task_id"] = TASK_ID
    payload["implementation_commit"] = implementation_commit
    payload["label"] = label
    payload["at"] = _now()
    response = payload.get("response", {})
    provenance = response.get("provenance", {}) if isinstance(response, dict) else {}
    payload["inventory_tool_calls"] = provenance.get("inventory_tool_calls")
    diagnostic = record.get("diagnostic")
    if isinstance(diagnostic, dict):
        payload["diagnostic"] = diagnostic
    _append_ledger(path, payload)


def _consumed(path: Path) -> tuple[int, int, int, list[str]]:
    """Total attempts charged, how many never completed, next attempt id, and validation errors.

    Three kinds of history are counted. ``PRE_LEDGER_ATTEMPTS`` covers BS-003's
    probes, made before this ledger existed. Entries written before BS-003-R3
    carry no ``phase`` field; each is one completed attempt. From BS-003-R3 on,
    an attempt is one ``start`` line, whether or not an ``end`` line followed.

    Only start/end pairs can show incompleteness. The older forms were written
    after their reply arrived, so nothing about them can be recovered now; they
    are counted as attempts and never as incomplete.

    Attempt ids number the ledger's own lines and so continue from the last one
    written; they are not the same as the total, which also carries the
    pre-ledger probes.
    """
    if not path.is_file():
        return PRE_LEDGER_ATTEMPTS, 0, 1, [f"ledger file does not exist: {path}"]

    errors: list[str] = []
    starts: set[int] = set()
    ends: set[int] = set()
    legacy = 0

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return PRE_LEDGER_ATTEMPTS, 0, 1, [f"failed to read ledger file: {exc}"]

    for idx, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception as exc:
            errors.append(f"line {idx}: invalid JSON ({exc})")
            continue
        if not isinstance(entry, dict):
            errors.append(f"line {idx}: entry must be a JSON object")
            continue

        phase = entry.get("phase")
        if phase in ("start", "end"):
            attempt = entry.get("attempt")
            if type(attempt) is not int or attempt < 1:
                errors.append(
                    f"line {idx}: phase={phase!r} attempt={attempt!r} must be a positive integer"
                )
                continue

            if phase == "start":
                if attempt in starts:
                    errors.append(f"line {idx}: duplicate start for attempt {attempt}")
                starts.add(attempt)
            elif phase == "end":
                if attempt in ends:
                    errors.append(f"line {idx}: duplicate end for attempt {attempt}")
                if attempt not in starts:
                    errors.append(f"line {idx}: end for attempt {attempt} without matching start")
                ends.add(attempt)
        else:
            legacy += 1

    incomplete = len(starts - ends)
    ledger_attempts = legacy + len(starts)
    return (
        PRE_LEDGER_ATTEMPTS + ledger_attempts,
        incomplete,
        ledger_attempts + 1,
        errors,
    )


def _write_evidence(
    *,
    evidence_dir: Path,
    base_url: str,
    health: dict[str, Any],
    invocations: int,
    charged: int,
    ceiling: int,
    unchanged: bool | None,
    records: list[dict[str, Any]],
    failures: list[str],
    stopped_at: str | None = None,
    implementation_commit: str = "unknown",
) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    completed_cases = {str(r.get("case", "")).split("-")[0] for r in records}
    is_full_pass = (
        not failures
        and stopped_at is None
        and completed_cases == REQUIRED_CASES
        and len(records) == len(REQUIRED_CASES)
        and all(r.get("verdict") == "PASS" for r in records)
    )
    if is_full_pass:
        verdict = "PASS"
    elif not failures and stopped_at is None and len(records) > 0:
        verdict = "PARTIAL"
    else:
        verdict = "BLOCKED"

    evidence = {
        "task_id": TASK_ID,
        "implementation_commit": implementation_commit,
        "verdict": verdict,
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_url": base_url,
        "health": health,
        "real_inference_invocations": invocations,
        "attempts_charged_before_this_run": charged,
        "cumulative_attempts_charged": charged + invocations,
        "cumulative_ceiling": ceiling,
        "business_snapshot_unchanged": unchanged,
        # The case this run stopped at, or None if it ran to the end. A run that
        # stopped early has to say so and say where.
        "stopped_at_case": stopped_at,
        "cases": records,
        "failures": failures,
    }
    path = evidence_dir / EVIDENCE_FILENAME
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    return path


def main(
    base_url: str,
    selected: set[str],
    ledger: Path | None = None,
    label: str = "",
    ceiling: int = CUMULATIVE_CEILING,
    implementation_commit: str | None = None,
) -> int:
    # 0. Selection validation: reject empty or unknown selection BEFORE client/network creation
    if not selected:
        print("FAIL no cases selected. Valid cases are 1,2,3,4,5,6.")
        return EXIT_FAIL
    unknown = set(selected) - REQUIRED_CASES
    if unknown:
        print(
            f"FAIL unknown case(s) selected: {sorted(unknown)}. "
            f"Allowed cases: {sorted(REQUIRED_CASES)}."
        )
        return EXIT_FAIL

    # 1. Explicit future authorization check: fail closed if missing or not exact match
    auth = os.environ.get(AUTHORIZATION_ENV)
    if auth != REQUIRED_AUTHORIZATION:
        print(
            f"FAIL {AUTHORIZATION_ENV}={auth!r} is not valid. A real run requires exact "
            f"authorization {REQUIRED_AUTHORIZATION!r}."
        )
        return EXIT_FAIL

    # 2. Ceiling check: an arbitrarily raised ceiling cannot bypass controls
    if ceiling > CUMULATIVE_CEILING:
        print(
            f"FAIL ceiling {ceiling} exceeds approved cumulative ceiling of {CUMULATIVE_CEILING}. "
            "An arbitrarily raised ceiling cannot bypass controls."
        )
        return EXIT_FAIL

    # 3. Canonical ledger required: running without ledger cannot bypass controls
    if ledger is None:
        print("FAIL a ledger is required; omitting --ledger cannot bypass controls.")
        return EXIT_FAIL

    try:
        resolved_ledger = ledger.resolve()
        resolved_canonical = CANONICAL_LEDGER.resolve()
    except Exception as exc:
        print(f"FAIL resolving ledger path failed: {exc}")
        return EXIT_FAIL

    if resolved_ledger != resolved_canonical:
        print(
            f"FAIL ledger {ledger} is not canonical {CANONICAL_LEDGER}. "
            "Using an alternate ledger cannot bypass cumulative history."
        )
        return EXIT_FAIL

    if not ledger.is_file():
        print(f"FAIL ledger {ledger} does not exist. A valid canonical ledger is required.")
        return EXIT_FAIL

    charged, incomplete, next_attempt, errors = _consumed(ledger)
    if errors:
        print(f"FAIL ledger history is invalid: {'; '.join(errors)}")
        return EXIT_FAIL

    print(f"ok   ledger: {charged} attempts already charged, ceiling {ceiling}")
    if incomplete > 0:
        print(
            f"FAIL {incomplete} earlier attempt(s) started but never completed. "
            "Failing closed on incomplete history."
        )
        return EXIT_FAIL

    # 4. Enforce permanent closure on any failed, aborted, or incomplete R8 attempt
    r8_history_errors = _check_r8_history(ledger)
    if r8_history_errors:
        print(
            f"FAIL an earlier R8 attempt failed or was incomplete: {'; '.join(r8_history_errors)}. "
            "The BS-003-R8-AGY allocation is permanently closed."
        )
        return EXIT_FAIL

    commit_hash = _get_implementation_commit(implementation_commit)
    print(f"ok   implementation commit: {commit_hash}")

    all_cases = _cases()
    wanted = sum(1 for case in all_cases if case["id"].split("-")[0] in selected)
    if charged + wanted > ceiling:
        print(
            f"FAIL this run would charge {wanted} more attempts, "
            f"taking the total to {charged + wanted} past the ceiling of {ceiling}. "
            "Nothing was sent. The allowance is never restarted."
        )
        return EXIT_FAIL

    client = Client(base_url)

    status, health = client.call("GET", "/api/health")
    if status != 200 or health.get("agent_mode") != "strands_ollama":
        print(f"FAIL health not in assistant mode: {status} {health}")
        return EXIT_FAIL
    print(f"ok   health: {health}")

    status, _created = client.call("POST", "/api/workspaces", {})
    if status != 201:
        print(f"FAIL workspace: {status}")
        return EXIT_FAIL

    invocations = 0
    records: list[dict[str, Any]] = []
    failures: list[str] = []
    seen_correlation_ids: set[str] = set()

    for case in all_cases:
        if case["id"].split("-")[0] not in selected:
            continue
        # The pre-case snapshot is a prerequisite, not a datum to grade later.
        # Without a trustworthy "before" there is nothing for the "after" to be
        # compared against, so this case could never show that interpretation
        # wrote nothing — and a case that cannot show that must not spend
        # allowance or reach the model at all. Checked here, ahead of both the
        # ledger charge and the request.
        status_before, before_snapshot = client.call("GET", "/api/snapshot")
        prerequisite = _snapshot_problem(status_before, before_snapshot)
        if prerequisite is not None:
            failures.append(f"{case['id']}: {prerequisite}")
            _write_evidence(
                evidence_dir=EVIDENCE_DIR,
                base_url=base_url,
                health=health,
                invocations=invocations,
                charged=charged,
                ceiling=ceiling,
                # None, not True: with no case completed, read-only behaviour
                # was never established. Vacuous truth is not evidence.
                unchanged=(
                    all(record["snapshot_unchanged"] for record in records) if records else None
                ),
                records=records,
                failures=failures,
                stopped_at=str(case["id"]),
                implementation_commit=commit_hash,
            )
            print(f"\nSTOPPED before {case['id']}: {prerequisite}")
            print("No interpretation was requested and no attempt was charged for it.")
            print(f"cumulative attempts charged: {charged + invocations} of {ceiling}")
            return EXIT_FAIL

        invocations += 1
        attempt = next_attempt + invocations - 1

        # Charged before the request leaves
        _ledger_start(
            ledger,
            attempt=attempt,
            case=case,
            label=label,
            implementation_commit=commit_hash,
        )

        started = time.monotonic()
        status, payload = client.call("POST", "/api/intake/interpret", {"text": case["text"]})
        elapsed = round(time.monotonic() - started, 2)

        # Capture diagnostic immediately before any subsequent call resets client state!
        diagnostic = client.last_diagnostic

        # Per-case business snapshot AFTER the request
        status_after, after_snapshot = client.call("GET", "/api/snapshot")
        case_snapshot_unchanged = (
            status_before == 200 and status_after == 200 and before_snapshot == after_snapshot
        )

        case_failures = _check_case(
            case,
            status,
            payload,
            snapshot_unchanged=case_snapshot_unchanged,
            diagnostic=diagnostic,
            seen_correlation_ids=seen_correlation_ids,
            snapshot_statuses=(status_before, status_after),
        )

        record: dict[str, Any] = {
            "case": case["id"],
            "intent": case["intent"],
            "synthetic_text": case["text"],
            "http_status": status,
            "seconds": elapsed,
            "diagnostic": diagnostic,
            "response": payload,
            "snapshot_statuses": [status_before, status_after],
            "snapshot_unchanged": case_snapshot_unchanged,
            "verdict": "PASS" if not case_failures else "FAIL",
            "case_failures": case_failures,
        }
        records.append(record)

        # Record end in ledger immediately so failed attempts are preserved with verdict
        _ledger_end(
            ledger,
            attempt=attempt,
            record=record,
            label=label,
            implementation_commit=commit_hash,
        )

        print(f"\n--- {case['id']} ({elapsed}s, HTTP {status}) ---")
        print(json.dumps(payload, indent=2, sort_keys=True))

        if case_failures:
            failures.extend(case_failures)
            # Fail fast: write evidence immediately, do NOT send next case!
            evidence_path = _write_evidence(
                evidence_dir=EVIDENCE_DIR,
                base_url=base_url,
                health=health,
                invocations=invocations,
                charged=charged,
                ceiling=ceiling,
                unchanged=case_snapshot_unchanged,
                records=records,
                failures=failures,
                stopped_at=str(case["id"]),
                implementation_commit=commit_hash,
            )
            print(f"\nevidence written: {evidence_path}")
            print(f"real inference invocations this run: {invocations}")
            print(f"cumulative attempts charged: {charged + invocations} of {ceiling}")
            print("\nFAILURES:")
            for line in failures:
                print(f"  - {line}")
            print(
                f"\nSmoke proof failed at {case['id']}. "
                "Stopped immediately without sending next request."
            )
            return EXIT_FAIL

    evidence_path = _write_evidence(
        evidence_dir=EVIDENCE_DIR,
        base_url=base_url,
        health=health,
        invocations=invocations,
        charged=charged,
        ceiling=ceiling,
        unchanged=all(r.get("snapshot_unchanged", False) for r in records),
        records=records,
        failures=[],
        implementation_commit=commit_hash,
    )
    print(f"\nevidence written: {evidence_path}")
    print(f"real inference invocations this run: {invocations}")
    print(f"cumulative attempts charged: {charged + invocations} of {ceiling}")

    completed_cases = {str(r.get("case", "")).split("-")[0] for r in records}
    is_full_pass = completed_cases == REQUIRED_CASES and len(records) == len(REQUIRED_CASES)

    if is_full_pass:
        print("\nassistant live proof passed")
        return EXIT_PASS

    print(f"\nassistant partial run ({len(records)} of {len(REQUIRED_CASES)} cases completed)")
    return EXIT_PARTIAL


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", nargs="?", default=DEFAULT_BASE_URL)
    parser.add_argument("--cases", default="1,2,3,4,5,6", help="comma-separated case numbers")
    parser.add_argument(
        "--ledger",
        default=str(CANONICAL_LEDGER),
        help="append-only JSONL probe ledger (defaults to canonical ledger)",
    )
    parser.add_argument("--label", default="", help="label recorded in the ledger")
    parser.add_argument(
        "--ceiling",
        type=int,
        default=CUMULATIVE_CEILING,
        help="refuse to run if the ledger's total would pass this many attempts",
    )
    parser.add_argument(
        "--implementation-commit",
        default="",
        help="git commit hash of the frozen implementation (defaults to git rev-parse HEAD)",
    )
    args = parser.parse_args()
    raise SystemExit(
        main(
            args.base_url,
            {c.strip() for c in args.cases.split(",") if c.strip()},
            Path(args.ledger) if args.ledger else None,
            args.label,
            args.ceiling,
            args.implementation_commit,
        )
    )
