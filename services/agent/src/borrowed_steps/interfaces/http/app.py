"""FastAPI adapter implementing the frozen M1 API contract.

This layer only translates. It resolves the session cookie, checks the request
origin, enforces the idempotency header, maps framework types onto use cases and
serialises the exact response shapes. No workflow decision is taken here, and no
model, provider or network call takes part in any request.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Response
from fastapi import Request as HttpRequest
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from borrowed_steps.application.errors import IdempotencyConflictError
from borrowed_steps.application.ports import (
    Clock,
    IdempotencyRecord,
    IdGenerator,
    Session,
    Snapshot,
    WorkspaceUnitOfWork,
)
from borrowed_steps.application.use_cases import (
    SESSION_TTL,
    Services,
    create_request,
    inspect_item,
    pick_up_loan,
    read_snapshot,
    reserve_equipment,
    resolve_session,
    return_loan,
    start_workspace,
)
from borrowed_steps.config import Settings, load_settings
from borrowed_steps.domain.errors import CodedError, ValidationFailedError
from borrowed_steps.domain.models import Equipment, EquipmentState, Event, Loan, Request
from borrowed_steps.infrastructure.sqlite_store import SqliteStore
from borrowed_steps.infrastructure.system import SecretsIdGenerator, SystemClock
from borrowed_steps.interfaces.http.schemas import (
    CreateRequestBody,
    InspectionBody,
    ReservationBody,
    TransitionBody,
    WorkspaceBody,
)
from borrowed_steps.isotime import normalise, to_iso

__all__ = ["AGENT_MODE", "MILESTONE", "SESSION_COOKIE", "OriginForbiddenError", "create_app"]

SESSION_COOKIE = "bs_session"
IDEMPOTENCY_HEADER = "Idempotency-Key"
MILESTONE = "M1"
AGENT_MODE = "not_implemented"

_IDEMPOTENCY_KEY_MIN = 8
_IDEMPOTENCY_KEY_MAX = 100

_GENERIC_FAILURE = "The request could not be completed."

_LOGGER = logging.getLogger("borrowed_steps.http")


class OriginForbiddenError(CodedError):
    """The request carried an Origin header that is not the configured frontend."""

    code = "ORIGIN_FORBIDDEN"


_STATUS_BY_CODE: dict[str, int] = {
    "SESSION_REQUIRED": 401,
    "ORIGIN_FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "STATE_CONFLICT": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "VALIDATION_ERROR": 422,
    "APPROVAL_REQUIRED": 422,
}

_CODE_BY_STATUS: dict[int, str] = {
    401: "SESSION_REQUIRED",
    403: "ORIGIN_FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "STATE_CONFLICT",
    422: "VALIDATION_ERROR",
}


def _serialise(payload: dict[str, Any]) -> str:
    """Canonical JSON for one response body.

    A mutation is serialised exactly once, stored, and that same text is served
    both on the first response and on every idempotent replay, so the two are
    identical byte for byte rather than merely equal once decoded.
    """
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _stored_response(status_code: int, body: str) -> Response:
    """Serve a stored response body verbatim."""
    return Response(
        content=body.encode("utf-8"),
        status_code=status_code,
        media_type="application/json",
    )


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


def _equipment_json(item: Equipment) -> dict[str, Any]:
    return {
        "id": item.id,
        "label": item.label,
        "kind": item.kind.value,
        "state": item.state.value,
        "version": item.version,
    }


def _request_json(request: Request) -> dict[str, Any]:
    return {
        "id": request.id,
        "borrower_label": request.borrower_label,
        "equipment_kind": request.equipment_kind.value,
        "pickup_location": request.pickup_location,
        "due_at": to_iso(request.due_at),
        "status": request.status.value,
        "created_at": to_iso(request.created_at),
    }


def _loan_json(loan: Loan) -> dict[str, Any]:
    return {
        "id": loan.id,
        "request_id": loan.request_id,
        "equipment_id": loan.equipment_id,
        "status": loan.status.value,
        "due_at": to_iso(loan.due_at),
        "created_at": to_iso(loan.created_at),
    }


def _event_json(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "entity_type": event.entity_type.value,
        "entity_id": event.entity_id,
        "action": event.action.value,
        "at": to_iso(event.at),
    }


def _snapshot_json(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "equipment": [_equipment_json(item) for item in snapshot.equipment],
        "requests": [_request_json(item) for item in snapshot.requests],
        "loans": [_loan_json(item) for item in snapshot.loans],
        "events": [_event_json(item) for item in snapshot.events],
        "agent_mode": AGENT_MODE,
    }


def _digest(body: BaseModel) -> str:
    """Hash the canonical parsed input, so equal payloads hash equally."""
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
) -> FastAPI:
    """Build the ASGI application. Opens (and migrates) the SQLite file."""
    resolved = settings if settings is not None else load_settings()
    store = SqliteStore(resolved.db_path)
    services = Services(
        store=store,
        clock=clock if clock is not None else SystemClock(),
        ids=ids if ids is not None else SecretsIdGenerator(),
    )

    app = FastAPI(
        title="Borrowed Steps agent service",
        version=MILESTONE,
        description=(
            "M1 local persisted equipment-loan workflow. "
            "Humans decide allocation and inspection; no agent inference runs here."
        ),
    )
    app.state.settings = resolved
    app.state.services = services

    def check_origin(http_request: HttpRequest) -> None:
        origin = http_request.headers.get("origin")
        if origin is not None and origin not in resolved.allowed_origins:
            msg = "This origin is not allowed to submit changes."
            raise OriginForbiddenError(msg)

    def require_session(http_request: HttpRequest) -> Session:
        return resolve_session(services, http_request.cookies.get(SESSION_COOKIE))

    def require_idempotency_key(http_request: HttpRequest) -> str:
        key = http_request.headers.get(IDEMPOTENCY_HEADER)
        if key is None or not (_IDEMPOTENCY_KEY_MIN <= len(key) <= _IDEMPOTENCY_KEY_MAX):
            msg = "An Idempotency-Key header of 8 to 100 characters is required."
            raise ValidationFailedError(msg)
        return key

    def run_mutation(
        session: Session,
        key: str,
        route: str,
        digest: str,
        action: Callable[[WorkspaceUnitOfWork], dict[str, Any]],
    ) -> Response:
        """Apply one mutation exactly once, atomically, inside the workspace.

        The response body is serialised once and stored alongside the effect in
        the same transaction, then served from that stored text on both the
        first response and any replay.
        """
        with store.transaction(session.workspace_id) as uow:
            stored = uow.get_idempotency(key)
            if stored is not None:
                if stored.route != route or stored.request_hash != digest:
                    msg = "This idempotency key was already used for a different request."
                    raise IdempotencyConflictError(msg)
                return _stored_response(stored.status_code, stored.response_body)
            body = _serialise(action(uow))
            uow.save_idempotency(
                IdempotencyRecord(
                    key=key,
                    route=route,
                    request_hash=digest,
                    status_code=200,
                    response_body=body,
                )
            )
        return _stored_response(200, body)

    @app.exception_handler(CodedError)
    async def _coded_error(_: HttpRequest, exc: Exception) -> JSONResponse:
        error = exc if isinstance(exc, CodedError) else CodedError(_GENERIC_FAILURE)
        return _error(_STATUS_BY_CODE.get(error.code, 500), error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def _schema_error(_: HttpRequest, __: Exception) -> JSONResponse:
        return _error(422, "VALIDATION_ERROR", "The request payload failed validation.")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: HttpRequest, exc: Exception) -> JSONResponse:
        status_code = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
        return _error(
            status_code,
            _CODE_BY_STATUS.get(status_code, "REQUEST_FAILED"),
            _GENERIC_FAILURE,
        )

    @app.exception_handler(Exception)
    async def _unexpected_error(http_request: HttpRequest, exc: Exception) -> JSONResponse:
        """Never let an unexpected failure reach the client as a trace.

        The transaction has already rolled back by the time this runs, because
        the storage context manager unwinds on any exception. The detail stays
        in the server log; the client gets the frozen envelope and nothing else.
        """
        _LOGGER.error(
            "Unhandled error serving %s %s",
            http_request.method,
            http_request.url.path,
            exc_info=exc,
        )
        return _error(500, "INTERNAL_ERROR", _GENERIC_FAILURE)

    @app.get("/api/health")
    def health() -> JSONResponse:
        """Public liveness probe. Declares the milestone and the agent mode."""
        return JSONResponse({"status": "ok", "milestone": MILESTONE, "agent_mode": AGENT_MODE})

    @app.post("/api/workspaces", status_code=201)
    def post_workspace(http_request: HttpRequest, body: WorkspaceBody) -> JSONResponse:
        """Create an isolated synthetic workspace and issue its session cookie.

        The contract specifies a body of ``{}``. A missing body, ``null``, an
        array or any unknown field is refused before anything is created.
        """
        del body
        check_origin(http_request)
        session = start_workspace(services)
        with store.transaction(session.workspace_id, write=False) as uow:
            snapshot = read_snapshot(uow)
        response = JSONResponse(
            status_code=201,
            content={
                "workspace": {"id": session.workspace_id},
                "snapshot": _snapshot_json(snapshot),
            },
        )
        response.set_cookie(
            SESSION_COOKIE,
            session.id,
            max_age=int(SESSION_TTL.total_seconds()),
            httponly=True,
            samesite="lax",
            secure=resolved.cookie_secure,
            path="/",
        )
        return response

    @app.get("/api/snapshot")
    def get_snapshot(http_request: HttpRequest) -> JSONResponse:
        """Everything the caller's workspace can see."""
        session = require_session(http_request)
        with store.transaction(session.workspace_id, write=False) as uow:
            snapshot = read_snapshot(uow)
        return JSONResponse(_snapshot_json(snapshot))

    @app.post("/api/requests")
    def post_request(http_request: HttpRequest, body: CreateRequestBody) -> Response:
        """Record a structured borrower request."""
        check_origin(http_request)
        session = require_session(http_request)
        key = require_idempotency_key(http_request)

        def action(uow: WorkspaceUnitOfWork) -> dict[str, Any]:
            created = create_request(
                services,
                uow,
                borrower_label=body.borrower_label,
                equipment_kind=body.equipment_kind,
                pickup_location=body.pickup_location,
                due_at=normalise(body.due_at),
            )
            return {"request": _request_json(created)}

        return run_mutation(session, key, http_request.url.path, _digest(body), action)

    @app.post("/api/reservations")
    def post_reservation(http_request: HttpRequest, body: ReservationBody) -> Response:
        """A human allocates one available item to one open request."""
        check_origin(http_request)
        session = require_session(http_request)
        key = require_idempotency_key(http_request)

        def action(uow: WorkspaceUnitOfWork) -> dict[str, Any]:
            request, equipment, loan = reserve_equipment(
                services,
                uow,
                request_id=body.request_id,
                equipment_id=body.equipment_id,
                expected_equipment_version=body.expected_equipment_version,
                human_approved=body.human_approved,
            )
            return {
                "request": _request_json(request),
                "equipment": _equipment_json(equipment),
                "loan": _loan_json(loan),
            }

        return run_mutation(session, key, http_request.url.path, _digest(body), action)

    @app.post("/api/loans/{loan_id}/pickup")
    def post_pickup(loan_id: str, http_request: HttpRequest, body: TransitionBody) -> Response:
        """A human confirms the borrower collected the item."""
        check_origin(http_request)
        session = require_session(http_request)
        key = require_idempotency_key(http_request)

        def action(uow: WorkspaceUnitOfWork) -> dict[str, Any]:
            request, equipment, loan = pick_up_loan(
                services,
                uow,
                loan_id=loan_id,
                expected_equipment_version=body.expected_equipment_version,
                human_approved=body.human_approved,
            )
            return {
                "request": _request_json(request),
                "equipment": _equipment_json(equipment),
                "loan": _loan_json(loan),
            }

        return run_mutation(session, key, http_request.url.path, _digest(body), action)

    @app.post("/api/loans/{loan_id}/return")
    def post_return(loan_id: str, http_request: HttpRequest, body: TransitionBody) -> Response:
        """A human takes the item back. It stays unavailable until inspected."""
        check_origin(http_request)
        session = require_session(http_request)
        key = require_idempotency_key(http_request)

        def action(uow: WorkspaceUnitOfWork) -> dict[str, Any]:
            request, equipment, loan = return_loan(
                services,
                uow,
                loan_id=loan_id,
                expected_equipment_version=body.expected_equipment_version,
                human_approved=body.human_approved,
            )
            return {
                "request": _request_json(request),
                "equipment": _equipment_json(equipment),
                "loan": _loan_json(loan),
            }

        return run_mutation(session, key, http_request.url.path, _digest(body), action)

    @app.post("/api/equipment/{equipment_id}/inspection")
    def post_inspection(
        equipment_id: str, http_request: HttpRequest, body: InspectionBody
    ) -> Response:
        """A human records an inspection outcome, including release from quarantine."""
        check_origin(http_request)
        session = require_session(http_request)
        key = require_idempotency_key(http_request)

        def action(uow: WorkspaceUnitOfWork) -> dict[str, Any]:
            equipment, request, loan = inspect_item(
                services,
                uow,
                equipment_id=equipment_id,
                outcome=EquipmentState(body.outcome),
                expected_equipment_version=body.expected_equipment_version,
                human_approved=body.human_approved,
            )
            return {
                "equipment": _equipment_json(equipment),
                "request": None if request is None else _request_json(request),
                "loan": None if loan is None else _loan_json(loan),
            }

        return run_mutation(session, key, http_request.url.path, _digest(body), action)

    return app
