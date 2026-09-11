# BS-023 live operator canary passed

Executed 2026-09-11T16:39:59Z against Groq Free, model openai/gpt-oss-20b,
through the accepted Strands tool loop and structured extraction. Source
d3d84e1; execution manifest
47c742026de0c53b4ac2e2800b469441095c3f2fa4fc14532a98ff8de0eed42b.

One operator operation made three physical sends, all HTTP200. Serialized
request lengths were1057,1514,3753 bytes. All eight source-field assertions
passed, real inventory tool execution was required, and owned cleanup completed.
Durable receipt state SUCCEEDED, actual_sends3, actual_total_tokensNULL,
concurrency_active=false. A separate database restart/read verified persistence.
The operator grant is consumed; no remaining sends or replay authorized.

Free $0 account and30RPM/1000RPD/8000TPM/200000TPD limits were checked in the
signed-in Personal/Default account immediately before execution. The monthly
usage view reported no data with up to15-minute delay, not real-time headroom.
No paid upgrade, production business mutation or public deployment occurred.

An initial local launcher timed out after PostgreSQL startup, before receipt
creation or provider construction. The startup wrapper was corrected; zero
receipt rows were verified before the same grant's first invocation. This was
not a provider retry. The audit database was retained and its server stopped.

This is one successful synthetic operator fixture, not a reliability study,
total-token upper bound, safe global quota policy, hosted endpoint acceptance,
production deployment, or AgentCore claim. Public assistant remains disabled.
BS-024's rejected output-only admission is not integrated. Admission, hosted
interpreter integration, public hosting/persistence/scheduler/browser proof,
final audits, video and submission remain pending.

Sanitized detailed evidence and grant are retained under program-control
reviews/bs023, outside the public product source. Credentials are excluded.
