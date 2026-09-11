# BS-025 accepted after direct Codex repair

Independent review at ASTRA_MEDIUM of worker1b416d2, base d3fc192.
Scope: local hosted runtime packaging and offline verification only.
No production source, migration, HTTP configuration or frontend changed.

Repairs:
- Apply one audit guard before optional imports in every adapter child; reject
  outbound connect/connect_ex, DNS and datagrams. Numeric loopback remains for
  Windows asyncio. Regression tests exercise each blocked operation.
- Check all loaded borrowed_steps modules against the stage and installed
  distribution locations against the isolated runtime. The negative test now
  injects an outside module identity and requires rejection.
- Run exact pinned runtime closure and dependency metadata checks as part of
  the Groq verifier, rather than relying on a separate previous command.
- Replace duplicated adapter tests and hardcoded ignored staging paths with
  portable packaging regressions; tests no longer require worker-local output.
- Name the small synthetic structured-output schema FixtureExtraction. It is
  not the production extraction schema or proof of hosted orchestration.
- Update the existing PostgreSQL smoke's stale version2 expectation to the
  accepted schema3. Production migrations are unchanged.

Independent results:
- Fresh Python3.12.10 runtime:60 pinned Windows distributions; dependency check
  passes. Runtime pins match accepted source locks (including UTF-16 lock input).
- Locked frontend clean build; two fresh assemblies match:39 files,530459 bytes.
- Isolated staged Groq streaming/structured/refusal checks pass; synthetic HTTP
  only. Exact stage manifests unchanged before/after.
- Disabled hosted import/health/JSON404/auth/config/lifespan checks pass.
- Real disposable PostgreSQL staged business-workflow smoke passes at schema3.
  Own server stopped, disposable DB removed by existing verifier.
- 24 packaging tests pass,12 subtests; no skips. Ruff and strict mypy checks pass.
  Two existing dependency deprecation warnings remain.
- Installed distribution measurement85641958 bytes is Windows-only, not a
  measured Linux/Vercel bundle. No platform build, account change or provider call.

First reviewer test run failed on decoding the pre-existing UTF-16 source lock;
the test now handles its BOM without modifying the lock. The first database
smoke exposed the stale version2 assertion; rerun passed after correcting it.
Historical worker reports/evidence remain reports, superseded by this acceptance.

pywin32 retains an explicit Windows-only marker because MCP declares it on
Windows; it is excluded on Linux. This resolves the old packaging exclusion
for the expanded Strands closure without omitting a required Windows dependency.
The production Ollama interpreter is not imported by the hosted adapter test;
full hosted interpreter integration and quota admission remain pending.

BS-023's successful live operator canary remains separate and its grant closed.
BS-024 is still rejected/unmerged. Public assistant remains disabled.
