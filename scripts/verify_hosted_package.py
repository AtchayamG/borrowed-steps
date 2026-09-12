"""Offline staged-package verification. No claim of Vercel build or routing proof."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from assemble_hosted import checked_path, verify_manifest


def clean_environment() -> dict[str, str]:
    return {
        k: os.environ[k]
        for k in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
        if k in os.environ
    }


def run_child(
    python: Path,
    package: Path,
    code: str,
    env: dict[str, str] | None = None,
    *,
    allow_db: bool = False,
) -> subprocess.CompletedProcess[str]:
    # -I ignores PYTHONPATH/user site; -B leaves the verified stage unchanged.
    prelude = f"import sys, json; from pathlib import Path; PACKAGE = Path({str(package)!r}); sys.path.insert(0, str(PACKAGE))\n"
    prelude += "import sqlite3\ndef no_sqlite(*a, **kw): raise AssertionError('SQLite access forbidden')\nsqlite3.connect = no_sqlite\n"
    if not allow_db:
        prelude += "import psycopg\ndef no_db(*a, **kw): raise AssertionError('Database access forbidden')\npsycopg.connect = no_db\n"
    postlude = """
for name, module in tuple(sys.modules.items()):
    if name == 'app' or name == 'borrowed_steps' or name.startswith('borrowed_steps.'):
        assert Path(module.__file__).resolve().is_relative_to(PACKAGE), name
assert 'borrowed_steps.main' not in sys.modules
assert not any(name.split('.')[0] in {'strands', 'ollama', 'openai', 'boto3'} for name in sys.modules)
if 'app' in sys.modules:
    from app import app
    assert app.state.tasks is None
    import asyncio
    async def check_lifespan():
        async with app.router.lifespan_context(app):
            assert app.state.tasks is None
    asyncio.run(check_lifespan())
"""
    with tempfile.TemporaryDirectory(prefix="bs017-outside-") as outside:
        result = subprocess.run(
            [str(python), "-I", "-B", "-c", prelude + code + postlude],
            cwd=outside,
            env={**clean_environment(), **(env or {})},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert not any(Path(outside).iterdir()), (
            "Staged app wrote into external working directory"
        )
        return result


def hosted_env() -> dict[str, str]:
    return {
        "BS_RUNTIME": "hosted",
        "BS_STORE": "postgres",
        "BS_DATABASE_URL": "postgresql://synthetic@127.0.0.1:1/postgres",
        "BS_COOKIE_SECURE": "1",
        "BS_ALLOWED_ORIGINS": "https://borrowed-steps.example",
        "BS_TASKS_ENABLED": "0",
        "BS_ASSISTANT_ENABLED": "0",
        "BS_TASK_TICK_TOKEN": "test-token-123456789012345678901234",
    }


def require_success(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode:
        # Child tracebacks may contain supplied DSNs. Keep failure details local,
        # and report only the error class to the evidence stream.
        last = result.stderr.strip().splitlines()
        error = last[-1].split(":", 1)[0] if last else "child failure"
        raise AssertionError(f"Isolated verification failed: {error}")


def test_frontend_assets(package: Path) -> None:
    assert (package / "public/index.html").stat().st_size > 0
    for suffix in ("css", "js"):
        assets = list((package / "public/assets").glob(f"*.{suffix}"))
        assert assets and all(p.stat().st_size > 0 for p in assets)
    assert json.loads((package / "vercel.json").read_text()) == {
        "$schema": "https://openapi.vercel.sh/vercel.json"
    }
    print("PASS static artifacts present; native platform routing UNRUN")


def test_runtime_dependency_closure(python: Path, package: Path) -> None:
    result = run_child(
        python,
        package,
        """
import importlib.metadata as metadata
assert sys.version_info[:2] == (3, 12)
def normalized(name): return name.lower().replace('_', '-')
expected = {}
for line in (PACKAGE / 'requirements.txt').read_text().splitlines():
    if not line.strip() or line.startswith('#'): continue
    pin, _, marker = line.partition(';')
    if marker and sys.platform != 'win32': continue
    name, version = pin.strip().split('==')
    expected[normalized(name)] = version
