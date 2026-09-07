"""Application-level coded errors."""

from __future__ import annotations

from borrowed_steps.domain.errors import CodedError

__all__ = ["IdempotencyConflictError", "NotFoundError", "SessionRequiredError"]


class NotFoundError(CodedError):
    """The entity does not exist inside the caller's workspace."""

    code = "NOT_FOUND"


class SessionRequiredError(CodedError):
    """No session cookie was supplied, or the session is unknown or expired."""

    code = "SESSION_REQUIRED"


class IdempotencyConflictError(CodedError):
    """An idempotency key was reused for a different route or payload."""

    code = "IDEMPOTENCY_CONFLICT"
