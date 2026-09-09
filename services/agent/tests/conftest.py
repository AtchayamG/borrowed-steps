"""Fixtures. Every test runs against a real file-backed SQLite database."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from borrowed_steps.config import Settings
from borrowed_steps.interfaces.http.app import create_app
from support import ALLOWED_ORIGIN, NOW


class FakeClock:
    """Controllable clock, so session expiry and due dates are deterministic."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "borrowed_steps.db"


@pytest.fixture
def settings(db_path: Path) -> Settings:
    """Baseline settings, with the coordination runner off.

    ``tasks_enabled`` really defaults to true; it is turned off here so that
    the tests asserting exact event counts and lifecycle behaviour stay about
    the action under test rather than racing a background thread. The runner's
    own tests and the smoke script switch it back on, and one test asserts the
    production default is true.
    """
    return Settings(
        db_path=db_path,
        allowed_origins=(ALLOWED_ORIGIN,),
        cookie_secure=False,
        tasks_enabled=False,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def client(settings: Settings, clock: FakeClock) -> Iterator[TestClient]:
    with TestClient(create_app(settings, clock=clock)) as test_client:
        yield test_client
