# BS-017: local hosted-package preparation

Codex decision at ASTRA_HIGH,2026-09-10. This task prepares a reviewable package
for the existing Vercel/PostgreSQL direction. It authorizes no deployment, account
linking, provider inference or workflow activation. Assistant stays disabled.

Use an isolated `deploy/vercel/` template and a small standard-library assembly
script. Produce an ignored, project-owned staging directory with a supported
FastAPI `app` entrypoint, accepted borrowed_steps source, pinned runtime-only
dependencies, Python3.12 selection, and built React files in `public/`. Do not
copy the whole repository. No tests, evidence, worker reports, environments,
credentials, runtime databases, caches, virtualenvs, node_modules or symlinks.
Never modify original backend/local manifests to make a deployment snapshot.

The entrypoint must require explicit hosted configuration and refuse missing or
local settings before create_app can open SQLite. It uses the accepted factory;
no alternate business logic. Export the real FastAPI instance. No database I/O
on import, no TaskRunner or provider import. Preserve same-origin relative /api
calls and API404 JSON; no SPA catch-all may mask invalid API paths.

Use installed metadata and existing accepted pins to derive a clean Python3.12
runtime closure for the disabled-assistant package, excluding test tools and
Windows-only pywin32. Prove the package imports and serves hosted health and
unauthorized operations in a fresh environment containing only that closure.
Do not claim a future Strands-enabled package is validated by this disabled one.
The existing full developer locks and all local behavior remain untouched.

Build the current frontend, assemble twice, compare relative file hashes, and
record source hash, dependency versions and uncompressed staging size separately
from installed dependencies. This is not a measured Linux Vercel function bundle,
cold start or availability claim. Local tests must not silently use the repository
editable install or unrelated PYTHONPATH. A disposable PostgreSQL HTTP smoke may
verify the staged package; no live cloud database can substitute for it.

Only official docs/package registries and own local tooling may be contacted.
Do not run login/link/pull/deploy commands or activate GitHub workflows. If a
Vercel CLI build requires an account/link, leave that verification explicitly
unrun; do not call a homemade package a Vercel build. Unknown routing/platform
details remain a documented release gate. Actual free-account eligibility,
resource headroom, shared inference admission and live tests remain unresolved.

Primary sources checked2026-09-10:
- [FastAPI deployment](https://vercel.com/docs/frameworks/backend/fastapi):
  supported app entrypoints and CDN public directory; do not mount public in FastAPI.
- [Python runtime](https://vercel.com/docs/functions/runtimes/python):
  Python3.12 supported; no automatic Python tree-shaking, runtime dependencies only.
- [Project configuration](https://vercel.com/docs/project-configuration):
  validate any optional configuration against the current official schema.

Prefer supported defaults and omit unnecessary configuration. No new multi-service
architecture, custom build-output framework or public deployment in this task.
