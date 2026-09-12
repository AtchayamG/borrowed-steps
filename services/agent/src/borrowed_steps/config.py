"""Runtime configuration; private database credentials never appear in errors."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

__all__ = [
    "ASSISTANT_DEADLINE_SECONDS",
    "ASSISTANT_FRAMEWORK",
    "ASSISTANT_INFERENCE_DEADLINE_SECONDS",
    "ASSISTANT_MAX_MODEL_REQUESTS",
    "ASSISTANT_MAX_TOOL_CALLS",
    "ASSISTANT_PROVIDER",
    "ASSISTANT_TRANSPORT_TIMEOUT_SECONDS",
    "DEFAULT_ALLOWED_ORIGINS",
    "DEFAULT_ASSISTANT_HOST",
    "DEFAULT_ASSISTANT_MODEL",
    "DEFAULT_ASSISTANT_PROVIDER",
    "DEFAULT_DB_PATH",
    "DEFAULT_RUNTIME",
    "DEFAULT_STORE",
    "TASK_STOP_TIMEOUT_SECONDS",
    "TASK_TICK_CANDIDATE_LIMIT",
    "TASK_TICK_SECONDS",
    "VALID_ASSISTANT_PROVIDERS",
    "VALID_RUNTIMES",
    "VALID_STORES",
    "Settings",
    "load_settings",
    "validate_settings",
]

DEFAULT_DB_PATH = "data/borrowed_steps.db"
DEFAULT_ALLOWED_ORIGINS = "http://127.0.0.1:5173,http://localhost:5173"
DEFAULT_RUNTIME = "local"
DEFAULT_STORE = "sqlite"
DEFAULT_ASSISTANT_PROVIDER = "ollama"

VALID_RUNTIMES = ("local", "hosted")
VALID_STORES = ("sqlite", "postgres")
VALID_ASSISTANT_PROVIDERS = ("ollama", "groq")

# Fixed by docs/M2A_CONTRACT.md: an explicit local endpoint and the one installed
# model. These are not configurable, so no environment can point production at a
# remote host or a different model.
DEFAULT_ASSISTANT_HOST = "http://127.0.0.1:11434"
DEFAULT_ASSISTANT_MODEL = "llama3.2:3b"

# Fixed by docs/M2A_CONTRACT.md, not configurable at runtime.
ASSISTANT_FRAMEWORK = "strands"
ASSISTANT_PROVIDER = "ollama"
ASSISTANT_DEADLINE_SECONDS = 115.0
# Inside the 115 s wait, so the adapter ends its own run before the route's
# outer guard fires and can never leave an orphan inference behind.
ASSISTANT_INFERENCE_DEADLINE_SECONDS = 110.0
# Finite transport timeout. The ollama client defaults to None, which would let a
# stalled provider hold the single inference slot indefinitely.
ASSISTANT_TRANSPORT_TIMEOUT_SECONDS = 60.0
ASSISTANT_MAX_MODEL_REQUESTS = 6
ASSISTANT_MAX_TOOL_CALLS = 2
# How long shutdown waits for in-flight interpretations to wind down after they
# are signalled and cancelled. Bounded on purpose: a run that will not stop must
# not hang the process. What actually happened is reported either way.
ASSISTANT_SHUTDOWN_SECONDS = 10.0
# How long a timed-out request waits for its own cancelled run to finish winding
# down before answering 504. Kept inside the 120.0-second total response bound:
# 115.0s wait + 5.0s cancel grace = 120.0s total maximum response time.
ASSISTANT_CANCEL_GRACE_SECONDS = 5.0

# Fixed by docs/M2B_CONTRACT.md. The runner ticks immediately at startup and
# then waits this long between ticks, interruptibly.
TASK_TICK_SECONDS = 30.0
# How long shutdown waits for the runner thread to leave its tick. Bounded on
# purpose, and the outcome is reported: a thread that will not stop is said to
# be still running, never counted as clean cleanup.
TASK_STOP_TIMEOUT_SECONDS = 10.0
# Most tasks one tick will consider. It bounds the work a single tick holds the
# writer for; a longer backlog is simply worked oldest-first over more ticks.
TASK_TICK_CANDIDATE_LIMIT = 100


def _validate_hosted_origins(origins: tuple[str, ...] | Sequence[str]) -> None:
    """Ensure origins strictly satisfy hosted HTTPS allowlist requirements.

    Rejects wildcards, credentials, paths, queries, fragments, invalid ports,
    and localhost/loopback origins. Error messages are sanitized and contain no
    reflected input or secrets.
    """
    if not origins:
        raise ValueError("Hosted runtime requires a non-empty allowed_origins allowlist.")
    for origin in origins:
        if not origin or not isinstance(origin, str) or not origin.strip():
            raise ValueError("Hosted allowed origin must be a non-empty string.")
        if origin.strip() != origin:
            raise ValueError("Hosted allowed origin contains surrounding whitespace.")
        if "*" in origin:
            raise ValueError("Hosted allowed origin cannot contain wildcards.")
        if "@" in origin:
            raise ValueError("Hosted allowed origin cannot contain credentials.")
        try:
            parts = urlsplit(origin)
        except ValueError:
            raise ValueError("Hosted allowed origin is invalid.") from None
        if parts.scheme != "https":
            raise ValueError("Hosted allowed origin must use HTTPS scheme.")
        if parts.path:
            raise ValueError("Hosted allowed origin cannot include a path.")
        if parts.query:
            raise ValueError("Hosted allowed origin cannot include a query.")
        if parts.fragment:
            raise ValueError("Hosted allowed origin cannot include a fragment.")
        if not parts.hostname:
            raise ValueError("Hosted allowed origin must include a valid host.")
        hostname = parts.hostname.lower()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise ValueError("Hosted allowed origin cannot be localhost or loopback.")
        try:
            address = ip_address(hostname)
        except ValueError:
            if not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}",
                hostname,
            ) or hostname.endswith("-"):
                raise ValueError(
                    "Hosted allowed origin must include a valid public host."
                ) from None
        else:
            if not address.is_global:
                raise ValueError("Hosted allowed origin must include a public IP address.")
        try:
            port = parts.port
        except ValueError:
            raise ValueError("Hosted allowed origin port is invalid.") from None
        if port is not None and not (1 <= port <= 65535):
            raise ValueError("Hosted allowed origin port out of range (1-65535).")
        host = f"[{hostname}]" if ":" in hostname else hostname
        expected = f"https://{host}" if port is None else f"https://{host}:{port}"
        if origin != expected:
            raise ValueError("Hosted allowed origin is not in exact canonical format.")


def validate_settings(settings: Settings) -> None:
    """Validate Settings consistency across local and hosted runtimes."""
    if settings.runtime not in VALID_RUNTIMES:
        raise ValueError("Invalid runtime; expected local or hosted.")
    if settings.store not in VALID_STORES:
        raise ValueError("Invalid store; expected sqlite or postgres.")
    if settings.assistant_provider not in VALID_ASSISTANT_PROVIDERS:
        raise ValueError("Invalid assistant provider; expected ollama or groq.")

    if settings.runtime == "local":
        if settings.store != "sqlite":
            raise ValueError("Local runtime requires store='sqlite'.")
        if settings.assistant_provider != "ollama":
            raise ValueError("Local runtime requires assistant_provider='ollama'.")
    elif settings.runtime == "hosted":
        if settings.store != "postgres":
            raise ValueError("Hosted runtime requires store='postgres'.")
        if settings.assistant_provider != "groq":
            raise ValueError("Hosted runtime requires assistant_provider='groq'.")
        if not settings.database_url or not settings.database_url.strip():
            raise ValueError("Hosted runtime requires an explicit database_url.")
        if settings.cookie_secure is not True:
            raise ValueError("Hosted runtime requires cookie_secure=True.")
        if settings.tasks_enabled is not False:
            raise ValueError("Hosted runtime requires tasks_enabled=False.")
        if settings.assistant_enabled and (
            not settings.groq_api_key or not settings.groq_api_key.strip()
        ):
            raise ValueError(
                "Hosted runtime requires an explicit groq_api_key when assistant is enabled."
            )
        _validate_hosted_origins(settings.allowed_origins)

    if settings.task_tick_token is not None and not (
        32 <= len(settings.task_tick_token) <= 256
        and bool(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", settings.task_tick_token))
    ):
        raise ValueError("BS_TASK_TICK_TOKEN must be 32 to 256 ASCII URL-safe characters.")


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the service needs to start."""

    db_path: Path
    allowed_origins: tuple[str, ...]
    cookie_secure: bool
    assistant_enabled: bool = False
    assistant_host: str = DEFAULT_ASSISTANT_HOST
    assistant_model: str = DEFAULT_ASSISTANT_MODEL
    # Coordination processing is on by default: it needs no provider, makes no
    # network call and costs nothing. Tests that assert exact event counts turn
    # it off so their counts stay about the action under test.
    tasks_enabled: bool = True
    runtime: str = DEFAULT_RUNTIME
    store: str = DEFAULT_STORE
    database_url: str | None = field(default=None, repr=False)
    assistant_provider: str = DEFAULT_ASSISTANT_PROVIDER
    task_tick_token: str | None = field(default=None, repr=False)
    groq_api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        validate_settings(self)


