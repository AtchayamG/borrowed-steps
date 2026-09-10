"""Vercel serverless entry point for Borrowed Steps.

Exports the canonical FastAPI application instance (`app`) for Vercel's Python runtime.
Strictly requires explicit hosted configuration and refuses missing or local runtime
settings before any database, SQLite, or TaskRunner initialization could occur.
Zero database I/O and zero provider inference on import.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["app", "init_app"]


def _configure_logging() -> None:
    level = os.environ.get("BS_LOG_LEVEL", "").strip().upper()
    if not level:
        return
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise RuntimeError("Invalid BS_LOG_LEVEL")
    logger = logging.getLogger("borrowed_steps")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(levelname)s:     %(name)s %(message)s")
        )
        logger.addHandler(handler)


def init_app() -> FastAPI:
    """Initialize and validate the hosted FastAPI application.

    Refuses missing, local, or invalid configurations before calling create_app.
    Never imports borrowed_steps.main or manufactures production values.
    """
    raw_runtime = os.environ.get("BS_RUNTIME", "").strip().lower()
    if not raw_runtime:
        raise RuntimeError(
            "Missing BS_RUNTIME environment variable. Hosted entry point requires "
            "BS_RUNTIME=hosted; refusing unconfigured execution."
        )
    if raw_runtime != "hosted":
        raise RuntimeError(
            "Invalid BS_RUNTIME. Hosted entry point requires "
            "BS_RUNTIME=hosted; refusing local execution to prevent SQLite or "
            "TaskRunner startup."
        )

    from borrowed_steps.config import load_settings
    from borrowed_steps.interfaces.http.app import create_app

    settings = load_settings(os.environ)
    if settings.runtime != "hosted":
        raise RuntimeError("Settings resolved runtime is not hosted; refusing startup.")

    _configure_logging()
    return create_app(settings)


app: FastAPI = init_app()
