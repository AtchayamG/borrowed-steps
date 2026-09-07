"""Runtime configuration, read from the environment. No secrets are involved."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DEFAULT_ALLOWED_ORIGINS", "DEFAULT_DB_PATH", "Settings", "load_settings"]

DEFAULT_DB_PATH = "data/borrowed_steps.db"
DEFAULT_ALLOWED_ORIGINS = "http://127.0.0.1:5173,http://localhost:5173"


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the service needs to start."""

    db_path: Path
    allowed_origins: tuple[str, ...]
    cookie_secure: bool


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from ``BS_DB_PATH``, ``BS_ALLOWED_ORIGINS`` and ``BS_COOKIE_SECURE``."""
    source = os.environ if env is None else env
    origins = source.get("BS_ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS)
    return Settings(
        db_path=Path(source.get("BS_DB_PATH", DEFAULT_DB_PATH)),
        allowed_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
        cookie_secure=_flag(source.get("BS_COOKIE_SECURE")),
    )
