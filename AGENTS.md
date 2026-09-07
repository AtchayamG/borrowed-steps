# Borrowed Steps worker rules
Authority: live official rules > program v5 blueprint > project specs/architecture/contracts > task brief > worker choices.
Program: D:/Work/Codex/Hackathon Projects/Agents For Humans/00_PROGRAM_CONTROL
Manual workers. Codex owns architecture/main/integration/release. Use assigned worktree; never merge/push/deploy.
Read C:/Users/Atchayam/.codex/RTK.md; prefix shell commands with rtk (rtk proxy for unsupported commands).
Apply C:/Users/Atchayam/.codex/skills/orchestrate-external-coding-agents/SKILL.md with user overrides: manual invocation, exact requested models, local task commits allowed, no extra agents or Hermes substitution.
Apply C:/Users/Atchayam/.codex/plugins/cache/ponytail/ponytail/4.9.0/skills/ponytail/SKILL.md; preserve required architecture, validation and tests.
Domain depends only on Python stdlib; application on domain/ports. FastAPI/Pydantic/SQLite/Strands/provider adapters belong outside.
₹0 spend. No cloud activation, card entry, paid inference, credentials discovery/copying, external messages, publishing or production data changes. Project-local packages from official registries and local tests allowed.
Synthetic data only; no diagnosis, medical suitability or ranking humans. Human allocation and inspection; no fake success/inference/integrations.
Before coding verify actual exact session model/effort. If unavailable or uncertain, write BLOCKED_MODEL with observable evidence; do not silently substitute.
Before return update docs/TASKSTATUS.md, HANDOVER.md, TEST_STATUS.md, REVIEW_QUEUE.md in your worktree; copy task report to docs/workers/<TASK_ID>.md.
Shared checkpoint filenames are worktree-local and reconciled by Codex; all other ownership is disjoint.
Statuses: READY_FOR_REVIEW, COMPLETED (Codex acceptance), BLOCKED, BLOCKED_USAGE_CLAUDE, BLOCKED_USAGE_AGY, BLOCKED_MODEL, TEST_FAILURE, ARCHITECTURE_DECISION_REQUIRED, SECURITY_DECISION_REQUIRED, DEMO_VALIDATION_REQUIRED, VIDEO_REVIEW_REQUIRED.
Preserve partial diff/status/tests on quota. Repair ordinary test/build failures; stop on architecture/security/contract conflicts.
