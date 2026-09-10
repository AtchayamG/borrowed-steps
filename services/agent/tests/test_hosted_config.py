"""Unit tests for hosted configuration, validation matrix, and secret protection."""

from __future__ import annotations

import subprocess
import sys
import traceback
from pathlib import Path

import pytest

from borrowed_steps.config import (
    DEFAULT_ALLOWED_ORIGINS,
    DEFAULT_ASSISTANT_PROVIDER,
    DEFAULT_RUNTIME,
    DEFAULT_STORE,
    Settings,
    load_settings,
    validate_settings,
)

VALID_HOSTED_ENV = {
    "BS_RUNTIME": "hosted",
    "BS_STORE": "postgres",
    "BS_DATABASE_URL": "postgresql://postgres@127.0.0.1:54339/postgres",
    "BS_COOKIE_SECURE": "1",
    "BS_ALLOWED_ORIGINS": "https://borrowed-steps.example",
    "BS_TASKS_ENABLED": "0",
    "BS_ASSISTANT_PROVIDER": "groq",
}


def test_default_local_configuration() -> None:
    settings = load_settings({})
    assert settings.runtime == DEFAULT_RUNTIME == "local"
    assert settings.store == DEFAULT_STORE == "sqlite"
    assert settings.assistant_provider == DEFAULT_ASSISTANT_PROVIDER == "ollama"
    assert settings.tasks_enabled is True
    assert settings.assistant_enabled is False
    assert settings.cookie_secure is False
    assert settings.database_url is None
    assert settings.allowed_origins == tuple(DEFAULT_ALLOWED_ORIGINS.split(","))
    validate_settings(settings)


def test_valid_hosted_configuration() -> None:
    settings = load_settings(VALID_HOSTED_ENV)
    assert settings.runtime == "hosted"
    assert settings.store == "postgres"
    assert settings.assistant_provider == "groq"
    assert settings.tasks_enabled is False
    assert settings.assistant_enabled is False
    assert settings.cookie_secure is True
    assert settings.database_url == "postgresql://postgres@127.0.0.1:54339/postgres"
    assert settings.allowed_origins == ("https://borrowed-steps.example",)
    validate_settings(settings)


def test_hosted_assistant_provider_defaults_to_groq() -> None:
    env = {k: v for k, v in VALID_HOSTED_ENV.items() if k != "BS_ASSISTANT_PROVIDER"}
    settings = load_settings(env)
    assert settings.assistant_provider == "groq"
    validate_settings(settings)


@pytest.mark.parametrize(
    ("key", "val", "match"),
    [
        ("BS_DATABASE_URL", "", "BS_DATABASE_URL"),
        ("BS_COOKIE_SECURE", "0", "BS_COOKIE_SECURE"),
        ("BS_TASKS_ENABLED", "1", "BS_TASKS_ENABLED"),
        ("BS_ASSISTANT_ENABLED", "1", "Hosted assistant is disabled"),
        ("BS_STORE", "sqlite", "BS_STORE=postgres"),
        ("BS_ASSISTANT_PROVIDER", "ollama", "BS_ASSISTANT_PROVIDER=groq"),
        ("BS_RUNTIME", "cloud", "Invalid BS_RUNTIME"),
        ("BS_STORE", "mysql", "BS_STORE=postgres"),
        ("BS_ASSISTANT_PROVIDER", "anthropic", "BS_ASSISTANT_PROVIDER=groq"),
    ],
)
def test_hosted_invalid_or_missing_env_fails_closed(key: str, val: str, match: str) -> None:
    env = dict(VALID_HOSTED_ENV)
    if val == "":
        env.pop(key, None)
    else:
        env[key] = val
    with pytest.raises(ValueError, match=match):
        load_settings(env)


def test_hosted_missing_database_url_fails() -> None:
    env = dict(VALID_HOSTED_ENV)
    del env["BS_DATABASE_URL"]
    with pytest.raises(ValueError, match="BS_DATABASE_URL"):
        load_settings(env)


def test_hosted_missing_cookie_secure_fails() -> None:
    env = dict(VALID_HOSTED_ENV)
    del env["BS_COOKIE_SECURE"]
    with pytest.raises(ValueError, match="BS_COOKIE_SECURE"):
        load_settings(env)


def test_hosted_missing_tasks_enabled_fails() -> None:
    env = dict(VALID_HOSTED_ENV)
    del env["BS_TASKS_ENABLED"]
    with pytest.raises(ValueError, match="BS_TASKS_ENABLED"):
        load_settings(env)


def test_hosted_missing_allowed_origins_fails() -> None:
    env = dict(VALID_HOSTED_ENV)
    del env["BS_ALLOWED_ORIGINS"]
    with pytest.raises(ValueError, match="BS_ALLOWED_ORIGINS"):
        load_settings(env)


def test_local_rejects_postgres_or_groq() -> None:
    with pytest.raises(ValueError, match="Local runtime requires BS_STORE=sqlite"):
        load_settings({"BS_STORE": "postgres"})
    with pytest.raises(ValueError, match="Local runtime requires BS_ASSISTANT_PROVIDER=ollama"):
        load_settings({"BS_ASSISTANT_PROVIDER": "groq"})


