# Local hosted package

Latest independent review: see docs/BS-025_ACCEPTANCE.md. The adapter verifier
now enforces shared network/import guards and exact runtime closure. Its schema
is a synthetic FixtureExtraction, not the production extraction schema.
The PostgreSQL smoke expects accepted schema3. Omitting --pg-url explicitly
skips database verification; Codex's acceptance ran it with a disposable database.
Run scripts/verify_hosted_groq.py with explicit --package-dir and --python-exe.
Portable pytest packaging regressions do not require an ignored staging folder.
Historical worker measurements below are superseded where this acceptance differs.

This packages the current disabled-assistant app and Groq transport adapter closure.
It is not a Vercel deployment or platform build. Use Python 3.12 and a Node version
compatible with the locked Vite version (independently tested with Node 22.22.3).

## Setup & Verification Commands

From this repository, choose a NEW runtime/output name if these already exist:

```powershell
# 1. Create fresh isolated runtime virtual environment
rtk proxy uv venv --python 3.12 deploy/vercel/.venv-runtime

# 2. Install exact 60-package pinned runtime closure (excluding dev/test tools)
rtk proxy uv pip install --python deploy/vercel/.venv-runtime/Scripts/python.exe -r deploy/vercel/requirements.txt

# 3. Assemble staged package with repeat verification
rtk proxy ./deploy/vercel/.venv-runtime/Scripts/python.exe scripts/assemble_hosted.py --output-dir deploy/vercel/output_bs025 --verify-repeat

# 4. Verify base hosted package (offline smoke: health, auth, config refusals)
rtk proxy ./deploy/vercel/.venv-runtime/Scripts/python.exe scripts/verify_hosted_package.py --package-dir deploy/vercel/output_bs025 --python-exe deploy/vercel/.venv-runtime/Scripts/python.exe

# 5. Verify staged Groq adapter under isolated runtime (synthetic HTTP intercept, bounds, refusals, 4 negative checks)
rtk proxy ./deploy/vercel/.venv-runtime/Scripts/python.exe scripts/verify_hosted_groq.py --package-dir deploy/vercel/output_bs025 --python-exe deploy/vercel/.venv-runtime/Scripts/python.exe

# Optional: PostgreSQL smoke if local disposable cluster is available
# rtk proxy ./deploy/vercel/.venv-runtime/Scripts/python.exe scripts/verify_hosted_package.py --package-dir deploy/vercel/output_bs025 --python-exe deploy/vercel/.venv-runtime/Scripts/python.exe --pg-url postgresql://SYNTHETIC_USER@127.0.0.1:55434/postgres
```

The optional PostgreSQL smoke requires an already-running, owned local PostgreSQL test server
and a synthetic role permitted to create disposable databases. Replace only the
explicit placeholders; never supply a cloud/database credential. The command
creates and drops its random `bs017_smoke_*` database, not the supplied admin DB.
Use port 55434 or 55439 only when the documented Codex-owned cluster is running there.
Server lifecycle is separate; stop only a server you own. Commands are Windows
examples; Linux runtime installation and Vercel execution remain unverified.

## Assembly & Staging Details

Assembly always runs `npm ci` and `npm run build` unless explicitly passed `--skip-build`, which
only reuses existing assets and cannot prove a fresh frontend build. It copies
accepted backend Python files, four template files and built static assets.
Outputs must be new direct `output*`/`staging*` children of `deploy/vercel`. Existing
directories are refused and nothing is recursively deleted. Repeated assembly
uses a new random sibling retained for inspection. Links/reparse points including
ancestors, unexpected source/static files, `.env` and databases are refused.

The root `app.py` requires explicit hosted configuration before factory creation.
Use `BS_RUNTIME=hosted`, `BS_STORE=postgres`, `BS_TASKS_ENABLED=0`,
`BS_ASSISTANT_ENABLED=0`, secure cookies, explicit allowed HTTPS origin and DB URL.
The accepted config/hosted contracts define the remaining requirements. No
secrets/default production values are provided. Optional `BS_LOG_LEVEL` accepts
`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` without echoing invalid environment values.

