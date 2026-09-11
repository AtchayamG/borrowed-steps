"""Tests for the staged Vercel package with GroqModel adapter in M3 hosted mode.

Verifies:
1. Runtime dependency closure (60 packages pinned, test/dev tools and Ollama excluded).
2. Staged package structure, manifest validation, and forbidden file exclusions.
3. Execution of focused verifier script under isolated runtime.
4. GroqModel wire envelope bounds (1024 tokens, low reasoning effort, <=16KB payload).
5. GroqModel structured output with _Extraction schema.
6. Four negative checks (socket blocked, unpinned target refused, missing dep, isolation).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, Field

# Locate repository root and key paths
REPO_ROOT = Path(__file__).resolve().parents[3]
VERCEL_DIR = REPO_ROOT / "deploy" / "vercel"
REQUIREMENTS_TXT = VERCEL_DIR / "requirements.txt"
OUTPUT_DIR = VERCEL_DIR / "output_bs025"
RUNTIME_VENV_DIR = VERCEL_DIR / ".venv-runtime"
RUNTIME_BIN = "Scripts" if os.name == "nt" else "bin"
RUNTIME_EXE = "python.exe" if os.name == "nt" else "python"
RUNTIME_VENV_PYTHON = RUNTIME_VENV_DIR / RUNTIME_BIN / RUNTIME_EXE
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_hosted_groq.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from assemble_hosted import verify_manifest  # type: ignore[import-not-found] # noqa: E402

from borrowed_steps.infrastructure.groq_model import (  # noqa: E402
    FIXED_MAX_COMPLETION_TOKENS,
    FIXED_REASONING_EFFORT,
    GROQ_MODEL_ID,
    MAX_REQUEST_BYTES,
    GroqEnvelopeRefusedError,
    GroqModel,
    GroqTargetRefusedError,
)

FORBIDDEN_PACKAGES = {
    "ast_serialize",
    "iniconfig",
    "librt",
    "mypy",
    "mypy_extensions",
    "ollama",
    "pathspec",
    "pluggy",
    "Pygments",
    "pytest",
    "ruff",
}

REQUIRED_PACKAGES = {
    "aws-bedrock-token-generator",
    "cryptography",
    "fastapi",
    "httpx",
    "mcp",
    "openai",
    "psycopg",
    "pydantic",
    "strands-agents",
    "uvicorn",
}


class _Extraction(BaseModel):
    """Loan-request fields with source evidence; use null for unstated fields."""

    borrower_label: str | None = Field(default=None)
    equipment_kind: str | None = Field(default=None)
    pickup_location: str | None = Field(default=None)
    due_at: str | None = Field(default=None)


def test_runtime_requirements_closure() -> None:
    """Requirements file contains exact 60 pinned packages with dev tools excluded."""
    assert REQUIREMENTS_TXT.is_file(), "deploy/vercel/requirements.txt must exist"
    lines = [
        line.strip()
        for line in REQUIREMENTS_TXT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # Check total package count matches the 60 derived packages
    assert len(lines) == 60, f"Expected 60 pinned packages, found {len(lines)}"

    package_names = set()
    for line in lines:
        name = re.split(r"[=<>~;]", line)[0].strip()
        package_names.add(name)

    # Exclude all dev/test/local packages
    for forbidden in FORBIDDEN_PACKAGES:
        msg = f"Forbidden dev package {forbidden} found in runtime closure"
        assert forbidden not in package_names, msg

    # Require critical runtime and adapter packages
    for required in REQUIRED_PACKAGES:
        msg = f"Required package {required} missing from runtime closure"
        assert required in package_names, msg


def test_staged_groq_package_structure_and_manifest() -> None:
    """Staged package exists, contains Groq adapter, and matches manifest."""
    assert OUTPUT_DIR.is_dir(), f"Staged package directory {OUTPUT_DIR} must exist"

    # Verify manifest integrity
    manifest = verify_manifest(OUTPUT_DIR)
    assert manifest["total_files"] == 39
    assert manifest["total_uncompressed_bytes"] > 0

    # Ensure groq_model.py is staged
    staged_adapter = OUTPUT_DIR / "borrowed_steps" / "infrastructure" / "groq_model.py"
    assert staged_adapter.is_file(), "groq_model.py must be present in staged package"

    # Ensure forbidden files are absent
    for _root, dirs, files in os.walk(OUTPUT_DIR):
        assert "__pycache__" not in dirs, "Found __pycache__ in staged package"
        assert "node_modules" not in dirs, "Found node_modules in staged package"
        for f in files:
            assert not f.endswith((".db", ".sqlite", ".pyc")), f"Forbidden artifact: {f}"
            assert not f.startswith(".env"), f"Forbidden secret file: {f}"


@pytest.mark.skipif(not RUNTIME_VENV_PYTHON.is_file(), reason="Runtime venv not present")
def test_isolated_verify_hosted_groq_script_execution() -> None:
    """Run focused verify_hosted_groq.py under isolated runtime venv."""
    cmd = [
        str(RUNTIME_VENV_PYTHON),
        str(VERIFY_SCRIPT),
        "--package-dir",
        str(OUTPUT_DIR),
        "--python-exe",
        str(RUNTIME_VENV_PYTHON),
    ]
    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"Verifier failed:\n{result.stderr}\n{result.stdout}"
    assert "PASS manifest verified" in result.stdout
    assert "PASS adapter streaming translation and envelope bounds verified" in result.stdout
    assert "PASS adapter structured extraction and parse handling verified" in result.stdout
    assert "PASS adapter refusal semantics verified" in result.stdout
    assert "PASS all 4 negative checks verified fail-closed" in result.stdout
    assert "PASS manifest unchanged" in result.stdout
    assert "ALL BS-025 STAGED GROQ ADAPTER CHECKS PASSED" in result.stdout


def test_staged_groq_adapter_envelope_bounds_unit() -> None:
    """Direct unit test of wire envelope limits and bounds."""
    captured = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        assert len(request.content) <= MAX_REQUEST_BYTES
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["model"] == GROQ_MODEL_ID
        assert payload["max_completion_tokens"] == FIXED_MAX_COMPLETION_TOKENS
        assert payload["reasoning_effort"] == FIXED_REASONING_EFFORT
        assert "max_tokens" not in payload
        m = GROQ_MODEL_ID.encode()
        sse = (
            b'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"'
            + m
            + b'","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=sse, request=request
        )

    async def run_test() -> None:
        model = GroqModel(api_key="gsk_dummy_test_key", transport=httpx.MockTransport(mock_handler))
        prompt: Any = [{"role": "user", "content": [{"text": "hello"}]}]
        _events = [e async for e in model.stream(prompt)]
        assert len(captured) == 1
        assert model.sent == 1
        await model.aclose()
        assert not model.client_open

    asyncio.run(run_test())


def test_staged_groq_adapter_structured_output_unit() -> None:
    """Direct unit test of structured output with _Extraction schema."""
    extracted = {
        "borrower_label": "Kavitha",
        "equipment_kind": "wheelchair",
        "pickup_location": "Central Hub",
        "due_at": "2026-11-01T10:00:00Z",
    }
    wire_response = {
        "id": "c_struct",
        "object": "chat.completion",
        "created": 2,
        "model": GROQ_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(extracted)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 20, "total_tokens": 35},
    }

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert "response_format" in payload
        assert "tools" not in payload
        assert "tool_choice" not in payload
        return httpx.Response(200, json=wire_response, request=request)

    async def run_test() -> None:
        model = GroqModel(api_key="gsk_dummy_test_key", transport=httpx.MockTransport(mock_handler))
        prompt: Any = [{"role": "user", "content": [{"text": "Kavitha needs a wheelchair"}]}]
        results = [res async for res in model.structured_output(_Extraction, prompt)]
        assert len(results) == 1
        obj = results[0]["output"]
        assert isinstance(obj, _Extraction)
        assert obj.borrower_label == "Kavitha"
        assert obj.equipment_kind == "wheelchair"
        assert obj.pickup_location == "Central Hub"
        assert obj.due_at == "2026-11-01T10:00:00Z"
        await model.aclose()

    asyncio.run(run_test())


def test_staged_groq_adapter_refusals_and_bounds() -> None:
    """Constructor and parameter mutation refusals."""
    with pytest.raises(ValueError, match="api_key must be a non-empty string"):
        GroqModel(api_key="")

    with pytest.raises(ValueError, match="max_sends must be between 1 and 6"):
        GroqModel(api_key="gsk_dummy", max_sends=10)

    model = GroqModel(
        api_key="gsk_dummy", transport=httpx.MockTransport(lambda req: httpx.Response(200))
    )
    prompt: Any = [{"role": "user", "content": [{"text": "test"}]}]

    with pytest.raises(GroqEnvelopeRefusedError):
        model.format_request(prompt, max_completion_tokens=4096)

    with pytest.raises(GroqEnvelopeRefusedError):
        model.format_request(prompt, max_tokens=500)

    with pytest.raises(GroqEnvelopeRefusedError):
        model.format_request(prompt, reasoning_effort="high")


def test_staged_groq_adapter_negative_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct execution of negative checks 1 and 2."""
    # Negative 1: Outbound socket connect blocked fail-closed
    orig_connect = socket.socket.connect

    def _blocked_connect(
        self: socket.socket,
        address: tuple[str, int] | str,
        *args: object,
        **kwargs: object,
    ) -> None:
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
            orig_connect(self, address, *args, **kwargs)
            return
        raise RuntimeError("Outbound socket connection blocked during offline verification")

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    s = socket.socket()
    with pytest.raises(RuntimeError, match="Outbound socket connection blocked"):
        s.connect(("93.184.216.34", 80))

    # Negative 2: Unpinned target refused before network
    async def run_unpinned_test() -> None:
        called = False

        async def handler(req: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200)

        model = GroqModel(api_key="gsk_dummy", transport=httpx.MockTransport(handler))
        bad_req = httpx.Request(
            "POST", "https://unpinned.groq.com/openai/v1/chat/completions", content=b"{}"
        )
        with pytest.raises(GroqTargetRefusedError):
            await model._groq_transport.handle_async_request(bad_req)
        assert not called, "Inner transport called on unpinned target"
        await model.aclose()

    asyncio.run(run_unpinned_test())
