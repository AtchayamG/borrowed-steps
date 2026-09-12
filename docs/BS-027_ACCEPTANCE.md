# BS-027 Acceptance — Hosted Groq Interpreter Boundary

Date: 2026-09-12  
Status: **ACCEPTED after bounded Codex review**  
Worker: AGY (`worker/agy/BS-027`)

BS-027 connects the accepted `GroqModel` transport and PostgreSQL
`InferenceAdmissionStore` through `StrandsGroqInterpreter`. The review repaired
application workspace-ID compatibility, preserved reservation/owner identity,
moved synchronous admission calls off the async event loop, and made provider
or settlement failures fail closed without returning a draft. Hosted assistant
injection remains gated while `BS_ASSISTANT_ENABLED=0`.

Verification:

- Focused interpreter and hosted-boundary checks: 32 passed.
- Full backend suite: 595 passed, 107 skipped.
- Ruff check and format check: passed.
- Strict mypy on changed source and review tests: passed.
- `git diff --check`: passed.
- No live Groq call, deployment, cloud mutation, or personal spend.

The canonical integration branch may now accept the worker commit and proceed
to the next bounded hosted release task.
