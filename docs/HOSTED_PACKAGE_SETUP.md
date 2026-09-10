# Local hosted package

This packages the current disabled-assistant app. It is not a Vercel deployment
or platform build. Use Python3.12 and a Node version compatible with the locked
Vite version (independently tested with Node22.22.3).

From this repository, choose a NEW runtime/output name if these already exist:

```powershell
rtk proxy uv venv --python 3.12 deploy/vercel/.venv-runtime
rtk proxy uv pip install --python deploy/vercel/.venv-runtime/Scripts/python.exe -r deploy/vercel/requirements.txt
rtk proxy ./deploy/vercel/.venv-runtime/Scripts/python.exe scripts/assemble_hosted.py --output-dir deploy/vercel/output_new --verify-repeat
rtk proxy ./deploy/vercel/.venv-runtime/Scripts/python.exe scripts/verify_hosted_package.py --package-dir deploy/vercel/output_new --python-exe deploy/vercel/.venv-runtime/Scripts/python.exe --pg-url postgresql://SYNTHETIC_USER@127.0.0.1:OWNED_PORT/postgres
```

The last command requires an already-running, owned local PostgreSQL test server
and a synthetic role permitted to create disposable databases. Replace only the
explicit placeholders; never supply a cloud/database credential. The command
creates and drops its random `bs017_smoke_*` database, not the supplied admin DB.
Use port55439 only when the documented Codex-owned cluster is running there.
Server lifecycle is separate; stop only a server you own. Commands are Windows
examples; Linux runtime installation and Vercel execution remain unverified.

Assembly always runs npm ci/build unless explicitly passed --skip-build, which
only reuses existing assets and cannot prove a fresh frontend build. It copies
31 accepted backend Python files, four template files and built static assets.
Outputs must be new direct output*/staging* children of deploy/vercel. Existing
directories are refused and nothing is recursively deleted. Repeated assembly
uses a new random sibling retained for inspection. Links/reparse points including
ancestors, unexpected source/static files, .env and databases are refused.

The root app.py requires explicit hosted configuration before factory creation.
Use BS_RUNTIME=hosted, BS_STORE=postgres, BS_TASKS_ENABLED=0,
BS_ASSISTANT_ENABLED=0, secure cookies, explicit allowed HTTPS origin and DB URL.
The accepted config/hosted contracts define the remaining requirements. No
secrets/default production values are provided. Optional BS_LOG_LEVEL accepts
DEBUG/INFO/WARNING/ERROR/CRITICAL without echoing invalid environment values.

The template follows native FastAPI entrypoint discovery and public/ assets.
No rewrite to /app.py and no FastAPI static mount are added. Local ASGI tests
verify API semantics; they do not prove CDN/root/deep-link routing on Vercel.
Official sources checked2026-09-10:
- https://vercel.com/docs/frameworks/backend/fastapi
- https://vercel.com/docs/functions/runtimes/python

The verifier uses fresh external working directories, clean environment and
Python -I/-B; checks loaded module paths, exact installed distribution pins,
dependency metadata, refusals, health/JSON404, lifespan and unauthorized routes.
SQLite is forbidden; PostgreSQL is forbidden except during the disposable smoke.
The smoke uses the actual staged factory/store/migrations and real PostgreSQL,
with an in-process ASGI client. This is not a network-server/browser/platform test.
Manifest verification recomputes every file/hash/size/category/total, detecting
same-size tampering, additions and path-traversal entries without trusting paths.

See docs/BS-017_ACCEPTANCE.md and services/agent/test-evidence/bs017/codex-* for
independent results. Source/assets:38 files,507219 bytes. Runtime distributions:
16 Windows packages,19173174 bytes; includes package metadata/recorded files,
not the entire virtualenv, Python interpreter or Linux function bundle.
Two assembly manifests match;31 backend sources match accepted05aacf4 bytewise.

Five dev-tool advisories remain queued (puppeteer/vitest dependency graphs).
No automated force-upgrade was applied to the accepted frontend lock.
Account eligibility, free quota/headroom, Linux build, real CDN/API routing,
cloud database, scheduler activation and final live release remain open gates.
Assistant packaging/inference admission needs separate proof. Zero inference
and cloud activation are authorized by this local packaging task.
