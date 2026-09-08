"""Health and workflow smoke against a running server. Standard library only.

Usage::

    python scripts/smoke.py [base_url]

Exercises the real HTTP surface of an already-started server: health, workspace
creation, request, reservation, pickup, return, inspection and an idempotent
replay. Makes no external or inference call of any kind.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.cookiejar import CookieJar
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
LOAN_LENGTH = timedelta(days=7)


def due_at() -> str:
    """A due date one week from now, so this smoke stays valid over time."""
    return (datetime.now(UTC) + LOAN_LENGTH).strftime("%Y-%m-%dT%H:%M:%SZ")


class Client:
    """Minimal cookie-aware JSON client."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.last_bytes = b""
        self.last_content_type = ""
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if idempotency_key is not None:
            request.add_header("Idempotency-Key", idempotency_key)
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read() or b"{}"
                self.last_content_type = str(response.headers.get("Content-Type", ""))
                status = int(response.status)
        except urllib.error.HTTPError as error:
            raw = error.read() or b"{}"
            self.last_content_type = str(error.headers.get("Content-Type", ""))
            status = int(error.code)
        self.last_bytes = raw
        return status, dict(json.loads(raw))


def _check(label: str, actual: object, expected: object) -> None:
    status = "ok  " if actual == expected else "FAIL"
    print(f"{status} {label}: {actual!r}")
    if actual != expected:
        raise SystemExit(1)


def main(base_url: str) -> int:
    client = Client(base_url)

    status, health = client.call("GET", "/api/health")
    _check("health status", status, 200)
    _check(
        "health body",
        (health["status"], health["milestone"]),
        ("ok", "M2A"),
    )
    _check(
        "health agent_mode is a configured mode",
        health["agent_mode"] in {"disabled", "strands_ollama"},
        True,
    )

    status, created = client.call("POST", "/api/workspaces", {})
    _check("workspace created", status, 201)
    equipment = created["snapshot"]["equipment"]
    _check("seeded items", len(equipment), 3)
    wheelchair = next(item for item in equipment if item["kind"] == "WHEELCHAIR")

    status, request_payload = client.call(
        "POST",
        "/api/requests",
        {
            "borrower_label": "Smoke borrower",
            "equipment_kind": "WHEELCHAIR",
            "pickup_location": "Equipment room, Velachery",
            "due_at": due_at(),
        },
        idempotency_key="smoke-request-0001",
    )
    _check("request created", status, 200)
    request_id = request_payload["request"]["id"]

    status, reserved = client.call(
        "POST",
        "/api/reservations",
        {
            "request_id": request_id,
            "equipment_id": wheelchair["id"],
            "expected_equipment_version": wheelchair["version"],
            "human_approved": True,
        },
        idempotency_key="smoke-reserve-0001",
    )
    _check("reserved", status, 200)
    reserved_bytes = client.last_bytes
    _check("reserved content type", client.last_content_type, "application/json")
    loan_id = reserved["loan"]["id"]

    status, replay = client.call(
        "POST",
        "/api/reservations",
        {
            "request_id": request_id,
            "equipment_id": wheelchair["id"],
            "expected_equipment_version": wheelchair["version"],
            "human_approved": True,
        },
        idempotency_key="smoke-reserve-0001",
    )
    _check("idempotent replay", (status, replay == reserved), (200, True))
    _check("replay is the same bytes", client.last_bytes == reserved_bytes, True)

    status, picked = client.call(
        "POST",
        f"/api/loans/{loan_id}/pickup",
        {"expected_equipment_version": 2, "human_approved": True},
        idempotency_key="smoke-pickup-0001",
    )
    _check("picked up", (status, picked["equipment"]["state"]), (200, "ON_LOAN"))

    status, returned = client.call(
        "POST",
        f"/api/loans/{loan_id}/return",
        {"expected_equipment_version": 3, "human_approved": True},
        idempotency_key="smoke-return-0001",
    )
    _check("returned", (status, returned["equipment"]["state"]), (200, "AWAITING_INSPECTION"))

    status, inspected = client.call(
        "POST",
        f"/api/equipment/{wheelchair['id']}/inspection",
        {"expected_equipment_version": 4, "outcome": "AVAILABLE", "human_approved": True},
        idempotency_key="smoke-inspect-0001",
    )
    _check("inspected", (status, inspected["equipment"]["state"]), (200, "AVAILABLE"))
    _check("loan closed", inspected["loan"]["status"], "CLOSED")

    status, refused = client.call(
        "POST",
        "/api/reservations",
        {
            "request_id": request_id,
            "equipment_id": wheelchair["id"],
            "expected_equipment_version": 1,
            "human_approved": True,
        },
        idempotency_key="smoke-stale-0001",
    )
    _check("stale version refused", (status, refused["error"]["code"]), (409, "STATE_CONFLICT"))

    status, snapshot = client.call("GET", "/api/snapshot")
    _check("snapshot agent_mode matches health", snapshot["agent_mode"], health["agent_mode"])
    _check(
        "event history",
        [event["action"] for event in snapshot["events"]],
        ["INSPECTED_AVAILABLE", "RETURNED", "PICKED_UP", "RESERVED", "REQUEST_CREATED"],
    )

    print("smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE_URL))