def test_direct_settings_construction_bypass_prevention() -> None:
    # Attempting to construct hosted settings with local defaults must fail
    with pytest.raises(ValueError, match="store='postgres'"):
        Settings(
            db_path=Path("test.db"),
            allowed_origins=("https://borrowed-steps.example",),
            cookie_secure=True,
            runtime="hosted",
            store="sqlite",
            database_url="postgresql://127.0.0.1/db",
            tasks_enabled=False,
            assistant_provider="groq",
        )

    with pytest.raises(ValueError, match="cookie_secure=True"):
        Settings(
            db_path=Path("test.db"),
            allowed_origins=("https://borrowed-steps.example",),
            cookie_secure=False,
            runtime="hosted",
            store="postgres",
            database_url="postgresql://127.0.0.1/db",
            tasks_enabled=False,
            assistant_provider="groq",
        )

    with pytest.raises(ValueError, match="tasks_enabled=False"):
        Settings(
            db_path=Path("test.db"),
            allowed_origins=("https://borrowed-steps.example",),
            cookie_secure=True,
            runtime="hosted",
            store="postgres",
            database_url="postgresql://127.0.0.1/db",
            tasks_enabled=True,
            assistant_provider="groq",
        )

    with pytest.raises(ValueError, match="Hosted assistant is disabled"):
        Settings(
            db_path=Path("test.db"),
            allowed_origins=("https://borrowed-steps.example",),
            cookie_secure=True,
            runtime="hosted",
            store="postgres",
            database_url="postgresql://127.0.0.1/db",
            tasks_enabled=False,
            assistant_enabled=True,
            assistant_provider="groq",
        )

    with pytest.raises(ValueError, match="explicit database_url"):
        Settings(
            db_path=Path("test.db"),
            allowed_origins=("https://borrowed-steps.example",),
            cookie_secure=True,
            runtime="hosted",
            store="postgres",
            database_url=None,
            tasks_enabled=False,
            assistant_provider="groq",
        )

    with pytest.raises(ValueError, match="assistant_provider='groq'"):
        Settings(
            db_path=Path("test.db"),
            allowed_origins=("https://borrowed-steps.example",),
            cookie_secure=True,
            runtime="hosted",
            store="postgres",
            database_url="postgresql://127.0.0.1/db",
            tasks_enabled=False,
            assistant_provider="ollama",
        )

    with pytest.raises(ValueError, match="Local runtime requires store='sqlite'"):
        Settings(
            db_path=Path("test.db"),
            allowed_origins=("http://127.0.0.1:5173",),
            cookie_secure=False,
            runtime="local",
            store="postgres",
        )

    with pytest.raises(ValueError, match="Local runtime requires assistant_provider='ollama'"):
        Settings(
            db_path=Path("test.db"),
            allowed_origins=("http://127.0.0.1:5173",),
            cookie_secure=False,
            runtime="local",
            assistant_provider="groq",
        )


@pytest.mark.parametrize(
    "bad_origin",
    [
        "",
        "   ",
        "http://borrowed-steps.example",
        "https://*.borrowed-steps.example",
        "https://user:pass@borrowed-steps.example",
        "https://borrowed-steps.example/path",
        "https://borrowed-steps.example/",
        "https://borrowed-steps.example?query=1",
        "https://borrowed-steps.example#fragment",
        "https://localhost",
        "https://localhost:8443",
        "https://127.0.0.1",
        "https://127.0.0.1:8443",
        "https://127.0.0.2",
        "https://[::1]",
        "https://[::1]:8443",
        "https://borrowed-steps.example:0",
        "https://borrowed-steps.example:65536",
        "https://borrowed-steps.example:abc",
        " https://borrowed-steps.example ",
        "https://foo bar.example",
        "https://foo%2ebar.example",
        "https://localhost.",
        "https://app.localhost",
        "https://2130706433",
        "https://127.1",
        "https://0x7f000001",
        "https://-bad.example",
    ],
)
def test_hosted_origin_allowlist_rejection(bad_origin: str) -> None:
    with pytest.raises(ValueError, match="Hosted"):
        Settings(
            db_path=Path("test.db"),
            allowed_origins=(bad_origin,),
            cookie_secure=True,
            runtime="hosted",
            store="postgres",
            database_url="postgresql://127.0.0.1/db",
            tasks_enabled=False,
            assistant_provider="groq",
        )
    if bad_origin != " https://borrowed-steps.example ":
        env = dict(VALID_HOSTED_ENV, BS_ALLOWED_ORIGINS=bad_origin)
        with pytest.raises(ValueError, match="Hosted"):
            load_settings(env)


def test_hosted_origin_allowlist_valid() -> None:
    env = dict(
        VALID_HOSTED_ENV,
        BS_ALLOWED_ORIGINS="https://borrowed-steps.example,https://app.example.org:8443",
    )
    settings = load_settings(env)
    assert settings.allowed_origins == (
        "https://borrowed-steps.example",
        "https://app.example.org:8443",
    )
    validate_settings(settings)


