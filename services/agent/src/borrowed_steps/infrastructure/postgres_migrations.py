"""Explicit, additive schema migrations for the PostgreSQL adapter.

Three rules hold this module together:

* **Nothing here runs implicitly.** ``PostgresStore`` never migrates. An
  operator calls :func:`apply_migrations` on purpose, with a URL passed in as
  an argument; this module never discovers a credential of its own.
* **One writer at a time, and only for the duration of the work.** The lock is
  ``pg_advisory_xact_lock``, which the transaction releases when it ends -
  including when the process dies mid-migration. A *session* advisory lock
  would outlive a transaction-mode pooler's idea of a session and could be
  handed to the wrong backend, so it is not used.
* **Additive and repeatable.** A version already recorded in
  ``schema_migrations`` is skipped, so a second, third or concurrent run is a
  no-op rather than an error. No migration seeds a workspace or any other row
  of application data: re-running must never resurrect or duplicate content.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import psycopg
from psycopg.rows import TupleRow

__all__ = [
    "EXPECTED_SCHEMA_VERSION",
    "SchemaVersionError",
    "apply_migrations",
    "read_schema_version",
]

EXPECTED_SCHEMA_VERSION = 1
"""Schema version this build of the adapter requires."""

# A fixed, arbitrary 64-bit key. Advisory locks share one namespace per
# database, so the value only has to be stable and unlikely to collide with
# another application's.
_MIGRATION_LOCK_KEY = 8_801_103_311_011

_CONNECT_TIMEOUT_S = 15
_LOCK_TIMEOUT_MS = 30_000
_STATEMENT_TIMEOUT_MS = 60_000


class SchemaVersionError(RuntimeError):
    """The database schema is not the version this build expects."""


def require_transport_security(database_url: str) -> None:
    """Require an explicit URI; reject libpq routing overrides and ambient hosts."""
    try:
        parts = urlsplit(database_url)
        query = dict(parse_qsl(parts.query, strict_parsing=True))
        host = parts.hostname
        valid = (
            parts.scheme in {"postgres", "postgresql"}
            and bool(host)
            and not parts.fragment
            and set(query) <= {"sslmode", "sslrootcert", "channel_binding"}
        )
        secure = host in {"127.0.0.1", "localhost", "::1"} or query.get("sslmode") == "verify-full"
    except ValueError:
        valid = secure = False
    if not valid or not secure:
        raise ValueError(
            "PostgreSQL requires an explicit host URI and verified TLS outside loopback"
        )


# Version 1 is the whole current M1/M2B shape. The SQLite database reached it
# in two steps because it existed before coordination tasks did; a fresh
# PostgreSQL database has no such history, so the contract asks for one
# migration that lands on today's structure. Later changes append a version -
# they never edit the tuple below.
#
# Deliberate choices worth naming:
#
# ``seq BIGSERIAL`` stands in for SQLite's ``rowid``. Three orderings in the
# adapter are insertion order rather than a timestamp - equipment as seeded,
# events newest first, and the most recent returned loan for an item - and a
# whole-second ``created_at`` cannot break those ties. PostgreSQL has no
# implicit row order, so the column has to be explicit.
#
# ``response_body TEXT`` is not an oversight: an idempotent replay must return
# the original bytes. JSONB would normalise key order and number formatting,
# and the replayed body would no longer be the body the client already saw.
#
# The composite unique indexes exist so the composite foreign keys can point at
# them. Those keys are what makes the *database* refuse a loan whose request
# belongs to another workspace - containment enforced at rest, not by every
# caller remembering to check.
_SCHEMA_V1: tuple[str, ...] = (
    """
    CREATE TABLE workspaces (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE equipment (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        label TEXT NOT NULL,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        seq BIGSERIAL NOT NULL
    )
    """,
    "CREATE INDEX ix_equipment_workspace ON equipment(workspace_id, seq)",
    "CREATE UNIQUE INDEX ux_equipment_workspace_id ON equipment(workspace_id, id)",
    """
    CREATE TABLE requests (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        borrower_label TEXT NOT NULL,
        equipment_kind TEXT NOT NULL,
        pickup_location TEXT NOT NULL,
        due_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX ix_requests_workspace ON requests(workspace_id, created_at, id)",
    "CREATE UNIQUE INDEX ux_requests_workspace_id ON requests(workspace_id, id)",
    """
    CREATE TABLE loans (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        request_id TEXT NOT NULL,
        equipment_id TEXT NOT NULL,
        status TEXT NOT NULL,
        due_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        seq BIGSERIAL NOT NULL,
        FOREIGN KEY (workspace_id, request_id) REFERENCES requests(workspace_id, id),
        FOREIGN KEY (workspace_id, equipment_id) REFERENCES equipment(workspace_id, id)
    )
    """,
    "CREATE INDEX ix_loans_workspace ON loans(workspace_id, created_at, id)",
    "CREATE UNIQUE INDEX ux_loans_workspace_id ON loans(workspace_id, id)",
    "CREATE INDEX ix_loans_equipment_seq ON loans(workspace_id, equipment_id, seq DESC)",
    # The durable backstop against two open loans on one item. It is a partial
    # index, so a RETURNED or CLOSED loan does not occupy the slot and the same
    # item can be lent again. Serialising on the workspace row is what normally
    # prevents the race; this is what makes the race impossible.
    """
    CREATE UNIQUE INDEX ux_loans_active_equipment
        ON loans(equipment_id) WHERE status IN ('RESERVED', 'ON_LOAN')
    """,
    """
    CREATE TABLE events (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        at TIMESTAMPTZ NOT NULL,
        seq BIGSERIAL NOT NULL
    )
    """,
    "CREATE INDEX ix_events_workspace ON events(workspace_id, seq DESC)",
    """
    CREATE TABLE idempotency (
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        key TEXT NOT NULL,
        route TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        status_code INTEGER NOT NULL,
        response_body TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (workspace_id, key)
    )
    """,
    """
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        loan_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('PICKUP_DUE', 'RETURN_DUE')),
        status TEXT NOT NULL CHECK (status IN ('PENDING', 'DUE', 'RESOLVED')),
        due_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        FOREIGN KEY (workspace_id, loan_id) REFERENCES loans(workspace_id, id)
    )
    """,
    "CREATE UNIQUE INDEX ux_tasks_loan_kind ON tasks(loan_id, kind)",
    "CREATE INDEX ix_tasks_pending_due ON tasks(status, due_at, id)",
    "CREATE INDEX ix_tasks_workspace ON tasks(workspace_id, created_at, id)",
    "COMMENT ON COLUMN idempotency.response_body IS"
    " 'Exact stored response bytes. TEXT, never JSONB: a replay must not be reserialised.'",
)

_MIGRATIONS: tuple[tuple[str, ...], ...] = (_SCHEMA_V1,)


def _connect(database_url: str) -> psycopg.Connection[TupleRow]:
    """One direct, short-lived admin connection.

    ``prepare_threshold=None`` conservatively disables prepared statements
    without assuming pooler configuration. ``autocommit=False`` matters here:
    the advisory lock and the DDL have to share one transaction, or the lock
    would be released before the work it guards.
    """
    require_transport_security(database_url)
    return psycopg.connect(
        database_url,
        autocommit=False,
        connect_timeout=_CONNECT_TIMEOUT_S,
        prepare_threshold=None,
    )


def read_schema_version(database_url: str) -> int:
    """The highest applied migration version, or ``0`` on an empty database.

    Read-only and lock-free, so a health check can call it without ever
    becoming a writer. ``to_regclass`` answers "does this table exist" without
    raising, which keeps "not migrated yet" a normal answer rather than an
    error to catch.
    """
    conn = _safe_connect(database_url)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT_MS}ms'")
            cursor.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
            present = cursor.fetchone()
            if present is None or not present[0]:
                return 0
            cursor.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
            row = cursor.fetchone()
            return 0 if row is None else int(row[0])
    except psycopg.Error:
        raise RuntimeError("PostgreSQL schema check failed") from None
    finally:
        conn.close()


