# BS-028 independent integration acceptance

Date: 2026-09-12
Status: COMPLETED — local and hosted release candidate verified.
Worker implementation: 0be967b, base db67213.
Reviewer configured mode: ASTRA_MAX (user confirmed).
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

The hosted release verification below adds public platform operation, Neon
provisioning/TLS/migrations, deployed provider proof, and anonymous persistence.
Scheduled runs and final product/security review remain release gates.

Next order, per user: finish Borrowed Steps live release, create and accept its
LumaLoad-process video, submit and verify Devpost; only then start Benchbook, then
Schoolbag. Final complete-product/repository/deployment/video/submission reviews
require ASTRA_MAX under the user's manual mode rules.

## Hosted release verification (2026-09-12)

Public URL: https://borrowed-steps.vercel.app (Vercel Hobby, project `borrowed-steps`).
The deployment uses Neon Free PostgreSQL and Groq Free credentials stored only as
Vercel production secrets. The database URL keeps `sslmode=verify-full`, the bundled
certifi CA file, and an explicit public IPv4 route for Vercel's serverless runtime.

- `GET /api/health`: HTTP 200, `status=ok`, `milestone=M3`, `agent_mode=strands_groq`.
- `POST /api/workspaces` with an empty JSON body: HTTP 201; returned a secure
  `bs_session` cookie and a seeded snapshot containing three equipment rows.
- The temporary release database probe was removed before this deployment; the same
  path now returns HTTP 404.
- Live Neon SQL verification reports schema version 4 and four applied migrations.
- Deployment `dpl_Ch99rUGy7WXyeUZmWJaethRteqeX` completed READY on Vercel.

The direct runtime regression after the hosted fix is **63 passed, 19 skipped** for
the PostgreSQL store, hosted configuration, and hosted runtime tests; ruff and
explicit-source mypy both pass. The source fix is committed as `14069ea`.

- Real hosted assistant check: `POST /api/intake/interpret` returned HTTP 200 with
  provenance `framework=strands`, `provider=groq`, `model=openai/gpt-oss-20b`, and
  the grounded wheelchair draft for the synthetic Priya S scenario. A first
  concurrent attempt returned the intentional `429 ASSISTANT_BUSY`; after the
  bounded admission window cleared, the retry succeeded.
