"""FastAPI adapter implementing the frozen M1 API contract.

This layer only translates. It resolves the session cookie, checks the request
origin, enforces the idempotency header, maps framework types onto use cases and
serialises the exact response shapes. No workflow decision is taken here, and no
model, provider or network call takes part in any request.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import threading
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from borrowed_steps.infrastructure.hosted_scheduler import HostedSchedulerService

from fastapi import Depends, FastAPI, Response
from fastapi import Request as HttpRequest
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from borrowed_steps.application.errors import (
    AssistantBusyError,
    AssistantDisabledError,
    AssistantTimeoutError,
    AssistantUnavailableError,
    IdempotencyConflictError,
)
from borrowed_steps.application.interpreter import (
    CleanupOwner,
    Interpretation,
    RequestInterpreter,
)
from borrowed_steps.application.ports import (
    Clock,
    IdempotencyRecord,
    IdGenerator,
    Session,
    Snapshot,
    Store,
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
from borrowed_steps.config import (
    ASSISTANT_CANCEL_GRACE_SECONDS,
    ASSISTANT_DEADLINE_SECONDS,
    ASSISTANT_SHUTDOWN_SECONDS,
    TASK_STOP_TIMEOUT_SECONDS,
    TASK_TICK_CANDIDATE_LIMIT,
    TASK_TICK_SECONDS,
    Settings,
    load_settings,
    validate_settings,
)
from borrowed_steps.domain.errors import CodedError, ValidationFailedError
from borrowed_steps.domain.models import (
    CoordinationTask,
    Equipment,
    EquipmentState,
    Event,
    Loan,
    Request,
)
from borrowed_steps.infrastructure.inventory import StoreInventoryReader
from borrowed_steps.infrastructure.sqlite_store import SqliteStore
from borrowed_steps.infrastructure.system import SecretsIdGenerator, SystemClock
from borrowed_steps.infrastructure.task_runner import TaskRunner
from borrowed_steps.interfaces.http.inference_slot import InferenceRegistry, InferenceSlot
from borrowed_steps.interfaces.http.schemas import (
    CreateRequestBody,
    InspectionBody,
    InterpretBody,
    ReservationBody,
    TransitionBody,
    WorkspaceBody,
)
from borrowed_steps.isotime import normalise, to_iso

__all__ = [
    "AGENT_MODE_DISABLED",
    "AGENT_MODE_STRANDS_OLLAMA",
    "MILESTONE",
    "MILESTONE_HOSTED",
    "MILESTONE_LOCAL",
    "SESSION_COOKIE",
    "OriginForbiddenError",
    "create_app",
]

SESSION_COOKIE = "bs_session"
IDEMPOTENCY_HEADER = "Idempotency-Key"
MILESTONE_LOCAL = "M2B"
MILESTONE_HOSTED = "M3"
MILESTONE = MILESTONE_LOCAL
AGENT_MODE_DISABLED = "disabled"
AGENT_MODE_STRANDS_OLLAMA = "strands_ollama"

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
    "ASSISTANT_BUSY": 429,
    "VALIDATION_ERROR": 422,
    "APPROVAL_REQUIRED": 422,
    "ASSISTANT_INVALID_OUTPUT": 502,
    "ASSISTANT_DISABLED": 503,
    "ASSISTANT_UNAVAILABLE": 503,
    "ASSISTANT_TIMEOUT": 504,
}

_CODE_BY_STATUS: dict[int, str] = {
    401: "SESSION_REQUIRED",
    403: "ORIGIN_FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "STATE_CONFLICT",
    422: "VALIDATION_ERROR",
    503: "SERVICE_UNAVAILABLE",
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


def _error(
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=headers,
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


def _task_json(task: CoordinationTask) -> dict[str, Any]:
    """The six public fields of a coordination task.

    ``workspace_id`` is deliberately absent: it is a storage concern, and a
    response that carried it would tell one caller something about the shape of
    everyone else's data.
    """
    return {
        "id": task.id,
        "loan_id": task.loan_id,
        "kind": task.kind.value,
        "status": task.status.value,
        "due_at": to_iso(task.due_at),
        "created_at": to_iso(task.created_at),
    }


def _snapshot_json(snapshot: Snapshot, agent_mode: str) -> dict[str, Any]:
    return {
        "equipment": [_equipment_json(item) for item in snapshot.equipment],
        "requests": [_request_json(item) for item in snapshot.requests],
        "loans": [_loan_json(item) for item in snapshot.loans],
        "events": [_event_json(item) for item in snapshot.events],
        "tasks": [_task_json(item) for item in snapshot.tasks],
        "agent_mode": agent_mode,
    }


def _interpretation_json(interpretation: Interpretation) -> dict[str, Any]:
    """The exact M2A response shape. No prose, score, priority or approval field."""
    draft = interpretation.draft
    return {
        "draft": {
            "borrower_label": draft.borrower_label,
            "equipment_kind": None if draft.equipment_kind is None else draft.equipment_kind.value,
            "pickup_location": draft.pickup_location,
            "due_at": None if draft.due_at is None else to_iso(draft.due_at),
        },
        "missing_fields": list(interpretation.missing_fields),
        "provenance": {
            "framework": interpretation.framework,
            "provider": interpretation.provider,
            "model": interpretation.model,
            "inventory_tool_calls": interpretation.inventory_tool_calls,
            "completed_at": to_iso(interpretation.completed_at),
        },
    }


def _real_interpreter(settings: Settings, clock: Clock) -> RequestInterpreter:
    """Build the local Strands adapter.

    Imported here rather than at module scope so the structured workflow starts
    and runs with no provider SDK installed at all when the assistant is off.
    """
    from borrowed_steps.infrastructure.strands_interpreter import StrandsOllamaInterpreter

    return StrandsOllamaInterpreter(
        host=settings.assistant_host,
        model_id=settings.assistant_model,
        clock=clock,
    )


async def _settle(task: asyncio.Task[Any], grace: float) -> None:
    """Wait, bounded, for a cancelled run to finish winding down.

    Returns as soon as the run ends, and gives up quietly when the grace expires
    so a run that will not stop cannot hold the response open. Giving up is not
    a failure here: the slot is released by the run's own done callback, so a
    run still winding down simply keeps holding it.
    """
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=grace)
    except asyncio.CancelledError:
        if not task.done():
            # Not the run's cancellation but our own: the caller left too.
            raise
    except Exception:  # the run's own failure, already reported by its telemetry
        return


def _digest(body: BaseModel) -> str:
    """Hash the canonical parsed input, so equal payloads hash equally."""
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
    interpreter: RequestInterpreter | None = None,
    assistant_deadline_seconds: float = ASSISTANT_DEADLINE_SECONDS,
    assistant_shutdown_seconds: float = ASSISTANT_SHUTDOWN_SECONDS,
    assistant_cancel_grace_seconds: float = ASSISTANT_CANCEL_GRACE_SECONDS,
) -> FastAPI:
    """Build the ASGI application. Opens (and migrates) the SQLite file.

    ``interpreter`` is a test seam. Left unset with the assistant enabled, the
    real local Strands adapter is built; with the assistant disabled no provider
    module is imported at all.
    """
    resolved = settings if settings is not None else load_settings()
    validate_settings(resolved)

    store: Store
    runner: TaskRunner | None = None
    assistant: RequestInterpreter | None = None
    scheduler: HostedSchedulerService | None = None
    if resolved.runtime == "hosted":
        if resolved.database_url is None:
            raise ValueError("Hosted runtime requires an explicit database_url.")
        from borrowed_steps.infrastructure.postgres_store import PostgresStore

        store = PostgresStore(resolved.database_url)
        agent_mode = AGENT_MODE_DISABLED
        milestone = MILESTONE_HOSTED
    else:
        store = SqliteStore(resolved.db_path)
        agent_mode = (
            AGENT_MODE_STRANDS_OLLAMA if resolved.assistant_enabled else AGENT_MODE_DISABLED
        )
        milestone = MILESTONE_LOCAL

    services = Services(
        store=store,
        clock=clock if clock is not None else SystemClock(),
        ids=ids if ids is not None else SecretsIdGenerator(),
    )

    if resolved.runtime == "hosted":
        from borrowed_steps.infrastructure.hosted_scheduler import HostedSchedulerService

        assert resolved.database_url is not None
        scheduler = HostedSchedulerService(
            resolved.database_url,
            clock=services.clock,
            ids=services.ids,
        )

    if resolved.runtime == "local":
        if resolved.tasks_enabled:
            runner = TaskRunner(
                store,
                services.clock,
                services.ids,
                interval=TASK_TICK_SECONDS,
                limit=TASK_TICK_CANDIDATE_LIMIT,
            )
        assistant = interpreter
        if assistant is None and resolved.assistant_enabled:
            assistant = _real_interpreter(resolved, services.clock)

    slot = InferenceSlot()
    # The registry asks the interpreter whether it still owns anything, so a run
    # that ended without closing its client is neither forgotten nor drained
    # away. Interpreters with nothing to own (the test doubles) do not implement
    # the protocol and are treated as always resolved.
    registry = InferenceRegistry(assistant if isinstance(assistant, CleanupOwner) else None)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Own the coordination runner and the in-flight interpretations.

        The runner is stopped first, so no tick is still writing while the rest
        of shutdown runs, and its stop is awaited off the event loop because
        joining a thread blocks. Whether it really stopped is reported: a thread
        that outlives its bounded join is named as still running, never counted
        as clean cleanup.

        The inference drain is unchanged and runs in a ``finally``, so a runner
        that refuses to stop cannot cause the provider cleanup to be skipped.

        The drain signals and cancels each run and then waits a bounded time.
        Its real outcome is logged either way: a run that does not end is
        reported as left behind, and a client that will not close is reported as
        still open. Neither is described as a clean shutdown, and nothing here
        claims the model at the other end stopped computing.
        """
        if runner is not None:
            runner.start()
        try:
            yield
        finally:
            try:
                if runner is not None and not await asyncio.to_thread(
                    runner.stop, TASK_STOP_TIMEOUT_SECONDS
                ):
                    _LOGGER.error(
                        "Shutdown: the coordination runner did not stop within %.1fs and is "
                        "still running. This shutdown was not clean.",
                        TASK_STOP_TIMEOUT_SECONDS,
                    )
            finally:
                report = await registry.drain(assistant_shutdown_seconds)
                if report.abandoned:
                    _LOGGER.warning(
                        "Shutdown: %d of %d interpretations ended; %d still running after "
                        "%.1fs and were left behind.",
                        report.ended,
                        report.requested,
                        report.abandoned,
                        assistant_shutdown_seconds,
                    )
                elif report.requested:
                    _LOGGER.info(
                        "Shutdown: all %d in-flight interpretations ended.", report.requested
                    )
                if report.cleanup_resolved is False:
                    _LOGGER.error(
                        "Shutdown: a provider client could not be closed and is still open. "
                        "This shutdown was not clean."
                    )
                elif report.cleanup_resolved:
                    _LOGGER.info("Shutdown: outstanding provider cleanup completed.")

    app = FastAPI(
        title="Borrowed Steps agent service",
        version=milestone,
        description=(
            "Hosted persisted equipment-loan workflow with in-app coordination notices."
            if resolved.runtime == "hosted"
            else (
                "Local persisted equipment-loan workflow with in-app coordination notices. "
                "Humans decide allocation and inspection; the coordination runner only "
                "records that a deadline passed and never changes inventory or a loan."
            )
        ),
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.services = services
    app.state.inference = registry
    # Exposed so a test can assert the real owned thread started and stopped,
    # rather than inferring it from a log line.
    app.state.tasks = runner
    app.state.scheduler = scheduler

    def require_hosted_schema() -> None:
        if resolved.runtime == "hosted":
            from borrowed_steps.infrastructure.postgres_migrations import SchemaVersionError
            from borrowed_steps.infrastructure.postgres_store import PostgresStore

            if isinstance(store, PostgresStore):
                try:
                    store.check_schema()
                except (SchemaVersionError, RuntimeError):
                    raise StarletteHTTPException(status_code=503) from None

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
        headers: dict[str, str] = {}
        diagnostic = getattr(error, "diagnostic", None)
        if isinstance(diagnostic, dict):
            headers["X-Assistant-Diagnostic"] = json.dumps(
                diagnostic, separators=(",", ":"), sort_keys=True
            )
        return _error(
            _STATUS_BY_CODE.get(error.code, 500),
            error.code,
            error.message,
            headers=headers if headers else None,
        )

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
        """Public liveness probe. Declares the milestone and the configured mode.

        ``agent_mode`` reports configuration only. It never claims the provider
        is reachable or that an interpretation would succeed.
        """
        return JSONResponse({"status": "ok", "milestone": milestone, "agent_mode": agent_mode})

    @app.post(
        "/api/workspaces",
        status_code=201,
        dependencies=[Depends(require_hosted_schema)],
    )
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
                "snapshot": _snapshot_json(snapshot, agent_mode),
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

    @app.get("/api/snapshot", dependencies=[Depends(require_hosted_schema)])
    def get_snapshot(http_request: HttpRequest) -> JSONResponse:
        """Everything the caller's workspace can see."""
        session = require_session(http_request)
        with store.transaction(session.workspace_id, write=False) as uow:
            snapshot = read_snapshot(uow)
        return JSONResponse(_snapshot_json(snapshot, agent_mode))

    @app.post("/api/intake/interpret", dependencies=[Depends(require_hosted_schema)])
    async def post_interpret(http_request: HttpRequest, body: InterpretBody) -> JSONResponse:
        """Suggest a draft request from free text. Read-only.

        Nothing is created, updated or recorded: no request, loan, equipment
        change, event, idempotency record or draft row. The caller reviews the
        suggestion and then submits the existing structured route themselves.

        Deliberately exempt from Idempotency-Key: a retry can only recompute a
        suggestion, never repeat an effect.
        """
        check_origin(http_request)
        session = await asyncio.to_thread(require_session, http_request)
        if assistant is None:
            msg = "The intake assistant is not enabled on this server."
            raise AssistantDisabledError(msg)
        if registry.closing:
            # Shutdown has begun. Starting inference now would either be
            # abandoned mid-run or hold the process open past its bound.
            msg = "The service is shutting down and is not starting new interpretations."
            raise AssistantUnavailableError(msg)
        if registry.cleanup_unresolved:
            # An earlier run left its provider client open. Starting another
            # would open a second one on top of it. This is not "busy": nothing
            # is running, and saying so would be false.
            msg = "The interpretation service is not available while a previous run is cleaned up."
            raise AssistantUnavailableError(msg)
        # A slot retained for a cleanup that has since resolved is released
        # here, so an earlier failure cannot leave the service permanently
        # answering "busy" with nothing running.
        if registry.settle():
            slot.release()
        if not slot.try_acquire():
            msg = "Another interpretation is already running. Try again in a moment."
            raise AssistantBusyError(msg)

        cancel = threading.Event()
        task = asyncio.create_task(
            assistant.interpret(
                body.text,
                StoreInventoryReader(store, session.workspace_id),
                cancel,
            )
        )

        def _finished(completed: asyncio.Task[Interpretation]) -> None:
            # Retrieve any error so it is never an unretrieved task exception.
            if not completed.cancelled():
                completed.exception()
            # The slot is freed when the inference truly ends, not when a caller
            # stops waiting, so a timed-out request cannot be overtaken — and
            # not while the run's provider client is still open, or the next
            # caller would open a second one on top of it.
            if registry.settle():
                slot.release()
            else:
                _LOGGER.error(
                    "An interpretation ended with cleanup unresolved; "
                    "the inference slot is retained."
                )

        task.add_done_callback(_finished)
        # From here the application owns the run, so shutdown can find it. The
        # check above and this call happen in one event-loop step, so no drain
        # can begin between them.
        registry.register(task, cancel)

        effective_deadline = min(
            assistant_deadline_seconds,
            max(0.0, 120.0 - assistant_cancel_grace_seconds),
        )

        try:
            interpretation = await asyncio.wait_for(
                asyncio.shield(task), timeout=effective_deadline
            )
        except TimeoutError:
            # Stop the run for real: set the cooperative signal *and* cancel the
            # task, which is the only half that can interrupt a provider call
            # already awaiting a reply. Then wait, briefly, for its cleanup —
            # the slot is released by the done callback when the run truly ends,
            # so a run still winding down keeps holding it and the next caller
            # is told the assistant is busy rather than overtaking it.
            registry.stop(task)
            _LOGGER.warning("Interpretation exceeded its deadline; the run was cancelled.")
            await _settle(task, assistant_cancel_grace_seconds)
            msg = "The interpretation took too long. Please try again or use the form."
            exc = AssistantTimeoutError(msg)
            if task.done() and not task.cancelled():
                task_exc = task.exception()
                if task_exc and hasattr(task_exc, "diagnostic"):
                    exc.diagnostic = task_exc.diagnostic  # type: ignore[attr-defined]
            raise exc from None
        except asyncio.CancelledError:
            # The caller went away. Without this the shielded task would keep
            # running with nobody waiting for it, holding the slot until its own
            # deadline. Waiting here is not possible — this coroutine is being
            # cancelled — so the done callback releases the slot when the run
            # finishes winding down.
            registry.stop(task)
            _LOGGER.warning("The caller stopped waiting; the interpretation was cancelled.")
            raise

        headers: dict[str, str] = {}
        if interpretation.diagnostic is not None:
            headers["X-Assistant-Diagnostic"] = json.dumps(
                interpretation.diagnostic, separators=(",", ":"), sort_keys=True
            )
        return JSONResponse(
            _interpretation_json(interpretation),
            headers=headers if headers else None,
        )

    @app.post("/api/requests", dependencies=[Depends(require_hosted_schema)])
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

    @app.post("/api/reservations", dependencies=[Depends(require_hosted_schema)])
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

    @app.post("/api/loans/{loan_id}/pickup", dependencies=[Depends(require_hosted_schema)])
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

    @app.post("/api/loans/{loan_id}/return", dependencies=[Depends(require_hosted_schema)])
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

    @app.post(
        "/api/equipment/{equipment_id}/inspection",
        dependencies=[Depends(require_hosted_schema)],
    )
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

    if resolved.runtime == "hosted":
        from borrowed_steps.infrastructure.postgres_migrations import SchemaVersionError

        def _authenticate_operational(http_request: HttpRequest) -> JSONResponse | None:
            if resolved.task_tick_token is None:
                return _error(
                    503,
                    "SERVICE_UNAVAILABLE",
                    _GENERIC_FAILURE,
                    headers={"Cache-Control": "no-store"},
                )
            auth_headers = http_request.headers.getlist("authorization")
            if len(auth_headers) != 1:
                return _error(
                    401,
                    "UNAUTHORIZED",
                    _GENERIC_FAILURE,
                    headers={"Cache-Control": "no-store"},
                )
            auth = auth_headers[0]
            prefix = "Bearer "
            if not auth.startswith(prefix):
                return _error(
                    401,
                    "UNAUTHORIZED",
                    _GENERIC_FAILURE,
                    headers={"Cache-Control": "no-store"},
                )
            token = auth[len(prefix) :]
            if not (
                32 <= len(token) <= 256 and bool(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token))
            ):
                return _error(
                    401,
                    "UNAUTHORIZED",
                    _GENERIC_FAILURE,
                    headers={"Cache-Control": "no-store"},
                )
            if not hmac.compare_digest(
                token.encode("utf-8"),
                resolved.task_tick_token.encode("utf-8"),
            ):
                return _error(
                    401,
                    "UNAUTHORIZED",
                    _GENERIC_FAILURE,
                    headers={"Cache-Control": "no-store"},
                )
            return None

        @app.post("/api/internal/tasks/tick", include_in_schema=False)
        def post_internal_tasks_tick(http_request: HttpRequest) -> JSONResponse:
            auth_err = _authenticate_operational(http_request)
            if auth_err is not None:
                return auth_err

            if scheduler is None:
                return _error(
                    503,
                    "SERVICE_UNAVAILABLE",
                    _GENERIC_FAILURE,
                    headers={"Cache-Control": "no-store"},
                )

            try:
                result = scheduler.tick()
            except SchemaVersionError:
                return _error(
                    503,
                    "SERVICE_UNAVAILABLE",
                    _GENERIC_FAILURE,
                    headers={"Cache-Control": "no-store"},
                )
            except Exception:
                _LOGGER.error("Scheduler tick failed")
                return _error(
                    503,
                    "SERVICE_UNAVAILABLE",
                    _GENERIC_FAILURE,
                    headers={"Cache-Control": "no-store"},
                )

            report_dict: dict[str, Any] | None = None
            if result.report is not None:
                report_dict = {
                    "considered": result.report.considered,
                    "marked_due": result.report.marked_due,
                    "resolved_stale": result.report.resolved_stale,
                    "unchanged": result.report.unchanged,
                    "contended": result.report.contended,
                    "stopped_early": result.report.stopped_early,
                }

            content = {
                "outcome": result.outcome,
                "capacity_limited": result.capacity_limited,
                "report": report_dict,
            }

            if result.outcome in {"success", "partial"}:
                status_code = 200
            elif result.outcome in {"busy", "stale_lease"}:
                status_code = 409
            else:
                status_code = 503

            return JSONResponse(
                status_code=status_code,
                content=content,
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/api/internal/tasks/status", include_in_schema=False)
        def get_internal_tasks_status(http_request: HttpRequest) -> JSONResponse:
            auth_err = _authenticate_operational(http_request)
            if auth_err is not None:
                return auth_err

            if scheduler is None:
                return _error(
                    503,
                    "SERVICE_UNAVAILABLE",
                    _GENERIC_FAILURE,
                    headers={"Cache-Control": "no-store"},
                )

            try:
                status_obj = scheduler.read_status()
            except SchemaVersionError:
                return _error(
                    503,
                    "SERVICE_UNAVAILABLE",
                    _GENERIC_FAILURE,
                    headers={"Cache-Control": "no-store"},
                )
            except Exception:
                _LOGGER.error("Scheduler status read failed")
                return _error(
                    503,
                    "SERVICE_UNAVAILABLE",
                    _GENERIC_FAILURE,
                    headers={"Cache-Control": "no-store"},
                )

            return JSONResponse(
                status_code=200,
                content=status_obj.to_dict(),
                headers={"Cache-Control": "no-store"},
            )

    return app
