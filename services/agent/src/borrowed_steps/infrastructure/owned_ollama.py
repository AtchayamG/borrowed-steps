"""Local lifecycle extension of the native Strands Ollama provider.

This is *not* a new provider. It subclasses the installed
``strands.models.ollama.OllamaModel`` and reuses its public
``format_request`` / ``format_chunk`` helpers unchanged, so request and response
translation stays the SDK's. Two things are added, and only two:

1. **Owned transport.** The installed provider constructs a fresh
   ``ollama.AsyncClient`` inside every ``stream`` and ``structured_output`` call
   and never closes it. Here one client is owned for the lifetime of a single
   logical interpretation and closed explicitly on every exit — success,
   provider error, timeout, cancellation and budget refusal — using the client's
   public ``close``/async-context API. The streaming response is closed the same
   way, including when a consumer abandons it part-way.
2. **A real request budget.** Every outbound chat request is charged *before* it
   is sent, across streaming, structured output and the optional recovery pass.
   ``Limits(turns=...)`` bounds agent loop cycles, not transport attempts, so it
   cannot do this on its own.

The control-flow of ``stream`` and ``structured_output`` below is adapted from
strands-agents 1.54.0, ``strands/models/ollama.py`` (Apache License 2.0,
Copyright Amazon.com, Inc. or its affiliates). The installed SDK is not
modified, monkeypatched or vendored wholesale, and no private attribute of the
client is touched.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Any, TypeVar

import ollama
from pydantic import BaseModel
from strands.models.ollama import OllamaModel
from strands.types.content import Messages
from strands.types.exceptions import ContextWindowOverflowException
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

__all__ = [
    "LOCAL_AUTHORIZATION_MARKER",
    "OwnedOllamaModel",
    "RequestBudget",
    "RequestBudgetExceededError",
]

# A fixed, deliberately non-secret placeholder. This is NOT authentication and
# authenticates nothing: the installed ollama client reads OLLAMA_API_KEY from
# the environment and adds an Authorization header whenever one is not already
# supplied, and `trust_env=False` does not disable that. Supplying this marker
# through the documented `headers` client argument occupies the slot, so an
# inherited key can never reach a request. The destination is loopback and the
# local server ignores the header entirely.
LOCAL_AUTHORIZATION_MARKER = "Bearer local-no-credential"

T = TypeVar("T", bound=BaseModel)


class RequestBudgetExceededError(Exception):
    """Raised instead of sending a request that would exceed the budget."""


class RequestBudget:
    """Counts outbound model requests for one logical interpretation.

    The failure is sticky. Once the budget is refused, every later charge is
    refused too, so an SDK layer that swallows the exception and keeps going
    still cannot turn the invocation into a success.
    """

    __slots__ = ("_exhausted", "_limit", "_sent")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._sent = 0
        self._exhausted = False

    @property
    def sent(self) -> int:
        return self._sent

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    @property
    def remaining(self) -> int:
        return max(self._limit - self._sent, 0)

    def charge(self) -> None:
        """Account for one request about to be sent, or refuse it."""
        if self._exhausted or self._sent >= self._limit:
            self._exhausted = True
            msg = f"model request budget of {self._limit} is exhausted"
            raise RequestBudgetExceededError(msg)
        self._sent += 1


async def _aclose(candidate: object) -> None:
    """Close a response stream if it is one. Safe to call more than once."""
    closer = getattr(candidate, "aclose", None)
    if closer is not None:
        await closer()


class OwnedOllamaModel(OllamaModel):
    """Native Ollama provider with an owned client and a counted request budget."""

    def __init__(
        self,
        *,
        host: str,
        model_id: str,
        budget: RequestBudget,
        timeout_seconds: float,
    ) -> None:
        super().__init__(
            host=host,
            ollama_client_args={
                "timeout": timeout_seconds,
                # Stay on the pinned loopback destination: no redirect chasing,
                # and no proxy picked up from the environment.
                "follow_redirects": False,
                "trust_env": False,
                # Occupy the Authorization slot so an inherited OLLAMA_API_KEY
                # cannot be attached. See LOCAL_AUTHORIZATION_MARKER above.
                "headers": {"Authorization": LOCAL_AUTHORIZATION_MARKER},
            },
            model_id=model_id,
            temperature=0.0,
        )
        self._budget = budget
        self._client: ollama.AsyncClient | None = None

    @property
    def budget(self) -> RequestBudget:
        return self._budget

    @property
    def client_open(self) -> bool:
        return self._client is not None

    async def __aenter__(self) -> OwnedOllamaModel:
        self._client = ollama.AsyncClient(self.host, **self.client_args)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the owned client, keeping the handle until the close returns.

        The reference is cleared *after* ``close`` succeeds, never before. If the
        close raises, or a cancellation lands part-way through it, this object
        still holds the only handle to an open client, so the caller can try
        again and shutdown can finish the job. Clearing first would drop that
        handle on the floor and leave a socket nobody owns.

        Closing an already-closed client is a no-op, so this stays safe to call
        more than once.
        """
        client = self._client
        if client is None:
            return
        await client.close()  # type: ignore[no-untyped-call]
        self._client = None

    def _require_client(self) -> ollama.AsyncClient:
        if self._client is None:
            msg = "the owned Ollama client is not open"
            raise RuntimeError(msg)
        return self._client

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: object = None,
        **kwargs: object,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream one model turn over the owned client.

        Adapted from strands-agents 1.54.0 ``OllamaModel.stream``; translation is
        unchanged, ownership and the budget charge are added.
        """
        del tool_choice, kwargs  # accepted for interface parity; Ollama ignores it
        client = self._require_client()
        request = self.format_request(messages, tool_specs, system_prompt)

        self._budget.charge()

        response: Any = None
        tool_requested = False
        event: Any = None
        try:
            response = await client.chat(**request)
            yield self.format_chunk({"chunk_type": "message_start"})
            yield self.format_chunk({"chunk_type": "content_start", "data_type": "text"})

            async for event in response:
                for tool_call in event.message.tool_calls or []:
                    yield self.format_chunk(
                        {"chunk_type": "content_start", "data_type": "tool", "data": tool_call}
                    )
                    yield self.format_chunk(
                        {"chunk_type": "content_delta", "data_type": "tool", "data": tool_call}
                    )
                    yield self.format_chunk(
                        {"chunk_type": "content_stop", "data_type": "tool", "data": tool_call}
                    )
                    tool_requested = True

                yield self.format_chunk(
                    {
                        "chunk_type": "content_delta",
                        "data_type": "text",
                        "data": event.message.content,
                    }
                )
        except ollama.ResponseError as error:
            if any(message in str(error).lower() for message in self.OVERFLOW_MESSAGES):
                raise ContextWindowOverflowException(str(error)) from error
            raise
        finally:
            # Closes on normal completion, on error, and on GeneratorExit when a
            # consumer abandons the stream part-way.
            await _aclose(response)

        stop_reason = "tool_use" if tool_requested else (event.done_reason if event else None)
        yield self.format_chunk({"chunk_type": "content_stop", "data_type": "text"})
        yield self.format_chunk({"chunk_type": "message_stop", "data": stop_reason})
        if event is not None:
            yield self.format_chunk({"chunk_type": "metadata", "data": event})

    async def structured_output(
        self,
        output_model: type[T],
        prompt: Messages,
        system_prompt: str | None = None,
        **kwargs: object,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        """Ask for structured output over the owned client.

        Adapted from strands-agents 1.54.0 ``OllamaModel.structured_output``.
        """
        del kwargs
        client = self._require_client()
        request = self.format_request(messages=prompt, system_prompt=system_prompt)
        request["format"] = output_model.model_json_schema()
        request["stream"] = False

        self._budget.charge()

        response: Any = None
        try:
            response = await client.chat(**request)
        except ollama.ResponseError as error:
            if any(message in str(error).lower() for message in self.OVERFLOW_MESSAGES):
                raise ContextWindowOverflowException(str(error)) from error
            raise
        finally:
            await _aclose(response)

        try:
            content = response.message.content.strip()
            yield {"output": output_model.model_validate_json(content)}
        except Exception as error:
            msg = f"Failed to parse or load content into model: {error}"
            raise ValueError(msg) from error
