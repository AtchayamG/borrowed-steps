# BS-021 corrected receipt contract

Codex HIGH takeover, 2026-09-11. Supersedes the original BS-021 receipt
contract and worker report. No live canary is authorized by this document.

## Scope and accounting correction

This is an operator-only, one-use execution receipt, not public quota admission.
The prior contract asked for a conservative total-token reservation while the
accepted BS-020 plan explicitly leaves input and reasoning accounting unknown.
It cannot truthfully promise that bound. This correction removes that claim.

Retain exactly six request reservations and 6,144 requested output tokens
(six per-request caps of 1,024). The latter is NOT a total-token quota bound.
Observed request counts and observed TOTAL provider tokens are separate nullable
fields: absent means unknown. Never substitute reservations for observations,
default uncertain usage to zero, refund these retained reservations, or derive
account/window capacity from this table. Public quota admission remains blocked
until separately designed from actual provider semantics and account limits.

## Trusted caller and fixed candidate

The storage boundary has no public route, provider client, credential discovery
or authority to create grants. Its caller is the trusted operator runner.
That runner must authenticate the operator, obtain a dated explicit one-use
authorization, and verify the actual code/locks/fixture against the approved
candidate immediately before dispatch. A claimed hash is not proof that running
code matches it. Before a live run, fingerprint the runner and helper sources
as well as the BS-020 source/lock inputs. This task supplies no live runner.

The accepted candidate remains the unchanged BS-020 offline plan hash
38ec48176db21d7f947cfdc1ae1b3b211efdbdb55c976043462aea85a16a3031.
Its live_authorized=false remains unchanged. Live authorization, if issued later,
is a separate explicit grant; never mutate the offline plan under its old hash.
Receipt input has no configurable per-send ceilings. Unknown/wrong candidate,
provider/model, non-exact reservations, missing or non-boolean authorization
and invalid UUID4 identities fail closed. No caller-input value appears in errors.

Receipt, owner execution and authorization IDs are canonical UUID4 identifiers;
caller generates and retains them before first use. Owner ID is an execution
identity, not a secret/authentication scheme. PostgreSQL authorization uniqueness
prevents reuse under a new receipt, including after success, failure or recovery.
The authorization is consumed by reservation. An expired new reservation is
refused; an identical existing reservation remains readable after expiration.

## State, replay and uncertainty

Only RESERVED -> DISPATCHED permits execution, and only one caller wins.
Repeated mark_dispatched raises a refusal, even for the same owner. A lost
response after database commit is uncertain; do not send or retry automatically.
Receipt reads and reservation replays never grant dispatch permission.

The owner can finish DISPATCHED as SUCCEEDED, FAILED_CONFIRMED or UNCERTAIN.
Success requires positive measured sends; total tokens may remain unknown.
Failures require a bounded enum category, never raw exception text. Terminal
replays must match the stored evidence exactly; conflicting finalization fails.
Uncertain execution retains concurrency. Expiry alone never releases capacity.

Explicit operator confirmation that the execution is dead can recover RESERVED,
DISPATCHED or UNCERTAIN (including a crash before recording terminal evidence).
Recovery records an operator UUID, retains reservations/observations, leaves the
receipt terminal UNCERTAIN, and releases only concurrency. It never restores an
authorization or allows dispatch. It is the operator caller's duty to establish
death; a database marker cannot kill or fence an already running HTTP request.

## PostgreSQL and tests

One transaction-scoped advisory lock serializes these low-volume operator writes.
Primary key, authorization uniqueness and partial active-receipt uniqueness
remain database backstops. Canonical JSON is stored for exact replay comparison;
a SHA-256 digest is recorded too. No hand-built delimiter encoding.
All connections close on exit, connect timeout 5s, statement timeout 5s,
lock timeout 2s, no transaction spans inference. Database time governs expiry.
No in-memory duplicate store, ORM, new dependency, web endpoint or implicit
migration. Structured business workflows and production provider wiring remain
unchanged. Schema v3 is still unpublished, so repair v3 in place; v1/v2 are intact.

Acceptance requires actual PostgreSQL concurrent duplicate reservations,
distinct-active races, one dispatch winner, changed evidence conflicts, expired
replay versus dispatch refusal, owner mismatch, crash recovery from all three
unfinished states, unknown-usage preservation, and fresh-process durability.
Verify v2-to-v3 data preservation and repeat migration, and run the full backend
suite against disposable local PostgreSQL. Tests use synthetic authorizations
only. This is not live provider proof, final security audit or release approval.
