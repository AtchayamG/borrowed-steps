"""Groq transport adapter for Strands OpenAIModel in M3 hosted mode.

This module provides ``GroqModel``, a subclass of Strands ``OpenAIModel`` pinned to
Groq's OpenAI-compatible endpoint (``https://api.groq.com/openai/v1``) and model
``openai/gpt-oss-20b``.

Enforces:
1. Strict constructor arguments: explicit API key required, no environment lookups,
   pinned endpoint and model, no network calls on import/construction.
2. Sticky actual-send budget (maximum 6 actual HTTP dispatches), shared across
   both stage 1 tool loops/continuations and stage 2 structured extraction.
3. Monotonic operation deadline (capped at 110.0s) and per-request timeout
   (capped at 60.0s).
4. Strict target verification: fails closed on non-HTTPS or non-Groq chat completion targets.
5. Preservation of 429 status codes and parsed Retry-After headers without unbounded sleeps.
6. Clean resource lifecycle and cancellation handling without leaking tasks or connections.
7. Strictly typed and zero raw secrets/prompts logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import json
import logging
import math
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, TypeVar

import httpx
import openai
from openai.types.chat.parsed_chat_completion import ParsedChatCompletion
from pydantic import BaseModel
from strands.models.openai import OpenAIModel
from strands.types.content import Messages
from strands.types.exceptions import (
    ContextWindowOverflowException,
    ModelThrottledException,
)
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec
from typing_extensions import override

_LOGGER = logging.getLogger(__name__)

GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
GROQ_MODEL_ID: str = "openai/gpt-oss-20b"
ALLOWED_HOST: str = "api.groq.com"
ALLOWED_PATH: str = "/openai/v1/chat/completions"

DEFAULT_MAX_SENDS: int = 6
DEFAULT_OPERATION_DEADLINE_SECONDS: float = 110.0
DEFAULT_REQUEST_TIMEOUT_SECONDS: float = 60.0

T = TypeVar("T", bound=BaseModel)


class GroqModelError(Exception):
    """Base exception for all Groq transport adapter errors."""


class GroqSendBudgetExceededError(GroqModelError):
    """Raised when the send budget is exhausted and another send is attempted."""


class GroqDeadlineExpiredError(GroqModelError):
    """Raised when the monotonic operation deadline has expired."""


class GroqRequestTimeoutError(GroqModelError):
    """Raised when an individual request times out within the per-request limit."""


class GroqTargetRefusedError(GroqModelError):
    """Raised when a request target does not match the pinned HTTPS Groq endpoint."""


class GroqRateLimitError(GroqModelError):
    """Raised on HTTP 429 with parsed retry-after information."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SendBudget:
    """Sticky send budget tracking actual wire dispatches.

    Once exhausted, the budget remains sticky-exhausted: no further sends may be
    attempted, and any attempt to charge raises GroqSendBudgetExceededError.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            msg = "Budget limit must be at least 1"
            raise ValueError(msg)
        self._limit: int = limit
        self._sent: int = 0
        self._exhausted: bool = False

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def sent(self) -> int:
        return self._sent

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    @property
    def remaining(self) -> int:
        if self._exhausted:
            return 0
        return max(0, self._limit - self._sent)

    def mark_exhausted(self) -> None:
        """Mark budget permanently exhausted."""
        self._exhausted = True

    def charge(self) -> None:
        """Charge one send immediately before wire dispatch.

        Raises:
            GroqSendBudgetExceededError: If the budget is already exhausted or limit is reached.
        """
        if self._exhausted or self._sent >= self._limit:
            self._exhausted = True
            msg = f"Send budget exhausted: {self._sent}/{self._limit} sends charged"
            raise GroqSendBudgetExceededError(msg)
        self._sent += 1
        if self._sent >= self._limit:
            self._exhausted = True


def _parse_retry_after(header_value: str | None) -> float | None:
    """Parse HTTP Retry-After header as either seconds or HTTP-date."""
    if not header_value:
        return None
    val = header_value.strip()
    try:
        seconds = float(val)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(val)
        diff = dt.timestamp() - time.time()
        return max(0.0, diff)
    except Exception:
        return None


class _DeadlineByteStream(httpx.AsyncByteStream):
    """Wraps an async byte stream to enforce the monotonic operation deadline per chunk."""

    def __init__(
        self,
        stream: httpx.AsyncByteStream | httpx.SyncByteStream,
        deadline_monotonic: float,
        request_deadline: float,
    ) -> None:
        self._stream = stream
        self._deadline_monotonic = deadline_monotonic
        self._request_deadline = request_deadline
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            if isinstance(self._stream, httpx.AsyncByteStream):
                iterator = self._stream.__aiter__()
                while True:
                    try:
                        async with asyncio.timeout_at(
                            min(self._deadline_monotonic, self._request_deadline)
                        ):
                            chunk = await anext(iterator)
                    except StopAsyncIteration:
                        break
                    except TimeoutError as exc:
                        if self._deadline_monotonic <= self._request_deadline:
                            msg = "Operation deadline expired while streaming response chunks"
                            raise GroqDeadlineExpiredError(msg) from exc
                        msg = "Request timed out while reading response"
                        raise GroqRequestTimeoutError(msg) from exc
                    yield chunk
            else:
                for chunk in self._stream:
                    if time.monotonic() >= self._deadline_monotonic:
                        msg = "Operation deadline expired while streaming response chunks"
                        raise GroqDeadlineExpiredError(msg)
                    yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if not self._closed:
            if hasattr(self._stream, "aclose"):
                await self._stream.aclose()
            elif hasattr(self._stream, "close"):
                self._stream.close()
            self._closed = True


class _BoundedGroqTransport(httpx.AsyncBaseTransport):
    """Custom HTTP transport enforcing bounds, timeouts, deadline, and target pins.

    Intercepts HTTP requests immediately before network dispatch:
    - Verifies HTTPS protocol and pinned host/path.
    - Charges SendBudget at the physical transport boundary.
    - Applies per-request timeout and absolute monotonic deadline.
    - Captures HTTP 429 status code and Retry-After header.
    - Bounds streaming response reading by monotonic deadline.
    """

    def __init__(
        self,
        budget: SendBudget,
        deadline_monotonic: float,
        request_timeout_seconds: float,
        inner_transport: httpx.AsyncBaseTransport,
    ) -> None:
        self._budget = budget
        self._deadline_monotonic = deadline_monotonic
        self._request_timeout_seconds = request_timeout_seconds
        self._inner_transport = inner_transport
        self._last_status_code: int | None = None
        self._last_retry_after: float | None = None
        self._closed = False

    @property
    def last_status_code(self) -> int | None:
        return self._last_status_code

    @property
    def last_retry_after(self) -> float | None:
        return self._last_retry_after

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Inspect, validate, budget-charge, and dispatch an HTTP request."""
        if self._closed:
            msg = "Transport is closed"
            raise GroqModelError(msg)

        # Parent configuration is mutable; enforce the model pin at the wire too.
        if request.content:
            payload = json.loads(request.content)
            if not isinstance(payload, dict) or payload.get("model") != GROQ_MODEL_ID:
                raise GroqTargetRefusedError("Request model refused")

        # 1. Target URL validation (fail closed on unexpected targets)
        scheme = request.url.scheme.lower()
        host = request.url.host.lower()
        path = request.url.path

        if (
            scheme != "https"
            or host != ALLOWED_HOST
            or path != ALLOWED_PATH
            or request.url.port not in (None, 443)
            or request.url.query
            or request.url.userinfo
            or request.method != "POST"
        ):
            msg = (
                f"Request target refused: {scheme}://{host}{path} "
                f"(expected https://{ALLOWED_HOST}{ALLOWED_PATH})"
            )
            raise GroqTargetRefusedError(msg)

        # 2. Check monotonic deadline before sending
        now = time.monotonic()
        if now >= self._deadline_monotonic:
            msg = (
                f"Operation deadline expired before HTTP dispatch "
                f"({now:.3f} >= {self._deadline_monotonic:.3f})"
            )
            raise GroqDeadlineExpiredError(msg)

        # 3. Check budget before sending
        if self._budget.exhausted or self._budget.remaining <= 0:
            self._budget.mark_exhausted()
            msg = (
                f"Send budget exhausted: {self._budget.sent}/{self._budget.limit} "
                f"sends already charged"
            )
            raise GroqSendBudgetExceededError(msg)

        # 4. Compute effective timeout bounded by both request timeout and remaining deadline
        remaining_deadline = self._deadline_monotonic - now
        effective_timeout = min(self._request_timeout_seconds, remaining_deadline)
        if effective_timeout <= 0.0:
            msg = "Operation deadline expired before HTTP dispatch"
            raise GroqDeadlineExpiredError(msg)

        # 5. Charge budget immediately before dispatch
        self._budget.charge()

        # 6. Execute send through inner transport bounded by timeout
        try:
            async with asyncio.timeout(effective_timeout):
                response = await self._inner_transport.handle_async_request(request)
        except TimeoutError as exc:
            if remaining_deadline <= self._request_timeout_seconds:
                msg = "Operation deadline expired during HTTP dispatch"
                raise GroqDeadlineExpiredError(msg) from exc
            msg = f"Request dispatch timed out after {effective_timeout:.2f}s"
            raise GroqRequestTimeoutError(msg) from exc

        # 7. Record status and Retry-After
        self._last_status_code = response.status_code
        if response.status_code == 429:
            retry_header = response.headers.get("Retry-After") or response.headers.get(
                "retry-after"
            )
            self._last_retry_after = _parse_retry_after(retry_header)

        # 8. Wrap response stream with deadline bounds
        response.stream = _DeadlineByteStream(
            response.stream, self._deadline_monotonic, now + self._request_timeout_seconds
        )
        return response

    @override
    async def aclose(self) -> None:
        """No-op when called by ephemeral per-request clients."""

    async def force_close(self) -> None:
        """Close the underlying transport permanently."""
        if not self._closed:
            async with asyncio.timeout(2.0):
                await self._inner_transport.aclose()
            self._closed = True


