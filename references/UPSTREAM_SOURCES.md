# Upstream reference sources

This directory records source repositories used for architecture research.
Checked-out repositories under `references/upstream/` are project-local,
read-only references and are ignored by the parent repository.

## Remnic

- Repository: https://github.com/joshuaswarren/remnic
- Pinned commit: `019518b7ba11e9582484a147420c92f387b863c6`
- Local checkout: `references/upstream/remnic`
- License: MIT, copyright 2025 Joshua Warren
- Runtime status: reference only; not imported, installed, or executed by Agent Recipes
- Approved use: source study, architecture comparison, and attributed adaptation
- First studied module: `packages/remnic-core/src/lifecycle/tombstones.ts`

## Cass Memory System

- Repository: https://github.com/Dicklesworthstone/cass_memory_system
- Audited commit: `5cefdb30ed94a06f6b2eafcad5998f5933e6528b`
- Local checkout: intentionally absent
- License status: restricted for OpenAI/Anthropic and their agents
- Boundary: do not download, execute, test, index, copy, or continue source-level analysis without express written permission from the author
- Allowed project input: independently stated product requirements such as no-match, outcome feedback, confidence changes, and doctor diagnostics; implementation must be clean-room and must not derive from Cass source

## TencentDB Agent Memory

- Repository: https://github.com/TencentCloud/TencentDB-Agent-Memory
- Pinned commit: `4339e63650920871eb0e8888083a1779d114e3ae`
- Local checkout: `references/upstream/tencentdb-agent-memory`
- License: MIT, copyright 2026 Tencent
- Runtime status: reference and local audit only; not imported by Agent Recipes
- Audit install: project-local `npm install --ignore-scripts --package-lock=false`; upstream `postinstall` was deliberately blocked because it can rewrite an installed OpenClaw `dist/` tree
- Approved use: source study, architecture comparison, and future optional-adapter evaluation
- Current decision: do not replace Agent Recipes; automatic Skill generation is still Roadmap, and the current source lacks review promotion, strict execution lock, outcome, lifecycle, and claim-boundary contracts
- Full audit: `TENCENTDB_AGENT_MEMORY_SOURCE_AUDIT.md`

## Reproducibility

To recreate the allowed Remnic reference checkout without installing dependencies:

```bash
git clone --filter=blob:none --no-checkout https://github.com/joshuaswarren/remnic.git references/upstream/remnic
git -C references/upstream/remnic checkout --detach 019518b7ba11e9582484a147420c92f387b863c6
```

To recreate the TencentDB Agent Memory reference checkout without installing dependencies:

```bash
git clone --filter=blob:none https://github.com/TencentCloud/TencentDB-Agent-Memory.git references/upstream/tencentdb-agent-memory
git -C references/upstream/tencentdb-agent-memory checkout --detach 4339e63650920871eb0e8888083a1779d114e3ae
```

The nested checkout must not be packaged as Agent Recipes source or treated as
an Agent Recipes runtime dependency.

Unified comparison truth is maintained in
`THREE_REPO_COMPETITIVE_SCORECARD.json` and
`THREE_REPO_COMPETITIVE_SCORECARD.md`. Those files compare fixed snapshots and
must not be read as claims about later upstream releases.
