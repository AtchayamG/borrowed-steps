"""Reusable disposable PostgreSQL test fixtures and helpers for hosted runtime tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from borrowed_steps.infrastructure.postgres_migrations import (
    _SCHEMA_V1,
    EXPECTED_SCHEMA_VERSION,
    apply_migrations,
)


def get_test_postgres_url() -> str:
    """Return the base test PostgreSQL URL or skip the test if unset."""
    base = os.environ.get("BS_POSTGRES_TEST_URL")
    if not base:
        pytest.skip("BS_POSTGRES_TEST_URL not configured; real PostgreSQL proof not run")
    return base


@contextmanager
def disposable_database(base_url: str | None = None) -> Iterator[str]:
    """Create a temporary random database for one test case and force-drop it on exit."""
    base = base_url or get_test_postgres_url()
    db_name = "bs013_test_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    parts = urlsplit(base)
    url = urlunsplit(parts._replace(path="/" + db_name))
    try:
        yield url
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db_name)))


@contextmanager
def v1_database(base_url: str | None = None) -> Iterator[str]:
    """Create a disposable database, apply ONLY schema version 1, and yield URL."""
    with disposable_database(base_url) as url:
        with psycopg.connect(url) as conn, conn.transaction():
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations"
                " (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL)"
            )
            for stmt in _SCHEMA_V1:
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (1, now())")
        yield url


@contextmanager
def migrated_database(base_url: str | None = None) -> Iterator[str]:
    """Create a disposable database, apply schema migrations, and yield URL."""
    with disposable_database(base_url) as url:
        version = apply_migrations(url)
        msg = f"Expected migration version {EXPECTED_SCHEMA_VERSION}, got {version}"
        assert version == EXPECTED_SCHEMA_VERSION, msg
        yield url
