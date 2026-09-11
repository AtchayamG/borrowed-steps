"""Real Strands agent over Groq in hosted M3 mode with transactional admission.

Enforces:
1. Implements RequestInterpreter and CleanupOwner protocols.
2. Constructs GroqModel only AFTER admission is reserved and dispatched.
   Never constructs a provider client on refused, malformed, disabled, or unavailable requests.
3. Derives canonical request and payload hashes from bounded inputs.
4. Reserves exactly RESERVED_SENDS (6) through InferenceAdmissionStore.
5. Executes two-stage Strands flow (inventory tool loop, source-only structured extraction).
6. Finishes admission record exactly once with truthful actual_sends, nullable total tokens,
   cleanup_completed, and conservative failure code mapping.
7. Conservative uncertainty: cancellation, cleanup failure, or unexpected exits settle as UNCERTAIN
   retaining concurrency until operator recovery.
8. Zero Ollama imports, zero DB transactions across model execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import secrets
import time
from threading import Event
from typing import Any
from uuid import UUID

import httpx
from strands import Agent
from strands.agent.agent_result import AgentResult
from strands.types.agent import Limits
from strands.types.content import Messages
from strands.types.exceptions import (
    ContextWindowOverflowException,
    EventLoopException,
    MaxTokensReachedException,
    ModelThrottledException,
    StructuredOutputException,
)

from borrowed_steps.application.errors import (
    AssistantBusyError,
    AssistantInvalidOutputError,
    AssistantTimeoutError,
    AssistantUnavailableError,
)
from borrowed_steps.application.interpreter import (
    CleanupOwner,
    Interpretation,
    InventoryReader,
    RequestInterpreter,
)
from borrowed_steps.application.ports import Clock
from borrowed_steps.domain.errors import CodedError
from borrowed_steps.infrastructure.groq_model import (
    DEFAULT_MAX_SENDS,
    DEFAULT_OPERATION_DEADLINE_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    GROQ_MODEL_ID,
    GroqDeadlineExpiredError,
    GroqModel,
    GroqModelError,
    GroqRequestTimeoutError,
    GroqSendBudgetExceededError,
)
from borrowed_steps.infrastructure.inference_admission import (
    RESERVED_SENDS,
    AdmissionFailureCode,
    AdmissionRefusedError,
    AdmissionReplayConflictError,
    AdmissionReservationRequest,
    AdmissionState,
    AdmissionUnavailableError,
    InferenceAdmissionError,
    InferenceAdmissionStore,
)
from borrowed_steps.infrastructure.postgres_migrations import require_transport_security
from borrowed_steps.infrastructure.strands_common import (
    AGENT_SYSTEM_PROMPT,
    AGENT_USER_PROMPT,
    EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA,
    EXTRACTION_USER_PROMPT,
    LIMIT_STOP_REASONS,
    RECOVERY_PROMPT,
    TOOL_NAME,
    Telemetry,
    ToolBudget,
    _Extraction,
    build_inventory_tool,
    emit_telemetry,
    ground_extraction,
)

__all__ = ["StrandsGroqInterpreter"]

_LOGGER = logging.getLogger("borrowed_steps.assistant.hosted")

_CLOSE_TIMEOUT_SECONDS = 2.0
_DEFAULT_MAX_TOOL_CALLS = 2

_UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    ConnectionError,
    OSError,
    httpx.HTTPError,
    GroqModelError,
    ModelThrottledException,
)

_INVALID_OUTPUT_ERRORS: tuple[type[Exception], ...] = (
    StructuredOutputException,
    MaxTokensReachedException,
    ContextWindowOverflowException,
    EventLoopException,
    ValueError,
)


class StrandsGroqInterpreter(RequestInterpreter, CleanupOwner):
    """Interprets intake text in hosted mode using Groq and PostgreSQL admission control."""

    __slots__ = (
        "_admission_store",
        "_api_key",
        "_clock",
        "_closing",
        "_database_url",
        "_deadline_seconds",
        "_max_model_requests",
        "_max_tool_calls",
        "_transport",
        "_transport_timeout_seconds",
    )

    def __init__(
        self,
        *,
        api_key: str,
        database_url: str,
        clock: Clock,
        max_model_requests: int = DEFAULT_MAX_SENDS,
        max_tool_calls: int = _DEFAULT_MAX_TOOL_CALLS,
        deadline_seconds: float = DEFAULT_OPERATION_DEADLINE_SECONDS,
        transport_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        admission_store: InferenceAdmissionStore | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            msg = "api_key must be an explicit non-empty string"
            raise ValueError(msg)
        if not isinstance(database_url, str) or not database_url.strip():
            msg = "database_url must be an explicit non-empty string"
            raise ValueError(msg)
        require_transport_security(database_url)

        if (
            type(max_model_requests) is not int
            or max_model_requests < 1
            or max_model_requests > DEFAULT_MAX_SENDS
        ):
            msg = f"max_model_requests must be between 1 and {DEFAULT_MAX_SENDS}"
            raise ValueError(msg)

        if (
            not math.isfinite(deadline_seconds)
            or deadline_seconds <= 0.0
            or deadline_seconds > DEFAULT_OPERATION_DEADLINE_SECONDS
        ):
            msg = f"deadline_seconds must be between 0.0 and {DEFAULT_OPERATION_DEADLINE_SECONDS}"
            raise ValueError(msg)

        if (
            not math.isfinite(transport_timeout_seconds)
            or transport_timeout_seconds <= 0.0
            or transport_timeout_seconds > DEFAULT_REQUEST_TIMEOUT_SECONDS
        ):
            msg = (
                f"transport_timeout_seconds must be between 0.0 and "
                f"{DEFAULT_REQUEST_TIMEOUT_SECONDS}"
            )
            raise ValueError(msg)

        self._api_key = api_key
        self._database_url = database_url
        self._clock = clock
        self._max_model_requests = max_model_requests
        self._max_tool_calls = max_tool_calls
        self._deadline_seconds = deadline_seconds
        self._transport_timeout_seconds = transport_timeout_seconds
        self._transport = transport
        self._admission_store = (
            admission_store
            if admission_store is not None
            else InferenceAdmissionStore(database_url)
        )
        self._closing: dict[GroqModel, asyncio.Task[None]] = {}

    def _reconcile_closes(self) -> None:
        """Forget closes that really finished with the client shut."""
        for model, task in list(self._closing.items()):
            if task.done() and not model.client_open:
                del self._closing[model]

    @property
    def cleanup_unresolved(self) -> bool:
        """True while a client this interpreter opened is still open."""
        self._reconcile_closes()
        return bool(self._closing)

    def _close_operation(self, model: GroqModel) -> asyncio.Task[None]:
        """This model's one close operation, started only if none is running."""
        running = self._closing.get(model)
        if running is not None and not running.done():
            return running
        task = asyncio.create_task(model.aclose())
        self._closing[model] = task
        task.add_done_callback(self._close_settled)
        return task

    def _close_settled(self, task: asyncio.Task[None]) -> None:
        """Observe a close that finished after whoever was waiting gave up."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.warning("Hosted assistant close finished late with %s", type(error).__name__)

    async def _await_close(
        self, model: GroqModel, timeout: float
    ) -> tuple[bool, BaseException | None]:
        """Wait a bounded time for this model's single close operation."""
        task = self._close_operation(model)
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            msg = "the owned Groq client did not close in time"
            return False, TimeoutError(msg)
        try:
            await task
        except (Exception, asyncio.CancelledError) as error:
            return True, error
        return True, None

    async def resolve_cleanup(self) -> bool:
        """Try again to close what is still held. True when nothing remains."""
        self._reconcile_closes()
        for model in list(self._closing):
            finished, failure = await self._await_close(model, _CLOSE_TIMEOUT_SECONDS)
            if not finished:
                _LOGGER.warning("Hosted assistant cleanup still unresolved: close is still running")
                continue
            if failure is not None:
                _LOGGER.warning(
                    "Hosted assistant cleanup still unresolved: %s", type(failure).__name__
                )
                continue
            if not model.client_open:
                del self._closing[model]
        return not self._closing

    async def _close(self, model: GroqModel, telemetry: Telemetry) -> BaseException | None:
        """Close the owned client, keeping ownership when it will not close."""
        failure: BaseException | None = None
        try:
            for attempt in (1, 2):
                finished, failure = await self._await_close(model, _CLOSE_TIMEOUT_SECONDS)
                if not finished:
                    break
                if failure is not None and model.client_open and attempt == 1:
                    continue
                break
        except asyncio.CancelledError:
            telemetry.cleanup = "close_cancelled"
            _LOGGER.error(
                "Assistant %s cleanup interrupted by cancellation; close is still running.",
                telemetry.correlation_id,
            )
            raise

        if failure is None and not model.client_open:
            telemetry.cleanup = "closed"
            self._closing.pop(model, None)
            return None

        telemetry.cleanup = "close_failed"
        _LOGGER.error(
            "Assistant %s cleanup unresolved (%s); refusing further runs until closed.",
            telemetry.correlation_id,
            "client still open" if failure is None else type(failure).__name__,
        )
        if failure is not None:
            return failure
        msg = "the owned Groq client did not close"
        return RuntimeError(msg)

    async def interpret(
        self,
        text: str,
        inventory: InventoryReader,
        cancel: Event,
        *,
        workspace_id: str | None = None,
        request_key: str | None = None,
        reservation_id: str | None = None,
        owner_id: str | None = None,
        payload_hash: str | None = None,
        request_key_hash: str | None = None,
    ) -> Interpretation:
        """Interpret intake text with admission reservation, two-stage Strands flow,
        and settlement.
        """
        if self.cleanup_unresolved:
            msg = "The interpretation service is not available while a previous run is cleaned up."
            raise AssistantUnavailableError(msg)

        if cancel.is_set():
            msg = "The interpretation was cancelled."
            raise AssistantTimeoutError(msg)

        # 1. Resolve and validate input identity
        resolved_workspace = workspace_id or getattr(inventory, "workspace_id", None)
        if not resolved_workspace or not isinstance(resolved_workspace, str):
            msg = "Missing or invalid workspace identity for admission reservation."
            raise AssistantUnavailableError(msg)

        try:
            parsed_ws = UUID(resolved_workspace)
            if parsed_ws.version != 4 or str(parsed_ws) != resolved_workspace:
                raise ValueError("workspace_id must be canonical UUID4")
        except (ValueError, TypeError, AttributeError):
            msg = "Workspace identity must be canonical UUID4."
            raise AssistantUnavailableError(msg) from None

        res_id = reservation_id or str(secrets.token_hex(16))
        # Ensure canonical UUID4 string format for reservation_id and owner_id
        try:
            parsed_res = UUID(res_id)
            if parsed_res.version != 4 or str(parsed_res) != res_id:
                import uuid

                res_id = str(uuid.uuid4())
        except ValueError:
            import uuid

            res_id = str(uuid.uuid4())

        own_id = owner_id or str(secrets.token_hex(16))
        try:
            parsed_own = UUID(own_id)
            if parsed_own.version != 4 or str(parsed_own) != own_id:
                import uuid

                own_id = str(uuid.uuid4())
        except ValueError:
            import uuid

            own_id = str(uuid.uuid4())

        if payload_hash is None:
            canonical_payload = json.dumps({"text": text}, sort_keys=True, separators=(",", ":"))
            pay_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        else:
            pay_hash = payload_hash

        if request_key_hash is None:
            key_source = request_key if request_key is not None else res_id
            req_hash = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
        else:
            req_hash = request_key_hash

        # 2. Reserve admission BEFORE constructing any provider model
        reservation_req = AdmissionReservationRequest(
            reservation_id=res_id,
            workspace_id=resolved_workspace,
            owner_id=own_id,
            request_key_hash=req_hash,
            payload_hash=pay_hash,
            reserved_sends=RESERVED_SENDS,
        )

        try:
            self._admission_store.reserve(reservation_req)
        except AdmissionRefusedError as exc:
            msg_str = str(exc).lower()
            if "another operation is active" in msg_str:
                raise AssistantBusyError(
                    "Another interpretation is already running. Try again in a moment."
                ) from None
            if "provider 429 cooldown active" in msg_str:
                raise AssistantUnavailableError(
                    "The interpretation service is temporarily paused due to provider cooldown."
                ) from None
            if "limit exceeded" in msg_str:
                raise AssistantBusyError(
                    "The interpretation service request limit has been reached."
                ) from None
            raise AssistantUnavailableError("Admission reservation was refused.") from None
        except AdmissionReplayConflictError:
            raise AssistantUnavailableError("Admission reservation conflict.") from None
        except AdmissionUnavailableError:
            raise AssistantUnavailableError("Admission storage is unavailable.") from None
        except InferenceAdmissionError:
            raise AssistantUnavailableError("Admission reservation failed.") from None

        # 3. Mark dispatched BEFORE constructing any provider model
        try:
            self._admission_store.mark_dispatched(
                reservation_id=res_id,
                owner_id=own_id,
            )
        except AdmissionRefusedError:
            raise AssistantUnavailableError("Admission dispatch was refused.") from None
        except AdmissionUnavailableError:
            raise AssistantUnavailableError("Admission storage is unavailable.") from None
        except InferenceAdmissionError:
            raise AssistantUnavailableError("Admission dispatch failed.") from None

        # 4. Construct GroqModel ONLY after admission is reserved and dispatched
        model = GroqModel(
            api_key=self._api_key,
            max_sends=self._max_model_requests,
            operation_deadline_seconds=self._deadline_seconds,
            request_timeout_seconds=self._transport_timeout_seconds,
            transport=self._transport,
        )

        telemetry = Telemetry(correlation_id=secrets.token_hex(8))
        diagnostic_holder: dict[str, Any] = {}
        started = time.monotonic()
        budget = ToolBudget(self._max_tool_calls)
        in_flight: BaseException | None = None
        terminal_state: AdmissionState = AdmissionState.UNCERTAIN
        terminal_failure_code: AdmissionFailureCode | None = AdmissionFailureCode.EXECUTION_UNKNOWN

        try:
            await model.__aenter__()
            try:
                async with asyncio.timeout(self._deadline_seconds):
                    extraction = await self._stages(
                        model, text, inventory, cancel, budget, telemetry
                    )
            except TimeoutError as error:
                cancel.set()
                telemetry.outcome, telemetry.reason = "failed", "deadline"
                terminal_failure_code = AdmissionFailureCode.DEADLINE_EXPIRED
                terminal_state = AdmissionState.FAILED_CONFIRMED
                msg = "The interpretation took too long."
                raise AssistantTimeoutError(msg) from error
            except GroqSendBudgetExceededError as error:
                telemetry.outcome, telemetry.reason = "failed", "request_budget"
                terminal_failure_code = AdmissionFailureCode.INVALID_OUTPUT
                terminal_state = AdmissionState.FAILED_CONFIRMED
                msg = "The assistant could not produce a usable suggestion."
                raise AssistantInvalidOutputError(msg) from error
            except (GroqDeadlineExpiredError, GroqRequestTimeoutError) as error:
                cancel.set()
                telemetry.outcome, telemetry.reason = "failed", "deadline"
                terminal_failure_code = AdmissionFailureCode.DEADLINE_EXPIRED
                terminal_state = AdmissionState.FAILED_CONFIRMED
                msg = "The interpretation took too long."
                raise AssistantTimeoutError(msg) from error
            except ModelThrottledException as error:
                telemetry.outcome = "failed"
                telemetry.reason = "provider_429"
                terminal_failure_code = AdmissionFailureCode.PROVIDER_429
                terminal_state = AdmissionState.FAILED_CONFIRMED
                msg = "The hosted interpretation service is temporarily unavailable."
                raise AssistantUnavailableError(msg) from error
            except _UNAVAILABLE_ERRORS as error:
                telemetry.outcome = "failed"
                telemetry.reason = f"provider_{type(error).__name__}"
                if model.last_status_code == 429:
                    terminal_failure_code = AdmissionFailureCode.PROVIDER_429
                else:
                    terminal_failure_code = AdmissionFailureCode.PROVIDER_FAILURE
                terminal_state = AdmissionState.FAILED_CONFIRMED
                msg = "The hosted interpretation service is not available."
                raise AssistantUnavailableError(msg) from error
            except _INVALID_OUTPUT_ERRORS as error:
                telemetry.outcome = "failed"
                telemetry.reason = f"output_{type(error).__name__}"
                terminal_failure_code = AdmissionFailureCode.INVALID_OUTPUT
                terminal_state = AdmissionState.FAILED_CONFIRMED
                msg = "The assistant could not produce a usable suggestion."
                raise AssistantInvalidOutputError(msg) from error

            # Check for cancellation before accepting result
            if cancel.is_set():
                telemetry.outcome, telemetry.reason = "failed", "cancelled_before_accept"
                terminal_failure_code = AdmissionFailureCode.CANCELLED
                terminal_state = AdmissionState.UNCERTAIN
                msg = "The interpretation was cancelled."
                raise AssistantTimeoutError(msg)

            if model.last_status_code == 429:
                telemetry.outcome = "failed"
                telemetry.reason = "provider_429"
                terminal_failure_code = AdmissionFailureCode.PROVIDER_429
                terminal_state = AdmissionState.FAILED_CONFIRMED
                msg = "The hosted interpretation service is temporarily unavailable."
                raise AssistantUnavailableError(msg)

            # Ground the extraction against original source
            interpretation = ground_extraction(
                extraction,
                text,
                self._clock,
                requests_sent=model.sent,
                tool_calls=budget.successes,
                diagnostic=diagnostic_holder,
                framework="strands",
                provider="groq",
                model_id=GROQ_MODEL_ID,
            )
            telemetry.outcome, telemetry.reason = "success", "ok"
            terminal_state = AdmissionState.SUCCEEDED
            terminal_failure_code = None
            return interpretation

        except AssistantInvalidOutputError as error:
            if telemetry.outcome != "failed":
                telemetry.outcome = "failed"
            if telemetry.reason == "ok" or not telemetry.reason:
                telemetry.reason = "invalid_output"
            terminal_failure_code = AdmissionFailureCode.INVALID_OUTPUT
            terminal_state = AdmissionState.FAILED_CONFIRMED
            in_flight = error
            raise
        except AssistantTimeoutError as error:
            if telemetry.outcome != "failed":
                telemetry.outcome = "failed"
            if (
                telemetry.reason == "deadline"
                or terminal_failure_code == AdmissionFailureCode.DEADLINE_EXPIRED
            ):
                telemetry.reason = "deadline"
                terminal_failure_code = AdmissionFailureCode.DEADLINE_EXPIRED
                terminal_state = AdmissionState.FAILED_CONFIRMED
            elif cancel.is_set() or telemetry.reason in {"cancelled", "cancelled_before_accept"}:
                telemetry.reason = "cancelled"
                terminal_failure_code = AdmissionFailureCode.CANCELLED
                terminal_state = AdmissionState.UNCERTAIN
            else:
                telemetry.reason = "deadline"
                terminal_failure_code = AdmissionFailureCode.DEADLINE_EXPIRED
                terminal_state = AdmissionState.FAILED_CONFIRMED
            in_flight = error
            raise
        except AssistantUnavailableError as error:
            if telemetry.outcome != "failed":
                telemetry.outcome = "failed"
            if model.last_status_code == 429 or "429" in telemetry.reason:
                terminal_failure_code = AdmissionFailureCode.PROVIDER_429
            else:
                terminal_failure_code = AdmissionFailureCode.PROVIDER_FAILURE
            terminal_state = AdmissionState.FAILED_CONFIRMED
            in_flight = error
            raise
        except CodedError as error:
            if telemetry.outcome != "failed":
                telemetry.outcome = "failed"
                telemetry.reason = error.code.lower()
            in_flight = error
            raise
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                telemetry.outcome = "interrupted"
                telemetry.reason = "cancelled"
                terminal_failure_code = AdmissionFailureCode.CANCELLED
                terminal_state = AdmissionState.UNCERTAIN
            else:
                if telemetry.outcome == "unknown":
                    telemetry.outcome = "interrupted"
                    telemetry.reason = type(error).__name__
            cancel.set()
            in_flight = error
            raise
        finally:
            telemetry.sends = model.sent
            telemetry.tool_attempts = budget.attempts
            telemetry.tool_successes = budget.successes
            telemetry.elapsed_ms = int((time.monotonic() - started) * 1000)

            cancelled_in_cleanup: asyncio.CancelledError | None = None
            try:
                close_error = await self._close(model, telemetry)
            except asyncio.CancelledError as cancelled:
                cancelled_in_cleanup = cancelled
                close_error = cancelled
                if telemetry.outcome in {"unknown", "success"}:
                    telemetry.outcome = "interrupted"
                    telemetry.reason = "cancelled_during_cleanup"

            cleanup_completed = close_error is None and not model.client_open
            if not cleanup_completed:
                terminal_state = AdmissionState.UNCERTAIN
                if terminal_failure_code is None:
                    terminal_failure_code = AdmissionFailureCode.EXECUTION_UNKNOWN

            if close_error is not None and telemetry.outcome == "success":
                telemetry.outcome, telemetry.reason = "failed", "cleanup_unresolved"

            emit_telemetry(telemetry)
            diagnostic = telemetry.as_dict()
            diagnostic_holder.update(diagnostic)
            if in_flight is not None:
                with contextlib.suppress(AttributeError, TypeError):
                    in_flight.diagnostic = diagnostic  # type: ignore[attr-defined]

            # Storage settlement: cancel/close completed BEFORE finish settlement
            try:
                self._admission_store.finish(
                    reservation_id=res_id,
                    owner_id=own_id,
                    state=terminal_state,
                    cleanup_completed=cleanup_completed,
                    actual_sends=model.sent
                    if model.sent > 0
                    else (1 if terminal_state == AdmissionState.SUCCEEDED else None),
                    actual_total_tokens=None,
                    failure_code=terminal_failure_code,
                )
            except Exception as fin_err:
                _LOGGER.error(
                    "Assistant %s failed to settle admission record: %s",
                    telemetry.correlation_id,
                    type(fin_err).__name__,
                )

            if cancelled_in_cleanup is not None:
                raise cancelled_in_cleanup

            if close_error is not None and in_flight is None:
                if isinstance(close_error, asyncio.CancelledError):
                    raise close_error
                msg = "The hosted interpretation service is not available."
                cleanup_err = AssistantUnavailableError(msg)
                cleanup_err.diagnostic = diagnostic  # type: ignore[attr-defined]
                raise cleanup_err from close_error

    async def _stages(
        self,
        model: GroqModel,
        text: str,
        inventory: InventoryReader,
        cancel: Event,
        budget: ToolBudget,
        telemetry: Telemetry,
    ) -> _Extraction:
        """Stage one: inventory tool execution; stage two: source-only extraction."""
        telemetry.stage = "inventory"
        agent = Agent(
            model=model,
            tools=[build_inventory_tool(inventory, budget)],
            system_prompt=AGENT_SYSTEM_PROMPT,
            callback_handler=None,
            load_tools_from_directory=False,
            retry_strategy=None,
        )

        result = await self._run_agent(agent, AGENT_USER_PROMPT.format(text=text), cancel)
        if self._needs_recovery(budget, model, result, telemetry.recovery_used, cancel):
            telemetry.recovery_used = True
            telemetry.stage = "inventory_recovery"
            _LOGGER.info(
                "Assistant %s recovery: first pass executed no inventory tool.",
                telemetry.correlation_id,
            )
            result = await self._run_agent(agent, RECOVERY_PROMPT, cancel)

        self._require_real_inventory(result, budget, model, cancel, telemetry)

        telemetry.stage = "extraction"
        return await self._extract(model, text, telemetry)

    async def _run_agent(self, agent: Agent, prompt: str, cancel: Event) -> AgentResult:
        """Stage one invocation with turns bounded by max requests."""
        return await agent.invoke_async(
            prompt,
            limits=Limits(turns=self._max_model_requests),
            cancel_signal=cancel,
        )

    def _needs_recovery(
        self,
        budget: ToolBudget,
        model: GroqModel,
        result: AgentResult,
        recovery_used: bool,
        cancel: Event,
    ) -> bool:
        """One corrective continuation, only for normally completed empty run."""
        if recovery_used or budget.successes > 0:
            return False
        if cancel.is_set() or model.exhausted:
            return False
        if result.stop_reason == "cancelled" or result.stop_reason in LIMIT_STOP_REASONS:
            return False
        return budget.remaining > 0 and model.budget.remaining > 0

    def _require_real_inventory(
        self,
        result: AgentResult,
        budget: ToolBudget,
        model: GroqModel,
        cancel: Event,
        telemetry: Telemetry,
    ) -> None:
        """Require verified inventory read before extraction."""
        if model.exhausted:
            telemetry.reason = "request_budget"
            msg = "The assistant could not produce a usable suggestion."
            raise AssistantInvalidOutputError(msg)
        if result.stop_reason == "cancelled" or cancel.is_set():
            telemetry.reason = "cancelled"
            msg = "The interpretation was cancelled."
            raise AssistantTimeoutError(msg)
        if result.stop_reason in LIMIT_STOP_REASONS:
            telemetry.reason = f"limit_{result.stop_reason}"
            msg = "The assistant could not produce a usable suggestion."
            raise AssistantInvalidOutputError(msg)
        if budget.attempts > self._max_tool_calls:
            telemetry.reason = "tool_budget"
            msg = "The assistant could not produce a usable suggestion."
            raise AssistantInvalidOutputError(msg)

        reported = result.metrics.tool_metrics.get(TOOL_NAME)
        if reported is not None and int(reported.success_count) > budget.successes:
            telemetry.reason = "tool_metrics_disagree"
            msg = "The assistant could not produce a usable suggestion."
            raise AssistantInvalidOutputError(msg)
        if not 1 <= budget.successes <= self._max_tool_calls:
            telemetry.reason = "no_tool_execution"
            msg = "The assistant could not produce a usable suggestion."
            raise AssistantInvalidOutputError(msg)

    async def _extract(
        self,
        model: GroqModel,
        text: str,
        telemetry: Telemetry,
    ) -> _Extraction:
        """Stage two: source-only structured extraction."""
        if model.budget.remaining <= 0:
            telemetry.reason = "no_capacity_for_extraction"
            msg = "The assistant could not produce a usable suggestion."
            raise AssistantInvalidOutputError(msg)

        messages: Messages = [
            {"role": "user", "content": [{"text": EXTRACTION_USER_PROMPT.format(text=text)}]}
        ]
        stream = model.structured_output(
            _Extraction, messages, EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA
        )
        output: object = None
        try:
            async for event in stream:
                if "output" in event:
                    output = event["output"]
        finally:
            await stream.aclose()

        if not isinstance(output, _Extraction):
            telemetry.reason = "malformed_extraction"
            msg = "The assistant could not produce a usable suggestion."
            raise AssistantInvalidOutputError(msg)
        return output
