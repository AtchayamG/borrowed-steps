# BS-028 independent integration acceptance

Date: 2026-09-12
Status: COMPLETED — accepted locally; public release remains pending.
Worker implementation: 0be967b, base db67213.
Reviewer configured mode: ASTRA_HIGH (user confirmed).
Serving identity is not independently exposed.

## Review outcome

The bounded diff enables the existing hosted factory, reports strands_groq,
and validates the documented provider/model tuples. The hosted mode remains
disabled by default. Existing admission, session, origin, human approval and
cleanup boundaries are retained. No live provider calls or cloud mutations were made.

Codex strengthened the existing HTTP success test to use create_app's real factory
and intercept only AsyncHTTPTransport. Codex also replaced the hardcoded September
20 sample with the existing seven-days-ahead helper and added an October 8 regression
check. Worker evidence formatting was normalized. No further worker correction task
is needed.

## Independently reproduced verification

Working directory for backend: services/agent in this worktree.
Python: sibling BS-022-agy/services/agent/.venv/Scripts/python.exe (Python 3.12.10).
PYTHONPATH=src;tests. BS_POSTGRES_TEST_URL was an owned loopback PostgreSQL
cluster on port 55441 with role bs021. PGCONNECT_TIMEOUT=5.

- python -m pytest -q tests -o faulthandler_timeout=45:
  **718 passed, zero skipped, two dependency deprecation warnings**, 244.10 seconds.
- python -m ruff check services/agent/src services/agent/tests/test_hosted_release_candidate.py: pass.
- python -m ruff format --check services/agent/src services/agent/tests/test_hosted_release_candidate.py: pass.
- python -m mypy --config-file services/agent/pyproject.toml services/agent/src services/agent/tests/test_hosted_release_candidate.py:
  pass, 36 source files.
- From apps/web: npm test -- --run: **124 passed in 13 files**.
- npm run typecheck; npm run lint; npm run build; npm run format:check: pass.
- Runtime assembly --verify-repeat --skip-build reused the freshly verified frontend
  build: final output_bs028_review_final, 42 files, 596259 source/assets bytes.
- Isolated Python 3.12 package verification passed on the initial reviewed package,
  including real disposable PostgreSQL migrations, human lifecycle, exact replay,
  conflicts, approval refusal, tick and fresh-instance scheduler status.
- Isolated Groq adapter verification passed, including four negative checks.
- The final package rerun also passed all isolated checks and disposable PostgreSQL
  smoke checks; manifest unchanged at 42 files / 596259 source/assets bytes.
- git diff --check against db67213: pass after evidence whitespace cleanup.

Two initial full-suite attempts were interrupted for reviewer harness configuration
errors (wrong cluster port, then a forbidden connection-timeout URI parameter).
They are not successful test evidence. The successful complete run above used the
correct port and libpq timeout environment setting. A test-only mypy invocation
without local source roots also failed; the explicit-source command above passed.

## Remaining release work

This acceptance proves local integration, not public platform operation. Vercel Linux
build/CDN routing, free-account headroom, Neon production provisioning/TLS/migrations,
deployed provider proof, actual scheduled runs, public browser workflow/persistence,
and final product/security review are still pending. No live URL is claimed.

Next order, per user: finish Borrowed Steps live release, create and accept its
LumaLoad-process video, submit and verify Devpost; only then start Benchbook, then
Schoolbag. Final complete-product/repository/deployment/video/submission reviews
require ASTRA_MAX under the user's manual mode rules.
