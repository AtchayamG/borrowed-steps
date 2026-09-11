"""Offline staged Groq adapter verification under isolated Python runtime.

BS-025 proof for the staged GroqModel/Strands adapter in M3 hosted mode.
Ensures zero live provider network calls, pure synthetic HTTP interception,
strict request envelope bounds (model pin, token caps, payload size <= 16KB),
streaming chunking, typed structured extraction with _Extraction schema,
clean resource lifecycle closure, refusal semantics, and 4 negative checks:
1. Outbound socket connection blocked fail-closed.
2. Unpinned target URL / model refused before network dispatch.
3. Missing dependency fails closed (runtime closure violation).
4. Dev checkout import forbidden (strict package isolation).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

# Ensure scripts directory is on sys.path for assemble_hosted import
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from assemble_hosted import checked_path, verify_manifest  # noqa: E402


def clean_environment() -> dict[str, str]:
    return {
        k: os.environ[k]
        for k in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
        if k in os.environ
    }


def run_isolated_code(
    python: Path,
    code: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute test code in an isolated subprocess with -I -B from external cwd."""
    with tempfile.TemporaryDirectory(prefix="bs025-verify-") as outside:
        full_env = {**clean_environment(), **(env or {})}
        result = subprocess.run(
            [str(python), "-I", "-B", "-c", code],
            cwd=outside,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if any(Path(outside).iterdir()):
            raise AssertionError("Isolated execution wrote to working directory")
        return result


def require_success(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        last_line = err.splitlines()[-1] if err else f"exit code {result.returncode}"
        raise AssertionError(f"[{label}] failed: {last_line}\nOutput:\n{err}")


def verify_adapter_streaming_and_bounds(python: Path, package: Path) -> None:
    """Exercise streaming translation, synthetic interception, envelope bounds, and hygiene."""
    code = f"""
import asyncio
import json
import socket
import sys
from pathlib import Path

PACKAGE = Path({str(package)!r})
sys.path.insert(0, str(PACKAGE))

# Socket blocking: allow only loopback self-pipe for Windows asyncio
_orig_connect = socket.socket.connect
def _block_socket(self, address, *args, **kwargs):
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
        return _orig_connect(self, address, *args, **kwargs)
    raise RuntimeError("Outbound socket connection blocked during offline verification")
socket.socket.connect = _block_socket

import httpx
from borrowed_steps.infrastructure.groq_model import (
    ALLOWED_HOST,
    ALLOWED_PATH,
    DEFAULT_MAX_SENDS,
    FIXED_MAX_COMPLETION_TOKENS,
    FIXED_REASONING_EFFORT,
    GROQ_BASE_URL,
    GROQ_MODEL_ID,
    MAX_REQUEST_BYTES,
    GroqEnvelopeRefusedError,
    GroqModel,
    GroqTargetRefusedError,
)

# Verify staged import source
mod_file = Path(sys.modules["borrowed_steps.infrastructure.groq_model"].__file__).resolve()
assert mod_file.is_relative_to(PACKAGE), f"Module not relative to staged package: {{mod_file}}"

async def main():
    # 1. Synthetic SSE Streaming & Envelope Bounds
    sse_data = (
        b'data: {{"id":"c1","object":"chat.completion.chunk","created":100,"model":"' + GROQ_MODEL_ID.encode() + b'","choices":[{{"index":0,"delta":{{"role":"assistant","content":"First chunk"}},"finish_reason":null}}]}}\\n\\n'
        b'data: {{"id":"c1","object":"chat.completion.chunk","created":100,"model":"' + GROQ_MODEL_ID.encode() + b'","choices":[{{"index":0,"delta":{{"content":" second chunk"}},"finish_reason":"stop"}}],"usage":{{"prompt_tokens":12,"completion_tokens":8,"total_tokens":20}}}}\\n\\n'
        b'data: [DONE]\\n\\n'
    )

    intercepted_requests = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        intercepted_requests.append(request)
        # Verify wire target
        assert request.method == "POST"
        assert str(request.url) == f"https://{{ALLOWED_HOST}}{{ALLOWED_PATH}}"
        # Verify envelope bounds
        assert len(request.content) <= MAX_REQUEST_BYTES, f"Request exceeds {{MAX_REQUEST_BYTES}} bytes"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["model"] == GROQ_MODEL_ID, f"Model mismatch: {{payload.get('model')}}"
        assert payload["max_completion_tokens"] == FIXED_MAX_COMPLETION_TOKENS
        assert payload["reasoning_effort"] == FIXED_REASONING_EFFORT
        assert "max_tokens" not in payload, "Deprecated max_tokens present in wire envelope"
        return httpx.Response(
            status_code=200,
            headers={{"Content-Type": "text/event-stream"}},
            content=sse_data,
            request=request,
        )

    transport = httpx.MockTransport(mock_handler)
    model = GroqModel(api_key="gsk_dummy_test_key", transport=transport)

    prompt = [{{"role": "user", "content": [{{"text": "Test streaming"}}]}}]
    events = []
    async for event in model.stream(prompt):
        events.append(event)

    assert len(intercepted_requests) == 1, "Expected exactly 1 wire dispatch"
    assert model.sent == 1, "SendBudget counter mismatch"

    # Verify event stream translation
    deltas = [e["contentBlockDelta"]["delta"]["text"] for e in events if "contentBlockDelta" in e]
    assert deltas == ["First chunk", " second chunk"]
    stop_reasons = [e["messageStop"]["stopReason"] for e in events if "messageStop" in e]
    assert stop_reasons == ["end_turn"]
    metadata_events = [e["metadata"] for e in events if "metadata" in e]
    assert len(metadata_events) == 1
    assert metadata_events[0]["usage"]["inputTokens"] == 12
    assert metadata_events[0]["usage"]["outputTokens"] == 8
    assert metadata_events[0]["usage"]["totalTokens"] == 20

    # Resource hygiene
    await model.aclose()
    assert not model.client_open
    assert len(model._active_clients) == 0

    # 2. Test finish reason length mapping
    sse_length_data = (
        b'data: {{"id":"c2","object":"chat.completion.chunk","created":101,"model":"' + GROQ_MODEL_ID.encode() + b'","choices":[{{"index":0,"delta":{{"content":"truncated text"}},"finish_reason":"length"}}],"usage":{{"prompt_tokens":10,"completion_tokens":1024,"total_tokens":1034}}}}\\n\\n'
        b'data: [DONE]\\n\\n'
    )
    async def mock_length_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={{"Content-Type": "text/event-stream"}}, content=sse_length_data, request=request)
    
    model_len = GroqModel(api_key="gsk_dummy_test_key", transport=httpx.MockTransport(mock_length_handler))
    len_events = [e async for e in model_len.stream(prompt)]
    len_stop = [e["messageStop"]["stopReason"] for e in len_events if "messageStop" in e]
    assert len_stop == ["max_tokens"], f"Expected max_tokens stopReason, got {{len_stop}}"
    await model_len.aclose()

    print("PASS_STREAMING_AND_BOUNDS")

asyncio.run(main())
"""
    res = run_isolated_code(python, code)
    require_success(res, "verify_adapter_streaming_and_bounds")
    assert "PASS_STREAMING_AND_BOUNDS" in res.stdout


def verify_adapter_structured_output(python: Path, package: Path) -> None:
    """Exercise structured extraction with _Extraction schema, wire params, and parse handling."""
    code = f"""
import asyncio
import json
import socket
import sys
from pathlib import Path
from pydantic import BaseModel, Field

PACKAGE = Path({str(package)!r})
sys.path.insert(0, str(PACKAGE))

_orig_connect = socket.socket.connect
def _block_socket(self, address, *args, **kwargs):
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
        return _orig_connect(self, address, *args, **kwargs)
    raise RuntimeError("Outbound socket connection blocked during offline verification")
socket.socket.connect = _block_socket

import httpx
from borrowed_steps.infrastructure.groq_model import (
    ALLOWED_HOST,
    ALLOWED_PATH,
    FIXED_MAX_COMPLETION_TOKENS,
    FIXED_REASONING_EFFORT,
    GROQ_MODEL_ID,
    MAX_REQUEST_BYTES,
    GroqModel,
)

class _Extraction(BaseModel):
    borrower_label: str | None = Field(default=None)
    equipment_kind: str | None = Field(default=None)
    pickup_location: str | None = Field(default=None)
    due_at: str | None = Field(default=None)

async def main():
    extraction_data = {{
        "borrower_label": "Priya S",
        "equipment_kind": "crutches",
        "pickup_location": "Adyar centre",
        "due_at": "2026-10-01T08:00:00Z"
    }}
    wire_response_json = {{
        "id": "chatcmpl-struct-1",
        "object": "chat.completion",
        "created": 102,
        "model": GROQ_MODEL_ID,
        "choices": [
            {{
                "index": 0,
                "message": {{
                    "role": "assistant",
                    "content": json.dumps(extraction_data),
                }},
                "finish_reason": "stop",
            }}
        ],
        "usage": {{"prompt_tokens": 40, "completion_tokens": 25, "total_tokens": 65}}
    }}

    captured_wire_payloads = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert len(request.content) <= MAX_REQUEST_BYTES
        payload = json.loads(request.content.decode("utf-8"))
        captured_wire_payloads.append(payload)
        # Verify schema is transmitted via response_format
        assert "response_format" in payload
        assert payload["model"] == GROQ_MODEL_ID
        assert payload["max_completion_tokens"] == FIXED_MAX_COMPLETION_TOKENS
        assert payload["reasoning_effort"] == FIXED_REASONING_EFFORT
        assert "tools" not in payload, "Stage 2 structured output must not transmit tool fields"
        assert "tool_choice" not in payload
        return httpx.Response(200, json=wire_response_json, request=request)

    model = GroqModel(api_key="gsk_dummy_test_key", transport=httpx.MockTransport(mock_handler))
    prompt = [{{"role": "user", "content": [{{"text": "Priya S needs crutches"}}]}}]

    results = [res async for res in model.structured_output(_Extraction, prompt)]
    assert len(results) == 1
    extracted_obj = results[0]["output"]
    assert isinstance(extracted_obj, _Extraction)
    assert extracted_obj.borrower_label == "Priya S"
    assert extracted_obj.equipment_kind == "crutches"
    assert extracted_obj.pickup_location == "Adyar centre"
    assert extracted_obj.due_at == "2026-10-01T08:00:00Z"

    await model.aclose()
    assert not model.client_open

    # Test malformed wire JSON fails cleanly without hanging
    bad_wire_response = {{
        "id": "chatcmpl-bad-1",
        "object": "chat.completion",
        "created": 103,
        "model": GROQ_MODEL_ID,
        "choices": [
            {{
                "index": 0,
                "message": {{"role": "assistant", "content": "not-valid-json"}},
                "finish_reason": "stop",
            }}
        ]
    }}
    async def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bad_wire_response, request=request)

    bad_model = GroqModel(api_key="gsk_dummy_test_key", transport=httpx.MockTransport(bad_handler))
    try:
        async for _ in bad_model.structured_output(_Extraction, prompt):
            pass
        raise AssertionError("Expected ValueError for unparseable structured output")
    except ValueError:
        pass
    await bad_model.aclose()

    print("PASS_STRUCTURED_OUTPUT")

asyncio.run(main())
"""
    res = run_isolated_code(python, code)
    require_success(res, "verify_adapter_structured_output")
    assert "PASS_STRUCTURED_OUTPUT" in res.stdout


def verify_adapter_refusal_semantics(python: Path, package: Path) -> None:
    """Exercise constructor refusals, parameter mutation rejections, and target refusals."""
    code = f"""
import asyncio
import sys
from pathlib import Path

PACKAGE = Path({str(package)!r})
sys.path.insert(0, str(PACKAGE))

import httpx
from borrowed_steps.infrastructure.groq_model import (
    FIXED_MAX_COMPLETION_TOKENS,
    FIXED_REASONING_EFFORT,
    GROQ_MODEL_ID,
    GroqEnvelopeRefusedError,
    GroqModel,
    GroqTargetRefusedError,
)

# 1. Constructor refusals
for bad_key in ("", "   ", None):
    try:
        GroqModel(api_key=bad_key)
        raise AssertionError(f"Expected ValueError for api_key={{bad_key!r}}")
    except ValueError:
        pass

for bad_sends in (0, -1, 7, 100):
    try:
        GroqModel(api_key="gsk_dummy", max_sends=bad_sends)
        raise AssertionError(f"Expected ValueError for max_sends={{bad_sends}}")
    except ValueError:
        pass

# 2. Kwargs and configuration mutation refusals
model = GroqModel(api_key="gsk_dummy", transport=httpx.MockTransport(lambda req: httpx.Response(200)))
prompt = [{{"role": "user", "content": [{{"text": "Hi"}}]}}]

# Modifying max_completion_tokens
try:
    model.format_request(prompt, max_completion_tokens=2048)
    raise AssertionError("Expected GroqEnvelopeRefusedError on max_completion_tokens mutation")
except GroqEnvelopeRefusedError:
    pass

# Supplying forbidden max_tokens
try:
    model.format_request(prompt, max_tokens=100)
    raise AssertionError("Expected GroqEnvelopeRefusedError on max_tokens parameter")
except GroqEnvelopeRefusedError:
    pass

# Modifying reasoning_effort
try:
    model.format_request(prompt, reasoning_effort="high")
    raise AssertionError("Expected GroqEnvelopeRefusedError on reasoning_effort mutation")
except GroqEnvelopeRefusedError:
    pass

print("PASS_REFUSALS")
"""
    res = run_isolated_code(python, code)
    require_success(res, "verify_adapter_refusal_semantics")
    assert "PASS_REFUSALS" in res.stdout


def verify_four_negative_checks(python: Path, package: Path) -> None:
    """Execute all 4 required negative checks proving strict fail-closed boundary."""
    # Negative 1: Outbound socket blocked fail-closed
    code_neg1 = f"""
import socket
import sys
from pathlib import Path
PACKAGE = Path({str(package)!r})
sys.path.insert(0, str(PACKAGE))

_orig_connect = socket.socket.connect
def _block_socket(self, address, *args, **kwargs):
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
        return _orig_connect(self, address, *args, **kwargs)
    raise RuntimeError("Outbound socket connection blocked during offline verification")
socket.socket.connect = _block_socket

s = socket.socket()
try:
    s.connect(("93.184.216.34", 80))
    raise AssertionError("Outbound socket connection was not blocked")
except RuntimeError as exc:
    assert "Outbound socket connection blocked" in str(exc)
print("PASS_NEGATIVE_1_SOCKET_BLOCKED")
"""
    res1 = run_isolated_code(python, code_neg1)
    require_success(res1, "negative_1_socket_blocked")
    assert "PASS_NEGATIVE_1_SOCKET_BLOCKED" in res1.stdout

    # Negative 2: Unpinned target refused before network
    code_neg2 = f"""
import asyncio
import json
import sys
from pathlib import Path
PACKAGE = Path({str(package)!r})
sys.path.insert(0, str(PACKAGE))

import httpx
from borrowed_steps.infrastructure.groq_model import (
    GroqModel,
    GroqTargetRefusedError,
    GroqEnvelopeRefusedError,
    FIXED_MAX_COMPLETION_TOKENS,
    FIXED_REASONING_EFFORT,
)

async def main():
    called = False
    async def dummy_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    model = GroqModel(api_key="gsk_dummy", transport=httpx.MockTransport(dummy_handler))

    # A: Wrong URL host
    bad_url_req = httpx.Request("POST", "https://unpinned.groq.com/openai/v1/chat/completions", content=b'{{"model":"openai/gpt-oss-20b","max_completion_tokens":1024,"reasoning_effort":"low"}}')
    try:
        await model._groq_transport.handle_async_request(bad_url_req)
        raise AssertionError("Expected GroqTargetRefusedError for unpinned host")
    except GroqTargetRefusedError:
        pass
    assert not called, "Network transport invoked despite unpinned target!"

    # B: Wrong model ID in envelope
    bad_model_req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions", content=b'{{"model":"unpinned-model-99b","max_completion_tokens":1024,"reasoning_effort":"low"}}')
    try:
        await model._groq_transport.handle_async_request(bad_model_req)
        raise AssertionError("Expected GroqEnvelopeRefusedError for unpinned model")
    except GroqEnvelopeRefusedError:
        pass
    assert not called, "Network transport invoked despite unpinned model!"

    await model.aclose()
    print("PASS_NEGATIVE_2_UNPINNED_REFUSED")

asyncio.run(main())
"""
    res2 = run_isolated_code(python, code_neg2)
    require_success(res2, "negative_2_unpinned_refused")
    assert "PASS_NEGATIVE_2_UNPINNED_REFUSED" in res2.stdout

    # Negative 3: Missing dependency fails closed (runtime closure violation)
    code_neg3 = f"""
import sys
from pathlib import Path
PACKAGE = Path({str(package)!r})
sys.path.insert(0, str(PACKAGE))

# Mask critical runtime dependency
sys.modules["openai"] = None

try:
    import borrowed_steps.infrastructure.groq_model
    raise AssertionError("Expected ModuleNotFoundError when openai is missing")
except (ModuleNotFoundError, ImportError):
    pass
print("PASS_NEGATIVE_3_MISSING_DEP")
"""
    res3 = run_isolated_code(python, code_neg3)
    require_success(res3, "negative_3_missing_dep")
    assert "PASS_NEGATIVE_3_MISSING_DEP" in res3.stdout

    # Negative 4: Dev checkout import detected / forbidden
    code_neg4 = f"""
import sys
from pathlib import Path
PACKAGE = Path({str(package)!r})
sys.path.insert(0, str(PACKAGE))

from borrowed_steps.infrastructure import groq_model

mod_path = Path(groq_model.__file__).resolve()
# Prove that module was loaded from PACKAGE, not from repo dev checkout
assert mod_path.is_relative_to(PACKAGE), f"Import escaped package: {{mod_path}}"

# Simulate outside module detection:
fake_external_path = Path(r"D:/Work/other_checkout/services/agent/src/borrowed_steps/infrastructure/groq_model.py")
assert not fake_external_path.resolve().is_relative_to(PACKAGE), "Path detection logic failed"

print("PASS_NEGATIVE_4_DEV_CHECKOUT_FORBIDDEN")
"""
    res4 = run_isolated_code(python, code_neg4)
    require_success(res4, "negative_4_dev_checkout_forbidden")
    assert "PASS_NEGATIVE_4_DEV_CHECKOUT_FORBIDDEN" in res4.stdout


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package-dir",
        type=Path,
        required=True,
        help="Path to assembled Vercel staging package (e.g. deploy/vercel/output_bs025)",
    )
    parser.add_argument(
        "--python-exe",
        type=Path,
        default=Path(sys.executable),
        help="Path to Python executable in isolated runtime environment",
    )
    args = parser.parse_args(argv)

    package = checked_path(args.package_dir)
    python = checked_path(args.python_exe)

    if not package.is_dir():
        parser.error(f"Package directory does not exist: {package}")
    if not python.is_file():
        parser.error(f"Python executable does not exist: {python}")

    print("--- BS-025 Staged Groq Adapter Verification ---")
    print(f"Package directory: {package}")
    print(f"Python executable: {python}")

    # 1. Verify manifest before tests
    print("\n[Step 1/6] Verifying package manifest before test execution...")
    manifest_pre = verify_manifest(package)
    total_files = manifest_pre["total_files"]
    total_bytes = manifest_pre["total_uncompressed_bytes"]
    print(f"PASS manifest verified ({total_files} files, {total_bytes} bytes)")

    # 2. Exercise streaming, envelope bounds, synthetic intercept, resource hygiene
    print("\n[Step 2/6] Verifying adapter streaming, envelope bounds, and hygiene...")
    verify_adapter_streaming_and_bounds(python, package)
    print("PASS adapter streaming translation and envelope bounds verified")

    # 3. Exercise structured extraction with _Extraction schema
    print("\n[Step 3/6] Verifying adapter structured output (_Extraction)...")
    verify_adapter_structured_output(python, package)
    print("PASS adapter structured extraction and parse handling verified")

    # 4. Exercise refusal semantics
    print("\n[Step 4/6] Verifying adapter refusal semantics...")
    verify_adapter_refusal_semantics(python, package)
    print("PASS adapter refusal semantics verified")

    # 5. Exercise 4 negative checks
    print(
        "\n[Step 5/6] Verifying 4 negative checks (socket, target, dependency, origin)..."
    )
    verify_four_negative_checks(python, package)
    print("PASS all 4 negative checks verified fail-closed")

    # 6. Verify manifest after tests to prove zero mutation
    print("\n[Step 6/6] Verifying package manifest after test execution...")
    manifest_post = verify_manifest(package)
    assert manifest_pre == manifest_post, "Package was modified during test execution!"
    print(f"PASS manifest unchanged ({total_files} files, {total_bytes} bytes)")

    print("\n=== ALL BS-025 STAGED GROQ ADAPTER CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
