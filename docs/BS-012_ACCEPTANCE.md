# BS-012 offline foundation acceptance

AGY return a6d90a0 from e5138cb was reviewed and directly corrected by Codex at ASTRA_MEDIUM. The original worker report and evidence describe the pre-correction run; this report supersedes their acceptance claims. No worker correction loop.

Fixed an actual deadline defect: response iteration waited indefinitely for a chunk before checking time. The new timeout wraps each pending async read and covers the whole request body. Timer classification uses the selected deadline, avoiding early Windows timer wakeups being mislabeled. Added indefinitely stalled stream/schema and cancellation regressions with independent watchdogs.

Other corrections: finite timeout limits; strict integer send limit; pinned port/query/method and on-wire model validation; explicit upstream generator closing; bounded client/transport cleanup with retained retry state after close failure; no silent cleanup exception suppression; unconditional tool removal from schema requests; sanitized provider/refusal/parse errors; requirements overlay includes the baseline lock; documentation whitespace corrected.

Independent verification on corrected source:
- Full pytest: 413 passed, two existing deprecation warnings, 27.23 seconds.
- Focused adapter checks: 31 cases covered by that run (18 original plus 13 reviewer cases).
- Ruff lint and format check: PASS.
- Strict mypy over src/tests/scripts: PASS, 64 source files.
- uv pip check: 69 packages compatible.
- Real Strands retry strategy receives synthetic 429s, makes exactly two intercepted sends under a reduced budget, and cannot send again through streaming or schema paths.
- Original real Strands tool-loop and OpenAI strict-schema serialization tests pass. SDK serialization produces stream:false for schema calls; no streaming options or tools. This is offline wire proof, not provider acceptance.

One intermediate full run exposed timer-classification behavior; it was corrected and the complete suite rerun. The first very tight deadline regression was adjusted to allow SDK startup before testing the stalled body. No failed run is counted as accepted.

Accepted scope: standalone Groq transport foundation only. No production interpreter/HTTP/settings wiring, cloud activation, public inference, global admission or deployment. Callers must own and close each model and generator and impose the existing outer orchestration deadline; the adapter's network deadline does not time arbitrary caller/tool work. Real Groq compatibility, model quality, token reservation, shared admission and final security/release audits remain pending. All29 historical live allocations stay closed; zero new model calls or spend.

Claude BS-011 completion has not been reported. Next safe action: inspect its PostgreSQL return when available before integrating the hosted application. NEXT_CODEX_MODE: ASTRA_LIGHT — routine coordination after this review.
