# Public hosting feasibility - Codex disposition (2026-09-09)
Superseding user instruction: public deployment must use hosted free platforms, never the user's computer. The personal-origin/tunnel candidate and host/domain question below are retired. See M3_HOSTED_DIRECTION.md for the current direction. M2B runner is now locally implemented; the stack description below is historical at f1e9428.

Status: CONDITIONAL CANDIDATE; no hosting approved or provisioned. Supersedes BS-006's recommendation at worker commit ef5bbfb. The original report remains in that worktree for provenance; its claims are not accepted merely because the worker marked it complete.

## Verified local facts and corrections
At canonical f1e9428: Python/FastAPI, React 18.3.1, SQLite WAL and loopback Ollama llama3.2:3b. The due runner and FastAPI SPA mount are not yet implemented. A quoted 3.5 GB RAM requirement is an estimate, not a measurement of this host. M2A has local real inference evidence; there is no public-release evidence.
Reject "sole", "100% compatible" and guaranteed zero-cost assertions. A local host plus named Cloudflare Tunnel is a candidate only if an existing suitable domain/account and reliably powered host/network are available without new personal spend. Capacity, persistence/backups, process restart, static serving, TLS/session/origin/abuse protection and judging-period availability must be proven. No provider/account activation is authorized by this report.

## Official source recheck
Checked 2026-09-09:
- [Cloudflare Quick Tunnels](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/) are for testing/development, with no uptime SLA. Reject as the production hosting recommendation.
- [Cloudflare managed tunnel setup](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/) requires a Cloudflare account and website/domain setup for this published-hostname route. User domain entitlement is unknown; free tunnel software does not prove free domain ownership or reliable origin uptime.
- [Render free services](https://render.com/docs/free) have ephemeral local filesystems; persistent disks require a paid service. Free Postgres expires after 30 days. The current SQLite stack is therefore not accepted on a free Render instance. This finding does not establish that every cloud provider is infeasible.
- [Hugging Face storage](https://huggingface.co/docs/hub/spaces-storage) documents ephemeral local disk and recommends Storage Buckets. It does not support the worker's quoted $5 persistent-storage price. A bucket is not automatically a verified SQLite WAL filesystem. No HF deployment is selected.
- AWS grant, account state, applicable plan and any payment/credit conditions remain unverified. Remove blanket tax/card/uncapped-charge claims from the decision; no pricing or financial conclusion is justified by the worker's evidence.

## Official event gate
Devpost connector fetched full official rules and requirements on 2026-09-09 (about 02:45 UTC), sourced from [official rules](https://agentsforhumans.devpost.com/rules) and [event](https://agentsforhumans.devpost.com/).
Submission closes 2026-09-15 00:00 UTC / 05:30 IST. Judging ends 2026-10-09 00:00 UTC / 05:30 IST.
Rules require working-project access free of charge through judging. A live demo URL is optional in the submission form, but the user's v5 requirement is stronger: all three products must be public, live and persistent. Video-only, scheduled-hours or repository-only fallback is NOT approved.
Strands is required; AgentCore is optional. Public source with MIT/Apache license, setup instructions/architecture diagram, an English-understandable public YouTube/Vimeo video of at most five minutes and AWS Builder ID remain submission obligations.
The how-to-enter rules also require an AWS account. Existing registration acknowledgment is retained; AWS account completion must be verified separately from Builder ID before submission. Credits were requested earlier but a grant is not verified.
This is a targeted recheck, not final eligibility or release acceptance.

## Next step
User input requested: which existing always-on host and domain can remain available through the judging end. Do not infer ownership from unrelated project addresses. Continue independent M2B implementation while that answer is pending. Then make one bounded deployment decision from actual entitlements; no further broad feasibility report or worker correction loop.
