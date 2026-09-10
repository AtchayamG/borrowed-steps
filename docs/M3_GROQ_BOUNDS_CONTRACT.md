# BS-019: enforce the request envelope before admission design

Codex decision, user-authorized ASTRA_HIGH, 2026-09-10; base75d3941.
Retain the Vercel + PostgreSQL + Groq candidate. BS-018 is accepted locally.
No provider calls, account activation, deployment or new inference allowance.

## Decision

BS-015 measured sample request bodies but did not bound them or generated output.
Implement two mechanical limits in the existing Groq adapter, independently of
future token accounting. These are engineering ceilings, not measured safe quota
allocations or a claim that all permitted inputs will complete successfully.

- Maximum serialized request body:16,384 bytes per actual send, including schema,
  tool specs, tool results, every message and JSON escaping. Measure the actual
  SDK HTTP body, not a reconstructed/shortened string. Reject before dispatch.
- Explicit `max_completion_tokens:1024` on both streaming tool-loop calls and
  structured-output calls. Request `reasoning_effort:low`; keep one completion.
  The fixed cap cannot be removed/increased by kwargs or mutable parent config.
  Do not send deprecated `max_tokens` or conflicting output-limit fields.
- Keep existing six-send sticky budget, absolute110s operation deadline,60s
  request timeout, pinned provider/model, no redirects/proxies/hidden retries,
  cancellation and owned-client cleanup. Both stages share the same budget.

Set supported fields through the existing SDK configuration/formatting path;
validate the final serialized payload in the existing transport before it charges
or forwards a request. Do not rewrite bytes after serialization, create another
transport framework or introduce a tokenizer/runtime dependency in this task.
Empty/non-object/malformed JSON, missing/invalid cap, wrong model, non-integer
or boolean cap, conflicting fields and excess bytes fail closed with generic
errors containing no credentials, prompt/history, payload or raw target values.
`n` may be absent (provider default1) or exactly integer1. Reasoning effort must
remain low in the captured payload. Reject mutation rather than silently widening.

No silent truncation of intake/tool evidence, fabricated extraction or success.
A limit failure leaves the existing human workflow available. This adapter is
not wired to the hosted route yet, so no public API/UI/schema changes are needed.
Length-limited/incomplete structured output must never become a valid draft.
Preserve provider429/failure semantics and do not add automatic retry loops.

## Evidence required

Use real installed Strands/OpenAI SDK serialization intercepted by a fail-closed
offline transport. Prove caps on both stages, continuations/retries, and the sixth
send; the seventh cannot dispatch. Test exact byte boundary and boundary+1,
Unicode/escaping, growing tool-result/history/schema bodies, missing/bad cap and
parent-config/kwargs mutation. Rejected sends must not call the inner transport
or consume a physical-send debit. Exercise incomplete/length responses with the
real SDK and assert an explicit failure, preserving cleanup/deadline tests.

Reuse BS-015 fixtures/probe functions; do not overwrite their accepted historical
measurements. Save new counts-only evidence under test-evidence/bs019. Fixture
responses must be labelled synthetic; never report fixture usage as provider
measurements. Include1024 and16384 in actual captured request assertions.

This bounds total submitted JSON to at most98,304 bytes across six sends and
requests at most6,144 generated tokens in total, subject to provider semantics.
It does NOT bound input tokens or prove that combined/input/output token limits
fit8,000TPM, nor prove reasoning accounting/server enforcement. Payload bytes are
not tokenizer counts. Preserve those explicit unknowns instead of adding a
characters/4 estimate. No interpretations/day claim or admission enablement.

## Next release sequence

1. Accept BS-019's mechanical limits and exact wire evidence.
2. Verify existing account access, actual Groq organization limits (including any
   input/output split and other consumers), Vercel remaining shared usage and
   Neon free-plan entitlement/lifetime. No secret values in reports or chat.
3. Codex defines a separately authorized small provider canary and its receipt
   accounting. Current task grants zero calls. Compare actual provider semantics,
   tools/strict output, length failures and source-grounded results. No samples
   may be promoted to a universal token bound.
4. Complete the smallest defensible global request/token admission design from
   that evidence. Uncertain invocations retain debits and block new inference;
   no lease-expiry refund and no DB transaction held across inference.
5. Integrate the enabled hosted interpreter, refresh package including provider
   dependencies, validate hosted platform/persistence and activate release only
   under the separate release gate. Keep structured human workflows usable when
   model quota is unavailable. MAX final audits/video/submission remain required.

## Official-source check (2026-09-10)

- [Groq API](https://console.groq.com/docs/api-reference): max_completion_tokens
  controls generated tokens; n currently1; gpt-oss20b supports low reasoning.
- [Groq reasoning](https://console.groq.com/docs/reasoning): cap/examples; no
  assertion here that low reasoning guarantees completion within1024 tokens.
- [Groq rate limits](https://console.groq.com/docs/rate-limits): organization
  scope, actual-console exceptions and possible separate input/output limits;
  reference20b table30RPM/1000RPD/8000TPM/200000TPD. Not account entitlement.
- [Vercel Hobby](https://vercel.com/docs/plans/hobby): personal/non-commercial
  restriction and shared capped resources remain material. No account measured.
- Neon plan and markdown endpoints failed via web reader this turn; do not treat
  prior quota numbers as freshly verified. Verify official source/account later.
- [Hackathon resources](https://agentsforhumans.devpost.com/resources) now states
  all AWS credits have been disbursed. Earlier personal grant request receipt
  remains valid; whether this user received credits is unconfirmed.
- [Rules](https://agentsforhumans.devpost.com/rules): submission Sept14 17:00PDT
  (Sept15 05:30IST); judging ends Oct8 17:00PDT (Oct9 05:30IST), unchanged.

This is a bounded implementation decision, not final security/release approval.
