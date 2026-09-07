"""Shared helpers for the HTTP tests. Synthetic data only."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

ALLOWED_ORIGIN = "http://127.0.0.1:5173"
FOREIGN_ORIGIN = "http://evil.example"
NOW = datetime(2026, 9, 7, 9, 0, 0, tzinfo=UTC)
DUE_AT = "2026-09-14T09:00:00Z"
PICKUP_LOCATION = "Equipment room, 2nd Cross Street, Velachery"


def key(label: str = "key") -> str:
    """A fresh idempotency key of at least eight characters."""
    return f"{label}-{uuid4().hex}"


def body(response: httpx.Response) -> dict[str, Any]:
    """The decoded JSON object of a response."""
    return dict(response.json())


def error_code(response: httpx.Response) -> str:
    """Assert the frozen error envelope and return its code.

    Also proves no framework trace or extra field leaks out.
    """
    payload = body(response)
    assert set(payload) == {"error"}, payload
    assert set(payload["error"]) == {"code", "message"}, payload
    return str(payload["error"]["code"])


def start_workspace(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/workspaces", json={})
    assert response.status_code == 201, response.text
    return body(response)


def snapshot(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/snapshot")
    assert response.status_code == 200, response.text
    return body(response)


def equipment_of(snap: dict[str, Any], kind: str) -> dict[str, Any]:
    for item in snap["equipment"]:
        if item["kind"] == kind:
            return dict(item)
    msg = f"no {kind} in this workspace"
    raise AssertionError(msg)


def create_request(
    client: TestClient,
    *,
    kind: str = "WHEELCHAIR",
    borrower_label: str = "Borrower A",
    pickup_location: str = PICKUP_LOCATION,
    due_at: str = DUE_AT,
    idempotency_key: str | None = None,
) -> httpx.Response:
    response: httpx.Response = client.post(
        "/api/requests",
        json={
            "borrower_label": borrower_label,
            "equipment_kind": kind,
            "pickup_location": pickup_location,
            "due_at": due_at,
        },
        headers={"Idempotency-Key": idempotency_key or key("request")},
    )
    return response


def reserve(
    client: TestClient,
    *,
    request_id: str,
    equipment_id: str,
    expected_version: int,
    human_approved: bool | None = True,
    idempotency_key: str | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {
        "request_id": request_id,
        "equipment_id": equipment_id,
        "expected_equipment_version": expected_version,
    }
    if human_approved is not None:
        payload["human_approved"] = human_approved
    response: httpx.Response = client.post(
        "/api/reservations",
        json=payload,
        headers={"Idempotency-Key": idempotency_key or key("reserve")},
    )
    return response


def transition(
    client: TestClient,
    *,
    loan_id: str,
    action: str,
    expected_version: int,
    human_approved: bool | None = True,
    idempotency_key: str | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {"expected_equipment_version": expected_version}
    if human_approved is not None:
        payload["human_approved"] = human_approved
    response: httpx.Response = client.post(
        f"/api/loans/{loan_id}/{action}",
        json=payload,
        headers={"Idempotency-Key": idempotency_key or key(action)},
    )
    return response


def inspect(
    client: TestClient,
    *,
    equipment_id: str,
    outcome: str,
    expected_version: int,
    human_approved: bool | None = True,
    idempotency_key: str | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {
        "expected_equipment_version": expected_version,
        "outcome": outcome,
    }
    if human_approved is not None:
        payload["human_approved"] = human_approved
    response: httpx.Response = client.post(
        f"/api/equipment/{equipment_id}/inspection",
        json=payload,
        headers={"Idempotency-Key": idempotency_key or key("inspect")},
    )
    return response
