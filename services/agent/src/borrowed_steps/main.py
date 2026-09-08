"""ASGI entry point.

Start with::

    python -m uvicorn borrowed_steps.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from borrowed_steps.interfaces.http.app import create_app

__all__ = ["app"]


def _configure_logging() -> None:
    """Optionally raise this service's own log level.

    ``uvicorn --log-level`` only configures the ``uvicorn.*`` loggers, so the
    service's grounding diagnostics are invisible without this. Set
    ``BS_LOG_LEVEL=INFO`` to see field names and reason codes. Those lines never
    contain intake text, candidate values or model reasoning.
    """
    level = os.environ.get("BS_LOG_LEVEL", "").strip().upper()
    if not level:
        return
    logger = logging.getLogger("borrowed_steps")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s %(message)s"))
        logger.addHandler(handler)


_configure_logging()

app: FastAPI = create_app()
