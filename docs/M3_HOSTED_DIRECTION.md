> 2026-09-09: [M3 hosted contract](M3_HOSTED_CONTRACT.md) defines the approved hosted-mode amendments and foundation tasks. Local behavior below remains unchanged; public deployment is pending.

# M3 hosted-only direction - 2026-09-09
User explicitly rejects using their own computer as a public origin because it cannot stay online through judging. This supersedes every pending request for an always-on personal host/domain and the local Cloudflare Tunnel candidate.
Public runtime must use hosted free services and a platform-provided hostname; no personal machine, tunnel to it, paid plan, card activation, trial auto-conversion or assumption of granted credits.
User screenshot shows an existing Vercel Hobby workspace. It is evidence of that displayed account/plan, not proof of deployment authorization for unrelated projects, remaining quota at action time, API credentials or connected database/provider entitlements.
Vercel is the preferred candidate, not yet a proven complete deployment. GitHub Pages can serve static content only; current Python/SQLite/runner/Ollama stack cannot be published unchanged as a full Pages site.

## Verified constraints, official sources checked 2026-09-09
- https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages : static site hosting.
- https://vercel.com/docs/frameworks/backend/fastapi : FastAPI can deploy as a Vercel Function; this is not evidence that an always-running lifespan thread will survive.
- https://vercel.com/docs/functions/runtimes : read-only filesystem with ephemeral writable scratch; not durable SQLite hosting.
- https://vercel.com/docs/functions/limitations : function execution is bounded; check exact Python packaging/memory/runtime duration for the deployed project.
- https://vercel.com/docs/cron-jobs/usage-and-pricing : Hobby jobs run at most daily with imprecise timing. Do not silently substitute daily reminders for current30-second coordination.
- https://vercel.com/docs/plans/hobby : personal/non-commercial restrictions and capped free usage apply; verify account eligibility/quotas before deployment.
Hosted database, scheduler and model provider are still unselected. Neon pricing retrieval was inconsistent/failed on direct open; search excerpts are insufficient to promise quotas. Groq documents a free tier (https://console.groq.com/docs/rate-limits, https://console.groq.com/docs/billing-faqs); account/model/tool/schema/rate/privacy suitability has not been proved.
Frequent scheduler access can keep a sleeping database active and exhaust monthly compute; include that in feasibility calculations. Free services are not an uptime guarantee. Never infer a free model API from a paid chat subscription.

## Bounded next tasks
BS-009 Claude: produce a concrete hosted migration decision artifact, compare at most two complete topologies, verify primary sources and installed Strands support, state exact adapters/contracts/tests needed. No production edits, provider calls or account changes. Codex decides migration/security and new inference budget after review.
BS-010 AGY: improve public-repository README, current architecture diagram and truthful local verification guide using accepted evidence. No cloud claims, application edits or publishing.
Both use independent worktrees based on the current docs decision commit. Keep M2B accepted local source17a6838 unchanged. All29 inference allocations remain closed.
NEXT_CODEX_MODE: ASTRA_LIGHT
REASON: HIGH direction settled; manual bounded worker work next. HIGH reviews the concrete hosted migration proposal later; serious security decisions/final gates use their required higher modes.