actual = {normalized(d.metadata['Name']): d.version for d in metadata.distributions()}
assert actual == expected, 'Runtime closure differs from exact pinned package set'
files = {Path(d.locate_file(f)).resolve() for d in metadata.distributions() for f in (d.files or [])}
total = sum(p.stat().st_size for p in files if p.is_file())
print(json.dumps({'python': sys.version.split()[0], 'platform': sys.platform, 'installed_packages': actual, 'installed_distribution_bytes': total}, sort_keys=True))
""",
    )
    require_success(result)
    print(result.stdout.strip())
    with tempfile.TemporaryDirectory(prefix="bs017-pip-") as outside:
        result = subprocess.run(
            ["uv", "pip", "check", "--python", str(python)],
            cwd=outside,
            env=clean_environment(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    require_success(result)
    print("PASS exact Python3.12 runtime closure and dependency metadata check")


def test_staged_import_and_config_refusal(python: Path, package: Path) -> None:
    env = hosted_env()
    cases = [
        ({}, "Missing BS_RUNTIME"),
        ({**env, "BS_RUNTIME": "local"}, "refusing local execution"),
        ({**env, "BS_TASKS_ENABLED": "1"}, "BS_TASKS_ENABLED=0"),
        ({**env, "BS_ASSISTANT_ENABLED": "1"}, "BS_GROQ_API_KEY"),
        (
            {k: v for k, v in env.items() if k != "BS_DATABASE_URL"},
            "requires an explicit BS_DATABASE_URL",
        ),
        ({**env, "BS_RUNTIME": "sensitive-sentinel"}, "Invalid BS_RUNTIME"),
        ({**env, "BS_LOG_LEVEL": "sensitive-sentinel"}, "Invalid BS_LOG_LEVEL"),
    ]
    for config, expected in cases:
        result = run_child(python, package, "from app import app", config)
        assert result.returncode != 0 and expected in result.stderr, expected
        assert "sensitive-sentinel" not in result.stderr
    print("PASS seven configuration refusals; SQLite/Postgres access guarded")


def test_health_and_auth(python: Path, package: Path) -> None:
    result = run_child(
        python,
        package,
        _ASGI_CLIENT_SNIPPET
        + """
from app import app
client = AsgiTestClient(app)
health = client.request('GET', '/api/health')
assert health.status_code == 200
assert health.json()['milestone'] == 'M3' and health.json()['agent_mode'] == 'disabled'
invalid = client.request('GET', '/api/nonexistent/route')
assert invalid.status_code == 404 and 'application/json' in invalid.headers['content-type']
assert '<html' not in invalid.text.lower()
for method, path in [('POST', '/api/internal/tasks/tick'), ('GET', '/api/internal/tasks/status')]:
    for headers in [None, {'Authorization': 'Bearer wrong-token'}, [('authorization', 'Bearer test-token-123456789012345678901234')] * 2]:
        assert client.request(method, path, headers=headers).status_code == 401
""",
        hosted_env(),
    )
    require_success(result)
    print(
        "PASS isolated import/health/JSON404/lifespan; six unauthorized requests, zero DB/provider access"
    )


def validate_pg_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"postgresql", "postgres"}
            and parsed.hostname in {"127.0.0.1", "::1"}
            and parsed.port is not None
            and not parsed.query
            and not parsed.fragment
            and parsed.path == "/postgres"
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "Test database must use an explicit numeric loopback host/port and /postgres, without URL options"
        )


_ASGI_CLIENT_SNIPPET = r"""
import asyncio
import json
from urllib.parse import urlsplit

class AsgiResponse:
    def __init__(self, status_code, headers, body, cookies):
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self.cookies = cookies

    @property
    def text(self):
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.body.decode("utf-8"))

class AsgiTestClient:
    def __init__(self, app, base_origin="https://borrowed-steps.example"):
        self.app = app
        self.base_origin = base_origin
        self.cookies = {}

    def request(self, method, path, headers=None, json_body=None, body=None):
        return asyncio.run(self._request_async(method, path, headers, json_body, body))

    async def _request_async(self, method, path, headers=None, json_body=None, body=None):
        content = b""
        req_headers = []
        if json_body is not None:
            content = json.dumps(json_body).encode("utf-8")
            req_headers.append((b"content-type", b"application/json"))
        elif body is not None:
            content = body
        if headers:
            if isinstance(headers, dict):
                for k, v in headers.items():
                    req_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
            else:
                for k, v in headers:
                    req_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
        if self.cookies:
            cookie_val = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            req_headers.append((b"cookie", cookie_val.encode("latin-1")))
        res_status = 0
        res_headers = {}
        res_body = bytearray()
        parsed = urlsplit(path)
        raw_path = parsed.path.encode("ascii")
        query_string = parsed.query.encode("ascii")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "https",
            "method": method.upper(),
            "path": parsed.path,
            "raw_path": raw_path,
            "query_string": query_string,
            "headers": req_headers,
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 50000),
        }
        body_sent = False
        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": content, "more_body": False}
            return {"type": "http.disconnect"}
        async def send(message):
            nonlocal res_status
            if message["type"] == "http.response.start":
                res_status = message["status"]
                for hk, hv in message.get("headers", []):
                    k_str = hk.decode("latin-1").lower()
                    v_str = hv.decode("latin-1")
                    res_headers[k_str] = v_str
                    if k_str == "set-cookie":
                        parts = v_str.split(";")[0].split("=", 1)
                        if len(parts) == 2:
                            self.cookies[parts[0].strip()] = parts[1].strip()
            elif message["type"] == "http.response.body":
                res_body.extend(message.get("body", b""))
        await self.app(scope, receive, send)
        return AsgiResponse(res_status, res_headers, bytes(res_body), dict(self.cookies))
