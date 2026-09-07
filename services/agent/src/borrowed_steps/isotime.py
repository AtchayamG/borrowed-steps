"""UTC ISO-8601 helpers shared by the outer adapters.

Leaf module: standard library only, no imports from any layer of this package.
The frozen API contract uses UTC ISO-8601 timestamps, normalised to whole
seconds so that stored and served representations are byte-identical.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["normalise", "parse_iso", "to_iso"]


def normalise(value: datetime) -> datetime:
    """Return ``value`` as a whole-second UTC datetime.

    Raises ``ValueError`` for naive datetimes: the contract has no local time.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        msg = "datetime must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC).replace(microsecond=0)


def to_iso(value: datetime) -> str:
    """Serialise ``value`` as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return normalise(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    """Parse a stored UTC ISO-8601 timestamp."""
    return normalise(datetime.fromisoformat(text))
