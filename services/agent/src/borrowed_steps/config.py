"""Runtime configuration, read from the environment. No secrets are involved."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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
    "DEFAULT_DB_PATH",
    "TASK_STOP_TIMEOUT_SECONDS",
    "TASK_TICK_CANDIDATE_LIMIT",
    "TASK_TICK_SECONDS",
    "Settings",
    "load_settings",
]

DEFAULT_DB_PATH = "data/borrowed_steps.db"
DEFAULT_ALLOWED_ORIGINS = "http://127.0.0.1:5173,http://localhost:5173"
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
    origins = source.get("BS_ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS)
    return Settings(
        db_path=Path(source.get("BS_DB_PATH", DEFAULT_DB_PATH)),
        allowed_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
        cookie_secure=_flag(source.get("BS_COOKIE_SECURE")),
        assistant_enabled=_flag(source.get("BS_ASSISTANT_ENABLED")),
        tasks_enabled=_flag(source.get("BS_TASKS_ENABLED"), default=True),
        assistant_host=_pinned(source, "BS_ASSISTANT_HOST", DEFAULT_ASSISTANT_HOST, "endpoint"),
        assistant_model=_pinned(source, "BS_ASSISTANT_MODEL", DEFAULT_ASSISTANT_MODEL, "model"),
    )