"""


def test_real_postgresql_smoke(
    python_exe: Path, package_dir: Path, base_pg_url: str
) -> None:
    validate_pg_url(base_pg_url)  # before even loading the database driver
    import psycopg
    from psycopg import sql

    disposable_db_name = "bs017_smoke_" + uuid.uuid4().hex
    parsed = urlsplit(base_pg_url)
    disposable_db_url = urlunsplit(parsed._replace(path="/" + disposable_db_name))
    with psycopg.connect(base_pg_url, autocommit=True, connect_timeout=5) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(disposable_db_name))
        )
    try:
        migration_script = f"from borrowed_steps.infrastructure.postgres_migrations import apply_migrations; assert apply_migrations({disposable_db_url!r}) == 4"
        require_success(
            run_child(python_exe, package_dir, migration_script, allow_db=True)
        )

        smoke_script = (
            _ASGI_CLIENT_SNIPPET
            + f"""
import os, sys, json
from datetime import datetime, timedelta, timezone
os.environ['BS_RUNTIME'] = 'hosted'
os.environ['BS_STORE'] = 'postgres'
os.environ['BS_DATABASE_URL'] = {disposable_db_url!r}
os.environ['BS_COOKIE_SECURE'] = '1'
os.environ['BS_ALLOWED_ORIGINS'] = 'https://borrowed-steps.example'
os.environ['BS_TASKS_ENABLED'] = '0'
os.environ['BS_ASSISTANT_ENABLED'] = '0'
os.environ['BS_ASSISTANT_PROVIDER'] = 'groq'
os.environ['BS_TASK_TICK_TOKEN'] = 'test-token-123456789012345678901234'


from app import app

ORIGIN = 'https://borrowed-steps.example'
client = AsgiTestClient(app, base_origin=ORIGIN)

# 1. Create Workspace
ws_res = client.request('POST', '/api/workspaces', json_body={{}}, headers={{'Origin': ORIGIN}})
assert ws_res.status_code == 201, f'Workspace creation failed: {{ws_res.status_code}}'
ws_data = ws_res.json()
assert 'workspace' in ws_data
assert 'snapshot' in ws_data
assert ws_data['snapshot']['agent_mode'] == 'disabled'
assert len(ws_data['snapshot']['equipment']) == 3

avail_items = [e for e in ws_data['snapshot']['equipment'] if e['state'] == 'AVAILABLE']
assert len(avail_items) >= 1
eq_item = avail_items[0]
eq_id = eq_item['id']

# 2. Get Snapshot
snap_res = client.request('GET', '/api/snapshot', headers={{'Origin': ORIGIN}})
assert snap_res.status_code == 200

