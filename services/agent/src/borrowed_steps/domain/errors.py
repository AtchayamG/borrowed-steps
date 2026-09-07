"""Coded errors.

Each error carries a stable machine-readable ``code``. Messages are written for
an operator and never echo caller-supplied input or a framework trace, per the
trust rules in docs/ARCHITECTURE.md.

``CodedError`` is the shared base so a single outer adapter handler can map any
raised error onto the frozen contract envelope. ``DomainError`` marks the subset
raised by the loan rules themselves.
"""

from __future__ import annotations

__all__ = [
    "ApprovalRequiredError",
    "CodedError",
    "DomainError",
    "StateConflictError",
    "ValidationFailedError",
]


class CodedError(Exception):
    """An error with a stable code intended for the API error envelope."""

    code = "INTERNAL_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DomainError(CodedError):
    """Base class for rule violations expressed by the domain."""

    code = "DOMAIN_ERROR"


class ValidationFailedError(DomainError):
    """A value violates a business validation rule (contract code VALIDATION_ERROR)."""

    code = "VALIDATION_ERROR"


class ApprovalRequiredError(DomainError):
    """A human affirmation is required and was not supplied."""

    code = "APPROVAL_REQUIRED"


class StateConflictError(DomainError):
    """The requested transition is not legal for the current state or version."""

    code = "STATE_CONFLICT"
