"""Real clock and identifier generator."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

__all__ = ["SecretsIdGenerator", "SystemClock"]


class SystemClock:
    """Whole-second UTC wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC).replace(microsecond=0)


class SecretsIdGenerator:
    """Opaque identifiers from the operating system CSPRNG."""

    def new_id(self) -> str:
        return secrets.token_hex(12)

    def new_session_id(self) -> str:
        return secrets.token_urlsafe(32)
