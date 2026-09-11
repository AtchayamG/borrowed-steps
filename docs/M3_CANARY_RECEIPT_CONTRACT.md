# M3 canary receipt contract — durable authorization boundary

Codex architecture decision, 2026-09-11, after BS-020 offline preparation.
This contract defines the smallest durable gate required before one real Groq
canary. It does not authorize a provider call or enable the public assistant.

## Purpose

Every future canary must create one durable receipt before dispatch. The receipt
binds the invocation to one exact BS-020 plan hash, one explicit dated
authorization, and fixed request/token reservations. A retry may read the same
receipt, but it may never dispatch a second provider invocation or change its
plan, limits, fixture, or authorization.

## State machine

Allowed states are `RESERVED`, `DISPATCHED`, `SUCCEEDED`, `FAILED_CONFIRMED`,
and `UNCERTAIN`. A receipt is created atomically in `RESERVED`; only the owner
may advance it. `UNCERTAIN` is terminal for dispatch: it retains every
conservative request/token debit and blocks new canaries until explicit human
recovery records that the prior invocation is dead. Recovery may release an
execution/concurrency marker, but it never refunds quota. No lease expiry or
process restart may turn `UNCERTAIN` into available quota automatically.

`SUCCEEDED` and `FAILED_CONFIRMED` are terminal and retain an immutable receipt
and evidence. A confirmed failure may release only capacity that is proven not
to have been sent; uncertain or partially sent work keeps its debit.

## Immutable fields and checks

The receipt stores a cryptographically random receipt id, exact plan hash,
authorization id and UTC expiry, provider/model, request and token reservation,
and creation time. The plan hash must equal the accepted BS-020 candidate plan
hash byte-for-byte. `live_authorized=false`, missing authorization, expired
authorization, mismatched plan, duplicate id, changed payload, negative or
unknown reservation, and any limit widening fail closed before dispatch.
Secrets, prompts, headers, raw responses, and credentials never enter the
receipt or evidence.

## Durability and replay

Use the existing explicit PostgreSQL adapter/migration boundary; do not add an
in-process-only lock or a JSON file. Receipt insert, uniqueness checks, state
transitions, and reservation accounting are short transactions. No transaction
may remain open during HTTP/inference. A duplicate receipt id returns the
original immutable record only when the request is byte-identical; a different
plan or payload is a conflict. Concurrent inserts cannot create two active
receipts.

## Scope of the next implementation task

Implement the receipt table/migration and a small storage/service boundary with
offline and disposable-local-Postgres tests. Keep public routes, hosted wiring,
provider credentials, production admission, and assistant enablement unchanged.
The task must prove atomic insert/duplicate conflict, exact plan binding,
state-transition rules, uncertainty debit retention, explicit recovery, and
concurrent active-receipt exclusion. It must not contact Groq, discover a key,
deploy, or spend money. The future live canary remains separately authorized by
Codex after this task is accepted.