# 3. Create Request with Idempotency-Key
req_body = {{
    'borrower_label': 'Alex',
    'equipment_kind': 'WHEELCHAIR',
    'pickup_location': 'Station North',
    'due_at': (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
}}
req_res = client.request(
    'POST',
    '/api/requests',
    json_body=req_body,
    headers={{'Origin': ORIGIN, 'Idempotency-Key': 'req-key-001'}},
)
assert req_res.status_code == 200, f'Request create failed: {{req_res.text}}'
req_data = req_res.json()
req_id = req_data['request']['id']

# 4. Exact Idempotency Replay
replay_res = client.request(
    'POST',
    '/api/requests',
    json_body=req_body,
    headers={{'Origin': ORIGIN, 'Idempotency-Key': 'req-key-001'}},
)
assert replay_res.status_code == 200
assert replay_res.body == req_res.body

# 5. Idempotency Conflict (different payload, same key)
conflict_res = client.request(
    'POST',
    '/api/requests',
    json_body={{**req_body, 'borrower_label': 'Different'}},
    headers={{'Origin': ORIGIN, 'Idempotency-Key': 'req-key-001'}},
)
assert conflict_res.status_code == 409

# 6. Human Approval Refusal
refuse_res = client.request(
    'POST',
    '/api/reservations',
    json_body={{
        'request_id': req_id,
        'equipment_id': eq_id,
        'expected_equipment_version': 1,
        'human_approved': False,
    }},
    headers={{'Origin': ORIGIN, 'Idempotency-Key': 'res-refuse'}},
)
assert refuse_res.status_code == 422

# 7. Human Reservation
res_res = client.request(
    'POST',
    '/api/reservations',
    json_body={{
        'request_id': req_id,
        'equipment_id': eq_id,
        'expected_equipment_version': 1,
        'human_approved': True,
    }},
    headers={{'Origin': ORIGIN, 'Idempotency-Key': 'res-key-001'}},
)
assert res_res.status_code == 200
loan_id = res_res.json()['loan']['id']

# 8. Human Pickup
pickup_res = client.request(
    'POST',
    '/api/loans/' + loan_id + '/pickup',
    json_body={{
        'expected_equipment_version': 2,
        'human_approved': True,
    }},
    headers={{'Origin': ORIGIN, 'Idempotency-Key': 'pickup-key-001'}},
)
assert pickup_res.status_code == 200
assert pickup_res.json()['loan']['status'] == 'ON_LOAN'

# 9. Human Return
return_res = client.request(
    'POST',
    '/api/loans/' + loan_id + '/return',
    json_body={{
        'expected_equipment_version': 3,
        'human_approved': True,
    }},
    headers={{'Origin': ORIGIN, 'Idempotency-Key': 'return-key-001'}},
)
assert return_res.status_code == 200
assert return_res.json()['loan']['status'] == 'RETURNED'
assert return_res.json()['equipment']['state'] == 'AWAITING_INSPECTION'

# 10. Human Inspection
inspect_res = client.request(
    'POST',
    '/api/equipment/' + eq_id + '/inspection',
    json_body={{
        'expected_equipment_version': 4,
        'outcome': 'AVAILABLE',
        'human_approved': True,
    }},
    headers={{'Origin': ORIGIN, 'Idempotency-Key': 'inspect-key-001'}},
)
assert inspect_res.status_code == 200
assert inspect_res.json()['equipment']['state'] == 'AVAILABLE'

# 11. Authorized Tick
tick_res = client.request(
    'POST',
    '/api/internal/tasks/tick',
    headers={{'Authorization': 'Bearer test-token-123456789012345678901234'}},
)
assert tick_res.status_code == 200
tick_data = tick_res.json()
assert tick_data['outcome'] == 'success'
assert tick_data['capacity_limited'] is False
assert 'report' in tick_data

# 12. Fresh App Instance Status Read
from app import init_app
fresh_app = init_app()
fresh_client = AsgiTestClient(fresh_app)
status_res = fresh_client.request(
    'GET',
    '/api/internal/tasks/status',
    headers={{'Authorization': 'Bearer test-token-123456789012345678901234'}},
)
assert status_res.status_code == 200
status_data = status_res.json()
assert status_data['status'] == 'success'
assert status_data['last_outcome'] == 'success'
assert status_data['last_success_recent'] is True
assert status_data['counts_complete'] is True

print('SMOKE_OK')
"""
        )
        require_success(run_child(python_exe, package_dir, smoke_script, allow_db=True))
        print(
            "PASS staged PostgreSQL migrations/lifecycle/replay/conflict/human approval/tick/fresh-instance status"
        )
    finally:
        with psycopg.connect(base_pg_url, autocommit=True, connect_timeout=5) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(disposable_db_name)
                )
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--python-exe", type=Path, required=True)
    parser.add_argument(
        "--pg-url",
        required=False,
        default=None,
        help="Synthetic local admin URL; never supply cloud credentials",
    )
    args = parser.parse_args(argv)
    if args.pg_url is not None:
        validate_pg_url(args.pg_url)
    package = checked_path(args.package_dir)
    python = checked_path(args.python_exe)
    assert package.is_dir() and python.is_file()
    before = verify_manifest(package)
    test_frontend_assets(package)
    test_runtime_dependency_closure(python, package)
    test_staged_import_and_config_refusal(python, package)
    test_health_and_auth(python, package)
    if args.pg_url is not None:
        test_real_postgresql_smoke(python, package, args.pg_url)
    else:
        print("SKIP PostgreSQL smoke (--pg-url omitted); offline verification complete")
    assert verify_manifest(package) == before
    print(
        f"PASS complete manifest before/after: {before['total_files']} files; {before['total_uncompressed_bytes']} source/assets bytes"
    )
    print(
        "ALL LOCAL CHECKS PASSED; Vercel/Linux build, CDN routing, account limits and deployment UNRUN"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
