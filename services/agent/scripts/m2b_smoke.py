"""Local M2B smoke: a real server, a real database, no browser and no model.

What this proves, and nothing more:

* A real uvicorn process on loopback, with the assistant disabled, writes
  coordination tasks through the real HTTP routes into a real SQLite file.
* The owned runner inside that process moves those tasks to DUE and writes the
  due events **while no HTTP request is being made** - the script reads the
  database file directly during those waits, so nothing a browser does can be
  mistaken for the runner's work.
* The server stops, and its process really exits.

What it does not prove: anything about a model. ``BS_ASSISTANT_ENABLED`` is
forced to false here and no inference happens. It is also not a browser test.

Every wait is bounded and the whole run has a deadline, so a regression fails
this script rather than hanging it. The database is disposable and is deleted
afterwards; only the JSON summary is kept.

Run from ``services/agent``::

    rtk .venv/Scripts/python.exe scripts/m2b_smoke.py
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "test-evidence" / "m2b"
RUN_DEADLINE_SECONDS = 300.0
BOOT_TIMEOUT_SECONDS = 40.0
# The runner ticks every 30 s, so a deadline set 40 s out is reached by a tick
# within roughly 70 s. Waits are bounded well above that and never below it.
RETURN_DUE_IN_SECONDS = 40
PROCESSING_TIMEOUT_SECONDS = 150.0
SHUTDOWN_TIMEOUT_SECONDS = 30.0

PICKUP_LOCATION = "Equipment room, 2nd Cross Street, Velachery"


class SmokeFailureError(Exception):
    """A required step did not happen. The run stops here."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db_tasks(db_path: Path) -> list[tuple[str, str, str]]:
    """(kind, status, due_at) read straight from the file, with no HTTP."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT kind, status, due_at FROM tasks ORDER BY kind").fetchall()
    finally:
        conn.close()
    return [(str(a), str(b), str(c)) for a, b, c in rows]


def _db_actions(db_path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT action FROM events ORDER BY rowid").fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


def _wait_for_status(
    db_path: Path, kind: str, status: str, timeout: float, deadline: float
) -> float:
    """Poll the database file until ``kind`` reaches ``status``.

    Deliberately reads the file rather than the API: this is the window in
    which the runner is supposed to work with nobody looking.
    """
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if time.monotonic() > deadline:
            msg = "the run deadline passed while waiting for the runner"
            raise SmokeFailureError(msg)
        for row_kind, row_status, _ in _db_tasks(db_path):
            if row_kind == kind and row_status == status:
                return round(time.monotonic() - started, 2)
        time.sleep(0.5)
    msg = f"{kind} never reached {status} within {timeout:.0f}s; tasks={_db_tasks(db_path)}"
    raise SmokeFailureError(msg)


def _start_server(db_path: Path, port: int, log_path: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env.update(
        {
            "BS_DB_PATH": str(db_path),
            # Explicit, not inherited: this run makes no model call at all.
            "BS_ASSISTANT_ENABLED": "false",
            "BS_TASKS_ENABLED": "true",
            "BS_LOG_LEVEL": "INFO",
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
        }
    )
    handle = log_path.open("wb")
    # Its own process group, so this script can ask for a graceful shutdown
    # rather than killing the process. A killed process never runs the lifespan
    # exit, which would leave the runner's stop path unproven.
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    # Fixed argv, no shell, loopback only.
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "borrowed_steps.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "info",
        ],
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )


def _shut_down(server: subprocess.Popen[bytes]) -> tuple[str, bool]:
    """Ask the server to stop the way an operator would. Report what happened.

    Returns the outcome and whether the stop was graceful. A process that had
    to be killed is reported as killed - never described as a clean shutdown.
    """
    interrupt = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
    try:
        server.send_signal(interrupt)
    except (OSError, ValueError):
        server.terminate()
    try:
        server.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        return "the server ignored the interrupt and had to be killed", False
    return f"the server exited on its own with code {server.returncode}", True


def _await_health(client: httpx.Client, deadline: float) -> dict[str, Any]:
    started = time.monotonic()
    last: Exception | None = None
    while time.monotonic() - started < BOOT_TIMEOUT_SECONDS:
        if time.monotonic() > deadline:
            msg = "the run deadline passed before the server answered"
            raise SmokeFailureError(msg)
        try:
            response = client.get("/api/health", timeout=5.0)
        except httpx.HTTPError as error:  # server not listening yet
            last = error
        else:
            if response.status_code == 200:
                return dict(response.json())
        time.sleep(0.3)
    msg = f"the server never became healthy: {last}"
    raise SmokeFailureError(msg)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailureError(message)


def main() -> int:
    deadline = time.monotonic() + RUN_DEADLINE_SECONDS
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="bs-m2b-smoke-"))
    db_path = workspace / "smoke.db"
    log_path = workspace / "server.log"
    port = _free_port()
    record: dict[str, Any] = {
        "task_id": "BS-007",
        "started_at": _iso(datetime.now(UTC)),
        "port": port,
        "assistant_enabled": False,
        "tasks_enabled": True,
        "real_inference_invocations": 0,
        "steps": [],
    }
    server: subprocess.Popen[bytes] | None = None
    verdict = "FAILED"

    try:
        server = _start_server(db_path, port, log_path)
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base) as client:
            health = _await_health(client, deadline)
            record["health"] = health
            _require(health.get("milestone") == "M2B", f"unexpected milestone: {health}")
            _require(
                health.get("agent_mode") == "disabled",
                f"the assistant must be disabled for this run: {health}",
            )
            record["steps"].append("server healthy, assistant disabled")

            created = client.post("/api/workspaces", json={})
            _require(created.status_code == 201, f"workspace creation: {created.status_code}")
            snapshot = created.json()["snapshot"]
            _require(snapshot["tasks"] == [], "a fresh workspace must have no tasks")
            record["steps"].append("fresh workspace has tasks: []")

            wheelchair = next(
                item for item in snapshot["equipment"] if item["kind"] == "WHEELCHAIR"
            )
            due_at = _iso(datetime.now(UTC) + timedelta(seconds=RETURN_DUE_IN_SECONDS))
            record["loan_due_at"] = due_at

            made = client.post(
                "/api/requests",
                json={
                    "borrower_label": "Borrower A",
                    "equipment_kind": "WHEELCHAIR",
                    "pickup_location": PICKUP_LOCATION,
                    "due_at": due_at,
                },
                headers={"Idempotency-Key": "m2b-smoke-request-1"},
            )
            _require(made.status_code == 200, f"request creation: {made.text}")
            request_id = made.json()["request"]["id"]

            reserved = client.post(
                "/api/reservations",
                json={
                    "request_id": request_id,
                    "equipment_id": wheelchair["id"],
                    "expected_equipment_version": wheelchair["version"],
                    "human_approved": True,
                },
                headers={"Idempotency-Key": "m2b-smoke-reserve-1"},
            )
            _require(reserved.status_code == 200, f"reservation: {reserved.text}")
            loan = reserved.json()["loan"]
            record["steps"].append("human-approved reservation created over HTTP")

            # From here to the assertion below, no HTTP call is made. Anything
            # that changes in the database is the runner's own work.
            waited = _wait_for_status(
                db_path, "PICKUP_DUE", "DUE", PROCESSING_TIMEOUT_SECONDS, deadline
            )
            record["pickup_due_after_seconds"] = waited
            record["steps"].append(
                f"runner marked PICKUP_DUE with no HTTP request in flight ({waited}s)"
            )
            _require(
                "PICKUP_DUE" in _db_actions(db_path),
                "the due event was not written with the transition",
            )

            picked = client.post(
                f"/api/loans/{loan['id']}/pickup",
                json={
                    "expected_equipment_version": reserved.json()["equipment"]["version"],
                    "human_approved": True,
                },
                headers={"Idempotency-Key": "m2b-smoke-pickup-1"},
            )
            _require(picked.status_code == 200, f"pickup: {picked.text}")
            record["steps"].append("human recorded pickup; return notice opened")

            # Again no HTTP: the return deadline passes and a later tick, in the
            # running server, processes it.
            waited = _wait_for_status(
                db_path, "RETURN_DUE", "DUE", PROCESSING_TIMEOUT_SECONDS, deadline
            )
            record["return_due_after_seconds"] = waited
            record["steps"].append(
                f"runner marked RETURN_DUE with no HTTP request in flight ({waited}s)"
            )

            final_tasks = _db_tasks(db_path)
            record["tasks_at_end"] = final_tasks
            record["events_at_end"] = _db_actions(db_path)
            _require(
                ("PICKUP_DUE", "RESOLVED", loan["created_at"]) in final_tasks
                or any(k == "PICKUP_DUE" and s == "RESOLVED" for k, s, _ in final_tasks),
                f"the pickup notice should be resolved by the pickup: {final_tasks}",
            )
            _require(
                _db_actions(db_path).count("PICKUP_DUE") == 1,
                "exactly one pickup due event",
            )
            _require(
                _db_actions(db_path).count("RETURN_DUE") == 1,
                "exactly one return due event",
            )

            # One read at the end, to show the API serves what the runner wrote.
            served = client.get("/api/snapshot")
            _require(served.status_code == 200, f"snapshot: {served.status_code}")
            record["snapshot_tasks"] = served.json()["tasks"]
            _require(
                {t["status"] for t in served.json()["tasks"]} == {"RESOLVED", "DUE"},
                "the served snapshot must match the processed state",
            )
        verdict = "PASSED"
    except (SmokeFailureError, httpx.HTTPError) as error:
        record["failure"] = f"{type(error).__name__}: {error}"
    finally:
        if server is not None:
            outcome, graceful = _shut_down(server)
            record["shutdown"] = outcome
            record["shutdown_was_graceful"] = graceful
            if not graceful:
                verdict = "FAILED"
        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            record["server_log_tail"] = tail
            completed = any("Application shutdown complete" in line for line in tail)
            unclean = any("not clean" in line for line in tail)
            record["lifespan_shutdown_completed"] = completed
            record["shutdown_reported_unclean"] = unclean
            if verdict == "PASSED" and (not completed or unclean):
                # The runner's stop path is only proven when the lifespan
                # actually ran to completion and reported nothing left running.
                record["failure"] = (
                    "the lifespan did not complete cleanly; "
                    f"shutdown_complete={completed} reported_unclean={unclean}"
                )
                verdict = "FAILED"
        record["verdict"] = verdict
        record["finished_at"] = _iso(datetime.now(UTC))
        (EVIDENCE_DIR / "m2b-smoke.json").write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8"
        )
        # Disposable by design: the evidence is the JSON, not the database.
        for leftover in workspace.glob("*"):
            leftover.unlink(missing_ok=True)
        workspace.rmdir()

    print(json.dumps({"verdict": verdict, "steps": record["steps"]}, indent=2))
    if verdict != "PASSED":
        print(f"FAILED: {record.get('failure', 'see evidence')}", file=sys.stderr)
    return 0 if verdict == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