def test_secrets_absent_from_repr_and_errors() -> None:
    secret_pass = "super_secret_pg_pass_998877"
    secret_dsn = f"postgresql://user:{secret_pass}@db.internal:5432/proddb"
    settings = Settings(
        db_path=Path("test.db"),
        allowed_origins=("https://borrowed-steps.example",),
        cookie_secure=True,
        runtime="hosted",
        store="postgres",
        database_url=secret_dsn,
        tasks_enabled=False,
        assistant_provider="groq",
    )

    settings_repr = repr(settings)
    settings_str = str(settings)
    assert secret_pass not in settings_repr
    assert secret_pass not in settings_str
    assert "database_url" not in settings_repr
    assert "database_url" not in settings_str

    # Test that origin credential rejection does not reflect credentials
    with pytest.raises(
        ValueError, match="Hosted allowed origin cannot contain credentials"
    ) as exc_info:
        load_settings(
            dict(
                VALID_HOSTED_ENV,
                BS_ALLOWED_ORIGINS="https://admin:my_credential@borrowed-steps.example",
            )
        )
    assert "my_credential" not in str(exc_info.value)


@pytest.mark.parametrize(
    "key", ["BS_RUNTIME", "BS_STORE", "BS_ASSISTANT_PROVIDER", "BS_ALLOWED_ORIGINS"]
)
def test_invalid_values_not_reflected_in_traceback(key: str) -> None:
    value = "synthetic-private-value"
    if key == "BS_ALLOWED_ORIGINS":
        value = "https://example.org:" + value
    with pytest.raises(ValueError, match=r"Invalid|Hosted") as exc_info:
        load_settings(dict(VALID_HOSTED_ENV, **{key: value}))
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert value not in rendered


@pytest.mark.parametrize("key", ["BS_TASKS_ENABLED", "BS_COOKIE_SECURE", "BS_ASSISTANT_ENABLED"])
def test_unknown_hosted_flags_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="explicit boolean"):
        load_settings(dict(VALID_HOSTED_ENV, **{key: "typo"}))


def test_local_factory_without_optional_driver_or_provider_imports(tmp_path: Path) -> None:
    code = """
import builtins
import sys
from pathlib import Path
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'psycopg', 'strands', 'ollama', 'openai'}:
        raise AssertionError('Optional dependency imported: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from borrowed_steps.config import Settings
from borrowed_steps.interfaces.http.app import create_app
app = create_app(Settings(Path(sys.argv[1]), (), False, tasks_enabled=False))
assert app.state.tasks is None
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "local.db")],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_task_tick_token_configuration_and_redaction() -> None:
    # 1. Unset by default (None) in both local and hosted
    local_settings = load_settings({})
    assert local_settings.task_tick_token is None

    hosted_settings = load_settings(VALID_HOSTED_ENV)
    assert hosted_settings.task_tick_token is None

    # 2. Valid token 32 to 256 URL-safe characters
    token_32 = "a" * 32
    token_256 = "b" * 256
    token_url_safe = "Abc-123_xyz-789_ABC-XYZ-0123456789"
    for tok in (token_32, token_256, token_url_safe):
        s = load_settings(dict(VALID_HOSTED_ENV, BS_TASK_TICK_TOKEN=tok))
        assert s.task_tick_token == tok
        assert tok not in repr(s)
        assert tok not in str(s)
        assert "task_tick_token" not in repr(s)
        assert "task_tick_token" not in str(s)

    # 3. Invalid tokens fail closed without echoing token in error message
    secret_bad_token = "short_secret_with_private_bits"
    with pytest.raises(ValueError, match="BS_TASK_TICK_TOKEN") as exc_info:
        load_settings(dict(VALID_HOSTED_ENV, BS_TASK_TICK_TOKEN=secret_bad_token))
    assert secret_bad_token not in str(exc_info.value)
    assert secret_bad_token not in "".join(traceback.format_exception(exc_info.value))

    # Reject > 256
    oversized = "a" * 257
    with pytest.raises(ValueError, match="BS_TASK_TICK_TOKEN"):
        load_settings(dict(VALID_HOSTED_ENV, BS_TASK_TICK_TOKEN=oversized))

    # Reject non-ASCII / non-URL-safe characters
    for invalid in [
        "a" * 31 + " ",  # whitespace at end (no trimming)
        " " + "a" * 31,  # whitespace at start (no trimming)
        "a" * 31 + "@",  # non-URL-safe char
        "a" * 31 + "/",  # slash
        "a" * 31 + "\n",  # newline
        "a" * 31 + "\u00e9",  # non-ASCII unicode
    ]:
        with pytest.raises(ValueError, match="BS_TASK_TICK_TOKEN"):
            load_settings(dict(VALID_HOSTED_ENV, BS_TASK_TICK_TOKEN=invalid))
