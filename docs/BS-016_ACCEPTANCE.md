# BS-016 local HTTP acceptance

## Codex BS-016 acceptance2026-09-10
ACCEPTED_LOCAL after direct corrections to workeredd3a05.541 tests passed,
zero failed/skipped; full suite 115.354s. Ruff lint/format, strict
mypy76 files and pip check72 packages pass. Workflow YAML/Bash syntax and16 offline
shell cases pass. No live workflow, provider call, cloud activation or spend.

New regressions failed on the worker source for both operational exception log
paths and the intake schema bypass, then passed after correction. Intake now
uses the request-time schema gate and performs session lookup off the event loop.
Operational errors log generic messages without exception details. Configuration
validation is centralized in Settings; scheduler typing no longer uses Any.
Real PostgreSQL HTTP tests now cover due notices, duplicate suppression, contention
partial results, event rollback/unknown counts and durable evidence-write failure.
The status no-task test now patches the actual imported call site. Workflow curl
disables default config and URL globbing, restricts protocols, suppresses raw error
details, validates token syntax and classifies only an exact three-digit2xx as success.

Original worker report below is historical and superseded by this acceptance.
No release/live scheduler/inference admission/video/submission acceptance implied.
NEXT_CODEX_MODE: ASTRA_LIGHT
REASON: Review complete; next step is routine manual AGY dispatch.
