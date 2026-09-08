"""Application-level coded errors."""

from __future__ import annotations

from borrowed_steps.domain.errors import CodedError

__all__ = [
    "AssistantBusyError",
    "AssistantDisabledError",
    "AssistantInvalidOutputError",
    "AssistantTimeoutError",
    "AssistantUnavailableError",
    "IdempotencyConflictError",
    "NotFoundError",
    "SessionRequiredError",
]


class NotFoundError(CodedError):
    """The entity does not exist inside the caller's workspace."""

    code = "NOT_FOUND"


class SessionRequiredError(CodedError):
    """No session cookie was supplied, or the session is unknown or expired."""

    code = "SESSION_REQUIRED"


class IdempotencyConflictError(CodedError):
    """An idempotency key was reused for a different route or payload."""

    code = "IDEMPOTENCY_CONFLICT"


class AssistantDisabledError(CodedError):
    """The intake assistant is not enabled in this process."""

    code = "ASSISTANT_DISABLED"


class AssistantUnavailableError(CodedError):
    """The local provider or model could not be reached."""

    code = "ASSISTANT_UNAVAILABLE"


class AssistantBusyError(CodedError):
    """An interpretation is already running; there is no waiting queue."""

    code = "ASSISTANT_BUSY"


class AssistantTimeoutError(CodedError):
    """The interpretation exceeded its deadline."""

    code = "ASSISTANT_TIMEOUT"


class AssistantInvalidOutputError(CodedError):
    """The model produced no usable, grounded, tool-backed result."""

    code = "ASSISTANT_INVALID_OUTPUT"
