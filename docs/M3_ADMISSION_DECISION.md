# M3 admission: provider token enforcement and application request control

Codex architecture decision at user-confirmed ASTRA_HIGH,2026-09-11.
Amends the local total-token reservation requirement in M3_HOSTED_CONTRACT.md
and step4 of M3_GROQ_BOUNDS_CONTRACT.md. This explicitly corrects the earlier
design; it does not accept BS-024 or its output-only quota claim.

## Responsibility and tradeoff

Groq enforces organization token quotas. Retain the verified Free plan, no
payment method/upgrade, no paid fallback or alternate endpoint/model.
Sources checked2026-09-11: https://console.groq.com/docs/rate-limits documents
TPM/TPD and possible split input/output limits, account-specific limits and429.
Headers are observations, not a reservation API. Billing FAQ at
https://console.groq.com/docs/billing-faqs confirms an upgrade requires a payment
method and activates usage billing. No upgrade is authorized.

PostgreSQL controls this application's requests across its instances, not other
account consumers. It does NOT label6144 output tokens a total-token reservation,
estimate input tokens from bytes, or promise20 successful interpretations/day.
Observed total tokens remain nullable. No tokenizer/template imitation, token
proxy or preflight inference call. Existing16384-byte/request,1024 output/request,
6 sends/operation,110s deadline and60s request timeout remain transport-enforced.

Provider429 aborts without retry, draft or business mutation. Pause new inference
for15 minutes after confirmed429. Manual structured intake remains usable.
Throttling can occur on the first operation if another consumer used tokens.
This removes an unsupported self-imposed local guarantee; it preserves ₹0 spend,
grounding, human approval and cleanup. Availability is explicitly best effort.

## BS-026 storage boundary only

Implement infrastructure/inference_admission.py with explicit PostgreSQL URL.
No HTTP wiring/provider/authentication framework. Follow canary_receipt.py's
transaction/validation conventions, but preserve that historical module/receipts.
Do not reuse rejected BS-024 code. Add V4 migration; keep V1–V3 SQL unchanged.

Fixed application policy (not provider total-token capacity):
- One active operation across every workspace in this database.
- Reserve6 sends per operation; never refund unused sends.
- Global rolling60-second reservation cap6; rolling24-hour cap120.
- Per-workspace rolling24-hour cap24 (at most4 admitted operations).
-120-second stored deadline identifies stale operations, never auto-releases.
- Confirmed provider429 creates a durable15-minute global cooldown.
Use fixed versioned constants, no caller-configurable limit or organization ID.
Every instance must share this database. Other projects need reviewed allocation;
this ledger does not control unrelated keys or deployments using another DB.

Use one transaction-scoped advisory lock for admission/finish/recovery and a
partial unique index allowing one active row. Workspace FK required. No DB
transaction across inference. Follow accepted bounded connect/statement/lock
timeouts and prepare_threshold=None. Retain existing migration lock.

Input identity: canonical UUID4 workspace, owner execution, reservation IDs;
lowercase64-hex request_key_hash/payload_hash. Future authenticated HTTP code
computes hashes server-side from bounded canonical inputs. No raw intake,
headers, secrets or arbitrary reason text stored. Uniqueness(workspace,key_hash).
Any changed identity/hash on reserve replay conflicts; exact replay only reads.
Reservation IDs cannot be reused under another workspace/key.

Methods: reserve, mark_dispatched, finish, recover_dead, get. Avoid multiple
confirm_success/failure methods. reserve creates RESERVED+active. Exactly one
RESERVED->DISPATCHED transition permits inference. Repeat dispatch, wrong owner,
expired deadline or lost DB confirmation grants no permission. Denied reserve
leaves no new row. Caller must never infer dispatch permission from reserve/get.

Owner finishes DISPATCHED as SUCCEEDED, FAILED_CONFIRMED or UNCERTAIN only.
Success requires actual_sends integer1..6; other states0..6 orNULL if unknown.
actual_total_tokens nonnegative bigint orNULL; never a quota credit. Fixed
failure enum, success none. Reject booleans/overflow/bad types with generic errors.
Exact finish replay compares all evidence; changed replay or transition from
UNCERTAIN to success/failure refuses. Confirmed terminal states require explicit
cleanup_completed=True; otherwise refuse and retain active. UNCERTAIN stays active.

recover_dead requires confirmed_dead=True, canonical operator UUID4, fixed reason
enum. RESERVED/DISPATCHED/UNCERTAIN -> RECOVERED, clear active and record current
DB released_at. Exact recovery replay compares operator/reason; other replay or
recovery of confirmed terminal states refuses. No public recovery route. This
storage function cannot authenticate operators or prove death by itself.

Active rows count their full6 reservations indefinitely. Released rows count
until released_at+window expires. released_at is set only after confirmed cleanup
or explicit dead recovery, never from an earlier timeout/deadline timestamp.
Windows/deadlines use DB time. Preserve429 cooldown across restart. Never prune
audit rows automatically in this task. Fixed daily cap keeps their scale small.
Later HTTP integration must cancel/close before storage settlement, so a DB
failure cannot skip cleanup. Finalization failure leaves the reservation active.

## Required verification

Real disposable PostgreSQL: concurrent workspaces yield one reservation/dispatch;
identity/replay conflicts; restart preserves active/uncertain state; expired
deadline never releases; finish cannot release UNCERTAIN; recovery of old
uncertainty retains fresh full-window debit; minute/day/workspace limits;
full debit despite actual_sends1;429 cooldown; invalid inputs; exact/conflicting
finish/recovery; additive migration preserves existing business and receipt rows.
Test lost confirmation/faults without provider calls. Update schema expectations
to4, including packaged smoke, and rerun existing PostgreSQL adapter/receipt tests.

## Following integration and release

After storage acceptance, wire accepted Groq/Strands stages, grounding, session/
Origin checks, defaults, busy/quota UI and cleanup ordering in a bounded task.
Prove no process slot leak on DB errors/cancellation. Do not restart extraction
design. Public assistant remains disabled until integration and release gates.
Codex later verifies current Free-plan status, hosted resource headroom, real
hosted interpretation, persistence/scheduler/browser behavior. Final higher-
effort security/release/video/submission audits remain required. No live grant.