def apply_migrations(database_url: str) -> int:
    """Bring the schema up to :data:`EXPECTED_SCHEMA_VERSION`; return the version.

    Safe to run on a fresh database, on an up-to-date one, and from two
    processes at once. The second caller blocks on the advisory lock, then
    finds every version already recorded and applies nothing - it does not
    fail, and it does not re-seed anything, because no migration writes
    application data.

    Everything commits once, at the end. A failure part-way through rolls the
    whole attempt back, so the database is never left on half a version.
    """
    conn = _safe_connect(database_url)
    try:
        with conn.cursor() as cursor:
            # Bounded from the first statement: a migration that cannot get the
            # lock, or that runs away, must give up rather than wedge the
            # database. SET LOCAL scopes both to this transaction.
            cursor.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT_MS}ms'")
            cursor.execute(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT_MS}ms'")
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations"
                " (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL)"
            )
            cursor.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
            row = cursor.fetchone()
            applied = 0 if row is None else int(row[0])
            if applied > EXPECTED_SCHEMA_VERSION:
                raise SchemaVersionError("PostgreSQL schema is newer than this build")
            now = datetime.now(UTC)
            for version, statements in enumerate(_MIGRATIONS, start=1):
                if version <= applied:
                    continue
                for statement in statements:
                    cursor.execute(statement)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                    (version, now),
                )
        conn.commit()
        return max(applied, len(_MIGRATIONS))
    except psycopg.Error:
        conn.rollback()
        raise RuntimeError("PostgreSQL migration failed") from None
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _safe_connect(database_url: str) -> psycopg.Connection[TupleRow]:
    try:
        return _connect(database_url)
    except psycopg.Error:
        raise RuntimeError("PostgreSQL schema connection failed") from None
