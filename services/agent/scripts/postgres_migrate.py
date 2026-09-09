"""Explicit admin migration; never print credentials or database error content."""

import os

from borrowed_steps.infrastructure.postgres_migrations import apply_migrations


def main() -> int:
    url = os.environ.get("BS_POSTGRES_ADMIN_URL")
    if not url:
        print("BS_POSTGRES_ADMIN_URL is required")
        return 2
    try:
        version = apply_migrations(url)
    except Exception:
        print("PostgreSQL migration failed; check configuration and database availability")
        return 1
    print(f"PostgreSQL schema version: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
