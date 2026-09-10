# BS-014 local foundation acceptance

## Latest checkpoint2026-09-10: BS-014 accepted; BS-015 evidence only

User-confirmed ASTRA_HIGH review complete. BS-014 worker4e4d712 corrected directly
by Codex to7226f41. BS-015 AGY16e2885 corrected toed5ece3. Both integrated locally;
original worker histories preserved. Claude remains unavailable until Monday;
use AGY manually until user reports availability. No automatic workers.

Canonical combined backend verification:522 passed, zero failed/skipped, two
existing dependency deprecation warnings,91.73s. Ruff lint/format and strict mypy
pass75 files; pip check passes72 packages. PYTHONPATH selected canonical source
using the BS-014 owned venv. Synthetic disposable PostgreSQL16.10, loopback only.
Earlier515 full and22 focused scheduler tests passed; corrected probe25 focused
tests passed. Final combined suite includes fresh-process scheduler persistence.

BS-014: fixed per-statement timing arithmetic, startup SQL timeouts, safe connection
errors, finite budget validation, injected-store limits, unknown failed-run counts,
status privacy and future-time freshness. V1 unchanged; real V1->V2 preservation,
concurrency, stale finalizer, human race, event rollback and slow SQL tests pass.
42s conditional SQL-path envelope; no production/network/OS guarantee claimed.

BS-015: accepted sampled wire evidence ONLY. Bytes are not measured tokens;
synthetic usage values and short output fixtures do not prove quota bounds.
Unsupported throughput,7000-token reservation and zero429 guarantees withdrawn.
No admission runtime/migration/activation authorized. Hosted assistant stays off.

No provider calls, cloud activation, spend, public push or deployment. All29
historical inference allocations stay closed. Final live/security/video/Devpost
audits and hosted operations/admission remain pending; product is not submitted.

Next: BS-016 AGY manual scheduler HTTP and inactive workflow packaging, from the
new frozen operational addendum. No Claude task. Review initial return at LIGHT;
raise to HIGH only for substantive integration/security decisions.
NEXT_CODEX_MODE: ASTRA_LIGHT
REASON: Architecture decision is saved; next action is routine manual dispatch.


See HOSTED_SCHEDULER_SETUP.md for the SQL statement accounting. Original worker
acceptance.json is historical; codex-review.json and this review supersede it.