The template follows native FastAPI entrypoint discovery and `public/` assets.
No rewrite to `/app.py` and no FastAPI static mount are added. Local ASGI tests
verify API semantics; they do not prove CDN/root/deep-link routing on Vercel.
Official sources checked 2026-09-10:
- https://vercel.com/docs/frameworks/backend/fastapi
- https://vercel.com/docs/functions/runtimes/python

## Verifier Execution

The verifiers (`verify_hosted_package.py` and `verify_hosted_groq.py`) use fresh
external working directories, clean environment and Python `-I -B`; check loaded
module paths, exact installed distribution pins, dependency metadata, refusals,
health/JSON404, lifespan and unauthorized routes.

### Groq Adapter Verifier (`scripts/verify_hosted_groq.py`)

Exercises the staged `GroqModel` adapter under the isolated runtime:
- Verifies package directory exists and manifest matches before and after test execution (zero package mutations).
- Enforces offline/isolated execution with clean environment, `python -I -B`, no repo root in `sys.path`, outbound sockets blocked fail-closed, and synthetic dummy API keys.
- Intercepts requests using `httpx.MockTransport`, confirming zero outbound network sockets.
- Proves streaming translation (chunking, delta reconstitution, `end_turn` stop reason, token usage extraction, and `length` truncation).
- Proves structured extraction using the `_Extraction` schema, validating wire format and typed Pydantic reconstruction.
- Asserts strict envelope bounds: request JSON <= 16 KB, `max_completion_tokens == 1024`, model identifier pinned to `openai/gpt-oss-20b` (`GROQ_MODEL_ID`), `reasoning_effort == 'low'`, deprecated `max_tokens` forbidden.
- Asserts resource hygiene: HTTP client and response streams cleanly closed, zero active clients, zero leaked connections.
- Asserts refusal semantics: empty/missing API keys fail closed, limits exceeding ceilings fail closed, parameter mutation attempts fail closed.
- Exercises 4 required negative checks:
  1. Outbound socket connection blocked fail-closed (`RuntimeError`).
  2. Unpinned target URL and unpinned model refused before network dispatch (`GroqTargetRefusedError`, `GroqEnvelopeRefusedError`).
  3. Missing dependency fails closed (`ModuleNotFoundError`).
  4. Dev checkout import forbidden (proves staged module isolation).

## Runtime Dependency Closure (BS-025)

The runtime closure in `deploy/vercel/requirements.txt` contains exactly **60 pinned packages**,
derived from the accepted `requirements.lock`, `requirements-groq.lock`, and `requirements-postgres.lock`.

### Excluded Dev / Test / Tool Packages

All 11 developer and test tools have been strictly excluded from the runtime bundle:

| Excluded Package | Rationale |
|------------------|-----------|
| `ast_serialize` | Developer AST serialization utility |
| `iniconfig` | Pytest configuration parser |
| `librt` | Real-time Linux runtime test dependency |
| `mypy` | Static type checker |
| `mypy_extensions` | Mypy type system extensions |
| `ollama` | Local Ollama client (hosted mode uses OpenAI-compatible GroqModel) |
| `pathspec` | Pattern matching for linters |
| `pluggy` | Pytest plugin architecture |
| `Pygments` | Syntax highlighter for terminal output |
| `pytest` | Test runner framework |
| `ruff` | Linter / code formatter |

### Pinned Runtime Packages (60)

