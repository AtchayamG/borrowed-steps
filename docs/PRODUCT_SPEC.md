# Product specification — BS-000 accepted 2026-09-07
User: volunteer coordinating a community equipment room in Velachery, Chennai.
Pain: two requests for the only ready wheelchair; another returned item still needs inspection.
Outcome: human resolves allocation, coordinates pickup and keeps uninspected equipment unavailable through return.
Claim: Borrowed Steps keeps community mobility equipment moving to the next person who needs it.

## Full product
Request text -> Strands extracts supported fields/asks clarification -> deterministic policy -> inventory/readiness tools -> human allocation -> atomic reservation -> pickup coordination -> confirmation -> due reminder -> return -> human inspection -> available/repair/quarantine.
Human always allocates; no urgency/medical ranking. Requested equipment is not prescribed by the agent.
M1 implements structured intake and the persisted state workflow/UI. M2 adds load-bearing Strands and persisted coordination/due processing. M1 is never final live-agent proof.
Notices are persisted in-app tasks unless a later authorized channel exists; never imply SMS/email delivery. Final due processing runs independently of browser tabs, idempotently.

## M1
Equipment room, structured requests, allocation confirmation, loan detail/pickup/return, inspection and durable event history.
Loading/empty/error states, 409 conflict recovery, keyboard and responsive behavior.
Each browser receives isolated synthetic workspace via server cookie. No real health/contact data.
Judge story: two wheelchair requests; allocate one; second pending; stale competing allocation fails. Pickup, return, quarantine; no second loan. Reinspect/release, then allocate waiting request.
Measure actual workflow later; no invented savings.
Visual: equipment room/library checkout; warm neutral, deep indigo, amber readiness tags, text status labels. English-first; synthetic Velachery pickup details. No copying LumaLoad identity.
Excluded: medical suitability, payments, real-person dispatch, OCR/maps, ranking humans, organization admin, chat-only experience.