def _flag(value: str | None, *, default: bool = False) -> bool:
    """Read a boolean environment flag.

    An unset or blank value keeps the caller's default. A value that is present
    but not recognised as true is false, so an explicit ``BS_TASKS_ENABLED=0``
    turns a default-on flag off.
    """
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _pinned(source: Mapping[str, str], key: str, fixed: str, label: str) -> str:
    """Refuse any attempt to move the assistant off its pinned local provider.

    The contract fixes the endpoint and the model. Rather than silently ignoring
    a mismatched environment variable, this fails loudly at startup: a silent
    substitution is exactly the failure mode the contract forbids.
    """
    supplied = source.get(key)
    if supplied is not None and supplied.strip() != fixed:
        msg = (
            f"{key} cannot be changed: M2A pins the {label} to {fixed}. "
            f"Unset {key} or set it to that exact value."
        )
        raise ValueError(msg)
    return fixed


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from the ``BS_`` environment variables.

    ``BS_ASSISTANT_ENABLED`` defaults to false: the structured workflow is fully
    usable with no provider installed and no inference of any kind.

    The assistant endpoint and model are pinned by the contract and cannot be
    overridden; a mismatched value raises ``ValueError`` rather than being
    ignored. Tests inject an interpreter through ``create_app`` instead.
    """
    source = os.environ if env is None else env

    raw_runtime = source.get("BS_RUNTIME", DEFAULT_RUNTIME).strip().lower()
    if raw_runtime not in VALID_RUNTIMES:
        raise ValueError("Invalid BS_RUNTIME; expected local or hosted.")

    raw_store = source.get("BS_STORE")
    if raw_runtime == "hosted":
        if raw_store is None or not raw_store.strip():
            raise ValueError("Hosted runtime requires explicitly selected BS_STORE=postgres.")
        store = raw_store.strip().lower()
        if store != "postgres":
            raise ValueError("Hosted runtime requires BS_STORE=postgres.")
    else:
        store = raw_store.strip().lower() if raw_store and raw_store.strip() else DEFAULT_STORE
        if store != "sqlite":
            raise ValueError("Local runtime requires BS_STORE=sqlite.")

    raw_provider = source.get("BS_ASSISTANT_PROVIDER")
    if raw_runtime == "hosted":
        provider = raw_provider.strip().lower() if raw_provider and raw_provider.strip() else "groq"
        if provider != "groq":
            raise ValueError("Hosted runtime requires BS_ASSISTANT_PROVIDER=groq.")
    else:
        provider = (
            raw_provider.strip().lower()
            if raw_provider and raw_provider.strip()
            else DEFAULT_ASSISTANT_PROVIDER
        )
        if provider != "ollama":
            raise ValueError("Local runtime requires BS_ASSISTANT_PROVIDER=ollama.")

    if raw_runtime == "hosted":
        for key in ("BS_TASKS_ENABLED", "BS_COOKIE_SECURE", "BS_ASSISTANT_ENABLED"):
            value = source.get(key)
            if value is not None and value.strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
                "1",
                "true",
                "yes",
                "on",
            }:
                raise ValueError(f"Hosted {key} must be an explicit boolean.")
        raw_tasks = source.get("BS_TASKS_ENABLED")
        if raw_tasks is None or not raw_tasks.strip():
            raise ValueError("Hosted runtime requires BS_TASKS_ENABLED=0 explicitly configured.")
        tasks_enabled = _flag(raw_tasks, default=True)
        if tasks_enabled:
            raise ValueError("Hosted runtime requires BS_TASKS_ENABLED=0.")
    else:
        tasks_enabled = _flag(source.get("BS_TASKS_ENABLED"), default=True)

    if raw_runtime == "hosted":
        raw_cookie = source.get("BS_COOKIE_SECURE")
        if raw_cookie is None or not raw_cookie.strip():
            raise ValueError("Hosted runtime requires BS_COOKIE_SECURE=1.")
        cookie_secure = _flag(raw_cookie, default=False)
        if not cookie_secure:
            raise ValueError("Hosted runtime requires BS_COOKIE_SECURE=1.")
    else:
        cookie_secure = _flag(source.get("BS_COOKIE_SECURE"))

    raw_assistant = source.get("BS_ASSISTANT_ENABLED")
    assistant_enabled = _flag(raw_assistant, default=False)
    if raw_runtime == "hosted" and assistant_enabled:
        raw_groq_key_check = source.get("BS_GROQ_API_KEY")
        if raw_groq_key_check is None or not raw_groq_key_check.strip():
            raise ValueError("Hosted assistant requires an explicit BS_GROQ_API_KEY.")

    if raw_runtime == "hosted":
        raw_origins = source.get("BS_ALLOWED_ORIGINS")
        if raw_origins is None or not raw_origins.strip():
            raise ValueError("Hosted runtime requires explicit non-empty BS_ALLOWED_ORIGINS.")
        allowed_origins = tuple(o.strip() for o in raw_origins.split(",") if o.strip())
        _validate_hosted_origins(allowed_origins)
    else:
        origins = source.get("BS_ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS)
        allowed_origins = tuple(o.strip() for o in origins.split(",") if o.strip())

    database_url: str | None
    if raw_runtime == "hosted":
        db_url = source.get("BS_DATABASE_URL")
        if db_url is None or not db_url.strip():
            raise ValueError("Hosted runtime requires an explicit BS_DATABASE_URL.")
        database_url = db_url.strip()
    else:
        database_url = source.get("BS_DATABASE_URL")

    raw_groq_key = source.get("BS_GROQ_API_KEY")
    groq_api_key = raw_groq_key.strip() if raw_groq_key and raw_groq_key.strip() else None

    return Settings(
        db_path=Path(source.get("BS_DB_PATH", DEFAULT_DB_PATH)),
        allowed_origins=allowed_origins,
        cookie_secure=cookie_secure,
        assistant_enabled=assistant_enabled,
        tasks_enabled=tasks_enabled,
        assistant_host=_pinned(source, "BS_ASSISTANT_HOST", DEFAULT_ASSISTANT_HOST, "endpoint"),
        assistant_model=_pinned(source, "BS_ASSISTANT_MODEL", DEFAULT_ASSISTANT_MODEL, "model"),
        runtime=raw_runtime,
        store=store,
        database_url=database_url,
        assistant_provider=provider,
        task_tick_token=source.get("BS_TASK_TICK_TOKEN"),
        groq_api_key=groq_api_key,
    )
