"""ASGI entry point.

Start with::

    python -m uvicorn borrowed_steps.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI

from borrowed_steps.interfaces.http.app import create_app

__all__ = ["app"]

app: FastAPI = create_app()