| Package | Version | Environment / Notes |
|---------|---------|---------------------|
| `annotated-doc` | 0.0.5 | Pydantic / typing support |
| `annotated-types` | 0.8.0 | Type annotation metadata |
| `anyio` | 4.15.1 | Asynchronous I/O core |
| `attrs` | 26.1.0 | Data attribute classes |
| `aws-bedrock-token-generator` | 1.1.0 | Required transitive dependency of strands-agents[openai] |
| `boto3` | 1.43.89 | AWS SDK core dependency |
| `botocore` | 1.43.89 | Low-level AWS core |
| `certifi` | 2026.7.22 | Root CA bundle |
| `cffi` | 2.1.1 | C foreign function interface (cryptography) |
| `click` | 8.5.0 | CLI parsing core |
| `colorama` | 0.4.6 | Terminal output formatting |
| `cryptography` | 50.0.1 | Cryptographic primitives (pyjwt[crypto] / mcp) |
| `distro` | 1.9.0 | OS platform identification |
| `docstring-parser` | 0.18.0 | Structured docstring extraction |
| `fastapi` | 0.141.1 | Web framework entrypoint |
| `h11` | 0.16.0 | Pure-Python HTTP/1.1 protocol |
| `httpcore` | 1.0.9 | HTTP transport engine |
| `httpx` | 0.28.1 | Synchronous and asynchronous HTTP client |
| `httpx-sse` | 0.4.3 | Server-Sent Events parser for HTTPX |
| `idna` | 3.19 | Internationalized domain names |
| `jiter` | 0.16.0 | Fast JSON parser (OpenAI SDK) |
| `jmespath` | 1.1.0 | JSON query language |
| `jsonschema` | 4.26.0 | JSON schema validation |
| `jsonschema-specifications` | 2025.9.1 | JSON schema spec definitions |
| `mcp` | 1.30.0 | Model Context Protocol core |
| `openai` | 2.54.0 | OpenAI Python SDK (Groq transport target) |
| `opentelemetry-api` | 1.44.0 | OpenTelemetry observability API |
| `opentelemetry-instrumentation` | 0.65b0 | OpenTelemetry instrumentation |
| `opentelemetry-instrumentation-threading` | 0.65b0 | Threading instrumentation |
| `opentelemetry-sdk` | 1.44.0 | OpenTelemetry SDK |
| `opentelemetry-semantic-conventions` | 0.65b0 | Semantic conventions |
| `packaging` | 26.3 | Core distribution utilities |
| `psycopg` | 3.2.10 | PostgreSQL database driver |
| `psycopg-binary` | 3.2.10 | C-optimized PostgreSQL client |
| `pycparser` | 3.0 | C parser in Python (cffi) |
| `pydantic` | 2.13.5 | Data validation and parsing |
| `pydantic-core` | 2.46.5 | Core Pydantic Rust validation engine |
| `pydantic-settings` | 2.15.0 | Settings management via Pydantic |
| `pyjwt` | 2.13.0 | JSON Web Tokens implementation |
| `python-dateutil` | 2.9.0.post0 | Extensions to standard datetime |
| `python-dotenv` | 1.2.3 | Environment variable loading |
| `python-multipart` | 0.0.32 | Streaming multipart form parser |
| `pywin32` | 312 | `sys_platform == 'win32'` (mcp dependency on Windows) |
| `pyyaml` | 6.0.3 | YAML parser and emitter |
| `referencing` | 0.37.0 | JSON Schema reference resolution |
| `rpds-py` | 2026.6.3 | Persistent data structures in Rust |
| `s3transfer` | 0.19.2 | S3 transfer manager |
| `six` | 1.17.0 | Python 2/3 compatibility utility |
| `sniffio` | 1.3.1 | Async library sniffer |
| `sse-starlette` | 3.4.11 | Server-Sent Events for Starlette |
| `starlette` | 1.6.0 | ASGI framework foundation |
| `strands-agents` | 1.54.0 | Agent execution and tool loops |
| `tqdm` | 4.70.0 | Progress bar utility |
| `typing-extensions` | 4.16.0 | Standard typing backports |
| `typing-inspection` | 0.4.4 | Runtime type introspection |
| `tzdata` | 2026.3 | `sys_platform == 'win32'` (IANA timezone database) |
| `urllib3` | 2.7.0 | HTTP client library |
| `uvicorn` | 0.52.4 | ASGI web server implementation |
| `watchdog` | 6.0.0 | File system event monitoring |
| `wrapt` | 2.4.0 | Decorators and monkey patching |

## Package Measurements

- **Staged Source and Assets**: 39 files, 530,459 bytes
  - Backend source (`borrowed_steps/**/*.py` and `app.py`): 35 files, 269,451 bytes (including `groq_model.py` at 32,300 bytes)
  - Configuration (`requirements.txt`, `vercel.json`): 2 files, 4,375 bytes
  - Static frontend assets (`public/index.html`, `public/assets/*`): 2 files, 256,633 bytes
- **Staged Installed Distribution Files**: 60 packages, 85,641,953 bytes (Windows measurement, excluding virtualenv overhead)
- **Manifest Repeat**: Byte-for-byte identical across independent assemblies.
