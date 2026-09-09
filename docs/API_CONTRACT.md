# Frozen API contract v1 — M1 business routes, with M2 amendments
Current implementation: M2B additive tasks and health.milestone="M2B" are accepted locally; see M2B_INTEGRATION.md. The target/pending wording below is historical dispatch context.
M2A_CONTRACT.md and its dated amendments govern the accepted intake implementation. M2B_CONTRACT.md freezes the additive task snapshot and M2B health label for the next implementation; these are not implemented at this documentation checkpoint. Business transitions below retain their original shapes.
Relative same-origin JSON routes. UTC ISO-8601 timestamps. due_at must be future and <=30 days ahead. IDs opaque strings. Reject unknown fields.
Reads and mutations return 200 except workspace creation 201.
Error: {error:{code:string,message:string}} with 401 no/expired session, 403 wrong origin, 404 missing scoped entity, 409 conflict, 422 validation. No framework trace.
All POST except workspaces and the read-only /api/intake/interpret require Idempotency-Key (8–100 chars). Same workspace/key/route/body returns exact saved status/body with no extra event; changed route/body ->409 IDEMPOTENCY_CONFLICT. Hash canonical parsed input. Validation failures may remain uncached.
If mutation Origin supplied it must match configured frontend origin. Cookie/session expires after 24h. Production TLS/abuse protections are release gate.

## Shapes
Equipment: {id,label,kind,state,version}
kind: WHEELCHAIR | WALKER | CRUTCHES
state: AVAILABLE | RESERVED | ON_LOAN | AWAITING_INSPECTION | REPAIR | QUARANTINED
version integer >=1, increment per state mutation.
Request: {id,borrower_label,equipment_kind,pickup_location,due_at,status,created_at}
status: REQUESTED | RESERVED | ON_LOAN | RETURNED | CLOSED
borrower_label 1–60 chars synthetic; pickup_location 1–120 chars; no medical/contact field.
Loan: {id,request_id,equipment_id,status,due_at,created_at}
status: RESERVED | ON_LOAN | RETURNED | CLOSED
Event: {id,entity_type,entity_id,action,at}
Snapshot at accepted M2A: {equipment:Equipment[],requests:Request[],loans:Loan[],events:Event[],agent_mode:"disabled"|"strands_ollama"}. M2B adds required tasks as specified in M2B_CONTRACT.md. Legacy clients may recognize "not_implemented"; current agent_mode describes configuration only.
Events newest first; no private input echoed. Other list ordering stable by created/id.

## Routes
GET /api/health -> {status:"ok",milestone:"M2A",agent_mode:"disabled"|"strands_ollama"}, public at the accepted implementation. M2B implementation changes only milestone to "M2B".
POST /api/workspaces body {} -> {workspace:{id},snapshot:Snapshot}. Sets opaque unpredictable server-issued bs_session cookie. Seed one AVAILABLE WHEELCHAIR, one AVAILABLE WALKER, one QUARANTINED CRUTCHES; no requests/loans. Separate workspace per creation.
GET /api/snapshot -> Snapshot scoped by cookie.

POST /api/requests {borrower_label,equipment_kind,pickup_location,due_at} -> {request:Request}
Creates REQUESTED and event REQUEST_CREATED. Invalid field/date ->422 VALIDATION_ERROR.

POST /api/reservations {request_id,equipment_id,expected_equipment_version,human_approved:true} -> {request:Request,equipment:Equipment,loan:Loan}
Only REQUESTED and matching AVAILABLE item/version. Missing/false human approval ->422 APPROVAL_REQUIRED. State/version/allocation conflict ->409 STATE_CONFLICT. Atomically request RESERVED, equipment RESERVED, loan RESERVED; event RESERVED. Other requests unchanged.

POST /api/loans/{id}/pickup {expected_equipment_version,human_approved:true} -> {request,equipment,loan}
Only RESERVED loan/item; request/item/loan ->ON_LOAN; event PICKED_UP.

POST /api/loans/{id}/return {expected_equipment_version,human_approved:true} -> {request,equipment,loan}
Only ON_LOAN; request/loan RETURNED, equipment AWAITING_INSPECTION; event RETURNED.

POST /api/equipment/{id}/inspection {expected_equipment_version,outcome,human_approved:true} -> {equipment:Equipment,request:Request|null,loan:Loan|null}
outcome AVAILABLE | REPAIR | QUARANTINED.
Allowed only from AWAITING_INSPECTION, REPAIR or QUARANTINED with current version. Human affirmation required for every outcome. Never while RESERVED/ON_LOAN/AVAILABLE.
If RETURNED loan exists, close loan/request regardless of outcome. Later reinspection may return null loan/request. Set item outcome, increment version, append INSPECTED_AVAILABLE/INSPECTED_REPAIR/INSPECTED_QUARANTINED.
All transition routes enforce human approval, current version, same-workspace IDs and idempotency. Invalid transition ->409 STATE_CONFLICT.

## Client
Initial GET snapshot; on 401 show Start synthetic workspace button, never silently reset.
After mutation success refetch snapshot; only show success after server confirmation.
409: refetch, explain competing update and preserve inputs.
Fresh idempotency key per deliberate action; retain exact key/body for uncertain-network retry. Never replay changed inputs under same key.
Explicit human confirmation before human_approved:true; no prechecked approval.
Runtime HTTP only. Test mocks implement exact contract and cannot be bundled as runtime fallback.