class GroqModel(OpenAIModel):
    """Strands OpenAIModel transport adapter pinned to Groq hosted inference.

    Represents one interpretation and is shared across stage 1 tool execution
    and stage 2 structured extraction.
    """

    def __init__(
        self,
        api_key: str,
        *,
        max_sends: int = DEFAULT_MAX_SENDS,
        operation_deadline_seconds: float = DEFAULT_OPERATION_DEADLINE_SECONDS,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Initialize GroqModel with explicit credentials and strict boundaries.

        Args:
            api_key: Explicit, non-empty Groq API key string.
            max_sends: Maximum actual wire dispatches allowed (<= 6).
            operation_deadline_seconds: Monotonic deadline in seconds (<= 110.0s).
            request_timeout_seconds: Per-request timeout in seconds (<= 60.0s).
            transport: Optional explicit test transport (e.g. MockTransport).

        Raises:
            ValueError: If api_key is missing/empty, or any limit exceeds its ceiling.
        """
        if not isinstance(api_key, str) or not api_key.strip():
            msg = "api_key must be a non-empty string"
            raise ValueError(msg)

        if type(max_sends) is not int or max_sends < 1 or max_sends > DEFAULT_MAX_SENDS:
            msg = f"max_sends must be between 1 and {DEFAULT_MAX_SENDS} (got {max_sends})"
            raise ValueError(msg)

        if (
            not math.isfinite(operation_deadline_seconds)
            or operation_deadline_seconds <= 0.0
            or operation_deadline_seconds > DEFAULT_OPERATION_DEADLINE_SECONDS
        ):
            msg = (
                f"operation_deadline_seconds must be between 0.0 and "
                f"{DEFAULT_OPERATION_DEADLINE_SECONDS} (got {operation_deadline_seconds})"
            )
            raise ValueError(msg)

        if (
            not math.isfinite(request_timeout_seconds)
            or request_timeout_seconds <= 0.0
            or request_timeout_seconds > DEFAULT_REQUEST_TIMEOUT_SECONDS
        ):
            msg = (
                f"request_timeout_seconds must be between 0.0 and "
                f"{DEFAULT_REQUEST_TIMEOUT_SECONDS} (got {request_timeout_seconds})"
            )
            raise ValueError(msg)

        self._api_key = api_key
        self._max_sends = max_sends
        self._operation_deadline_seconds = operation_deadline_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._deadline_monotonic = time.monotonic() + operation_deadline_seconds

        self._budget = SendBudget(limit=max_sends)

        if transport is not None:
            self._inner_transport: httpx.AsyncBaseTransport = transport
        else:
            self._inner_transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)

        self._groq_transport = _BoundedGroqTransport(
            budget=self._budget,
            deadline_monotonic=self._deadline_monotonic,
            request_timeout_seconds=self._request_timeout_seconds,
            inner_transport=self._inner_transport,
        )

        self._client_open: bool = True
        self._active_clients: set[openai.AsyncOpenAI] = set()

        # Initialize parent with pinned configuration.
        # Explicitly disable SDK-level retries.
        super().__init__(
            model_id=GROQ_MODEL_ID,
            client_args={
                "api_key": api_key,
                "base_url": GROQ_BASE_URL,
                "max_retries": 0,
            },
        )

    @property
    def budget(self) -> SendBudget:
        """SendBudget instance tracking wire dispatches."""
        return self._budget

    @property
    def sent(self) -> int:
        """Number of actual HTTP sends dispatched."""
        return self._budget.sent

    @property
    def exhausted(self) -> bool:
        """Whether the send budget has been exhausted."""
        return self._budget.exhausted

    @property
    def deadline_monotonic(self) -> float:
        """Absolute monotonic deadline timestamp."""
        return self._deadline_monotonic

    @property
    def time_remaining(self) -> float:
        """Seconds remaining until monotonic deadline expires."""
        return max(0.0, self._deadline_monotonic - time.monotonic())

    @property
    def last_status_code(self) -> int | None:
        """HTTP status code of the last received response."""
        return self._groq_transport.last_status_code

    @property
    def last_retry_after(self) -> float | None:
        """Parsed Retry-After value in seconds from the last 429 response, if any."""
        return self._groq_transport.last_retry_after

    @property
    def client_open(self) -> bool:
        """Whether this model adapter is open for requests."""
        return self._client_open

    @asynccontextmanager
    @override
    async def _get_client(self) -> AsyncIterator[openai.AsyncOpenAI]:
        """Create and yield an AsyncOpenAI client bound to the custom transport."""
        if not self._client_open:
            msg = "GroqModel adapter is closed"
            raise GroqModelError(msg)
        if self._active_clients:
            raise GroqModelError("A previous client operation has not closed")

        http_client = httpx.AsyncClient(
            transport=self._groq_transport,
            base_url=GROQ_BASE_URL,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(self._request_timeout_seconds),
        )

        client = openai.AsyncOpenAI(
            api_key=self._api_key,
            base_url=GROQ_BASE_URL,
            max_retries=0,
            http_client=http_client,
        )

        self._active_clients.add(client)
        try:
            yield client
        finally:
            async with asyncio.timeout(2.0):
                await client.close()
            self._active_clients.discard(client)

    @override
    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream conversation with Groq, enforcing budget and un-wrapping custom errors."""
        # Pre-check deadline and budget upfront
        if time.monotonic() >= self._deadline_monotonic:
            msg = "Operation deadline expired before streaming started"
            raise GroqDeadlineExpiredError(msg)

        if self._budget.exhausted or self._budget.remaining <= 0:
            self._budget.mark_exhausted()
            msg = (
                f"Send budget exhausted: {self._budget.sent}/{self._budget.limit} "
                f"sends already charged"
            )
            raise GroqSendBudgetExceededError(msg)

        try:
            async with contextlib.aclosing(
                super().stream(
                    messages,
                    tool_specs=tool_specs,
                    system_prompt=system_prompt,
                    tool_choice=tool_choice,
                    **kwargs,
                )
            ) as events:
                async for chunk in events:
                    yield chunk
        except openai.APIConnectionError as exc:
            if isinstance(exc.__cause__, GroqModelError):
                raise exc.__cause__ from None
            raise GroqModelError("Groq connection failed") from None
        except ModelThrottledException:
            raise ModelThrottledException("Groq rate limit reached") from None
        except ContextWindowOverflowException:
            raise ContextWindowOverflowException("Groq context limit exceeded") from None
        except openai.APIError:
            raise GroqModelError("Groq provider request failed") from None

    @override
    async def structured_output(
        self,
        output_model: type[T],
        prompt: Messages,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        """Request structured output from Groq, enforcing strict schema without tool fields."""
        # Pre-check deadline and budget upfront
        if time.monotonic() >= self._deadline_monotonic:
            msg = "Operation deadline expired before structured output started"
            raise GroqDeadlineExpiredError(msg)

        if self._budget.exhausted or self._budget.remaining <= 0:
            self._budget.mark_exhausted()
            msg = (
                f"Send budget exhausted: {self._budget.sent}/{self._budget.limit} "
                f"sends already charged"
            )
            raise GroqSendBudgetExceededError(msg)

        async with self._get_client() as client:
            try:
                request = self.format_request(prompt, system_prompt=system_prompt)
                # Drop streaming-only fields
                request.pop("stream", None)
                request.pop("stream_options", None)
                # Stage two structured output forbids tool and tool_choice fields
                request.pop("tools", None)
                request.pop("tool_choice", None)

                response: ParsedChatCompletion[T] = await client.beta.chat.completions.parse(
                    **request, response_format=output_model
                )
            except openai.APIConnectionError as exc:
                if isinstance(exc.__cause__, GroqModelError):
                    raise exc.__cause__ from None
                raise GroqModelError("Groq connection failed") from None
            except openai.RateLimitError:
                _LOGGER.warning("Groq rate limit encountered during structured output")
                raise ModelThrottledException("Groq rate limit reached") from None
            except openai.APIError as exc:
                if getattr(exc, "code", None) == "context_length_exceeded":
                    raise ContextWindowOverflowException("Groq context limit exceeded") from None
                raise GroqModelError("Groq provider request failed") from None
            except ValueError:
                raise ValueError("Groq structured output could not be parsed") from None

        parsed: T | None = None
        if len(response.choices) > 1:
            msg = "Multiple choices found in the Groq response."
            raise ValueError(msg)

        for choice in response.choices:
            if choice.message.refusal:
                msg = "Model refused structured output"
                raise ValueError(msg)
            if isinstance(choice.message.parsed, output_model):
                parsed = choice.message.parsed
                break

        if parsed is not None:
            yield {"output": parsed}
        else:
            msg = "No valid structured output was found in the Groq response."
            raise ValueError(msg)

    async def aclose(self) -> None:
        """Close model adapter, active client sessions, and underlying transport."""
        if not self._client_open and not self._active_clients and self._groq_transport._closed:
            return
        self._client_open = False
        active = list(self._active_clients)
        for client in active:
            async with asyncio.timeout(2.0):
                await client.close()
            self._active_clients.discard(client)
        await self._groq_transport.force_close()

    async def __aenter__(self) -> GroqModel:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()
