"""Packaging regression tests independent of ignored local staging artifacts."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from verify_hosted_groq import run_isolated_code  # noqa: E402


def test_runtime_pins_match_accepted_locks() -> None:
    pins = {}
    for name in ("requirements.lock", "requirements-groq.lock", "requirements-postgres.lock"):
        raw = (REPO_ROOT / "services/agent" / name).read_bytes()
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        for line in raw.decode(encoding).splitlines():
            if line and not line.startswith(("#", "-")):
                req = Requirement(line)
                pins[canonicalize_name(req.name)] = req.specifier
    for line in (REPO_ROOT / "deploy/vercel/requirements.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        req = Requirement(line)
        if req.name != "tzdata":  # pre-existing platform pin outside backend locks
            assert req.specifier == pins[canonicalize_name(req.name)]
        if req.name == "pywin32":
            assert req.marker is not None
            assert not req.marker.evaluate({"sys_platform": "linux"})


@pytest.mark.parametrize(
    "operation",
    [
        "socket.socket().connect(('203.0.113.1', 80))",
        "socket.socket().connect_ex(('203.0.113.1', 80))",
        "socket.getaddrinfo('example.invalid', 443)",
        "socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('203.0.113.1', 80))",
    ],
)
def test_offline_guard_prevents_network(operation: str) -> None:
    result = run_isolated_code(Path(sys.executable), "import socket\n" + operation)
    assert result.returncode != 0
    assert "Outbound socket connection blocked" in result.stderr


def test_isolation_guard_rejects_outside_module(tmp_path: Path) -> None:
    code = f"""from pathlib import Path
import sys
from types import ModuleType
PACKAGE = Path({str(tmp_path)!r})
module = ModuleType('borrowed_steps.injected')
module.__file__ = str(PACKAGE.parent / 'outside.py')
sys.modules[module.__name__] = module
"""
    result = run_isolated_code(Path(sys.executable), code)
    assert result.returncode != 0
    assert "Import escaped package" in result.stderr


def test_child_ignores_ambient_pythonpath() -> None:
    result = run_isolated_code(
        Path(sys.executable),
        "import sys\nassert 'outside-checkout-sentinel' not in str(sys.path)",
        {"PYTHONPATH": "outside-checkout-sentinel"},
    )
    assert result.returncode == 0
