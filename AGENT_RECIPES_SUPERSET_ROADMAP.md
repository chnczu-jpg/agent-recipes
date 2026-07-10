# Agent Recipes Superset Roadmap

Date: 2026-07-10

## Goal

Agent Recipes should be stronger than the useful combination of Remnic and
Cass without becoming a second general-purpose memory platform.

"Stronger" means a tested contract, not a longer feature list:

1. Match or exceed Remnic where lifecycle safety matters.
2. Independently provide the useful product behaviors associated with Cass.
3. Keep Agent Recipes' stricter candidate, review, no-match, execution lock,
   lock-bound outcome, and claim-boundary guarantees.
4. Stay project-local, inspectable, and fail-closed by default.

## Source boundary

- Remnic is MIT and may be studied or adapted with attribution.
- Cass source is restricted for the current OpenAI/Codex context. It is not a
  dependency or reference checkout. Cass-like behaviors must be independently
  designed from product requirements, not reconstructed from its code.
- The user-provided `深度系统拆解报告.docx` concerns the separate
  `coding_agent_session_search` project. Its clean-room principles and rejected
  scope are recorded in `CASS_REPORT_CLEANROOM_LEARNING_MAP.md`; its restricted
  source is not used.

## Superset acceptance matrix

| Capability | Remnic | Cass | Agent Recipes before this stage | Superset gate |
|---|---|---|---|---|
| Source provenance | strong | partial | strong | preserve source trace and claim limits |
| Candidate/formal isolation | partial | weak | strong | no automatic formal writes |
| Human promotion | partial | weak | strong | all formal changes remain explicit review decisions |
| Strict no-match | partial | strong | strong | weak or inactive recipes cannot be locked |
| Exact recipe execution lock | absent | absent | strong | preserve version/hash lock and lock-bound outcomes |
| Tombstone and anti-resurrection | strong | partial | documented, not implemented | append-only tombstone, exact content block, no silent reactivation |
| Revocation/undo | strong | partial | absent | revocation unblocks future review but never revives the old recipe id |
| Outcome attribution | strong | strong | basic | add recipe-version outcome counters and confidence only after lifecycle integrity |
| Doctor diagnostics | strong | strong | focused | expose lifecycle corruption, stale locks, and claim boundaries |
| Cross-agent access | strong | partial | local CLI/MCP | preserve core/CLI/MCP parity |
| Multi-axis machine readiness | strong in search domain | partial | doctor is mostly one status | expose governance axes and recommended actions |
| Stable machine response schema | strong | partial | parity tests but no golden readiness schema | lock public JSON shape with golden tests |
| Quarantine and budgeted packs | strong | partial | review queue exists, no general quarantine/pack contract | preserve evidence with omission/privacy reasons |
| Engineering maturity | strong | unstable at audited commit | weak | commits, CI, package, migration, second-machine proof |

## Keep in the core

- append-only event ledger and idempotency conflict protection;
- candidate -> review -> formal recipe state machine;
- strict lookup/no-match;
- recipe version/hash execution lock;
- lock-bound success/failure/unknown capture;
- lifecycle tombstone/revocation and anti-resurrection;
- doctor and minimal CLI/MCP surface.

## Move behind optional adapters

- source conversion, OCR, ASR, scene detection;
- Cognee, Graphiti, embeddings, and cloud LLMs;
- course extraction and software-skill drafting;
- benchmark and pressure-test suites that are not needed at runtime.

Optional does not mean deleted. It means these modules cannot determine core
truth, cannot write a formal recipe directly, and cannot be required for the
minimal install.

## Delivery stages

### Stage A: lifecycle integrity

Status: implemented and locally verified on 2026-07-10.

- Pin the Remnic reference and preserve its MIT notice.
- Implement recipe tombstone, revocation, and permanent retirement of the old
  recipe id.
- Block inactive recipes from lookup and lock.
- Block re-promotion of tombstoned content until explicit revocation.
- Add doctor, CLI, MCP, idempotency, and corruption tests.

Evidence:

- Remnic local reference HEAD: `019518b7ba11e9582484a147420c92f387b863c6`.
- `tests/test_phase13_lifecycle.py`: 6 tests pass.
- Full local suite: 192 tests pass.
- Main project doctor: `status=ok`, report `doctor_f3146ed75246`.
- The main project has no tombstoned formal recipes yet; lifecycle behavior was
  verified in isolated temporary projects and did not mutate production recipes.

### Stage B: outcome quality

Status: implemented and locally verified on 2026-07-10.

- Bind outcome statistics to exact recipe version/hash.
- Add explicit positive, negative, and unknown outcomes.
- Change confidence only from attributable outcomes.
- Never let confidence bypass human review or no-match.
- Add governance readiness axes and stable machine response contracts before
  confidence is allowed to influence recommendations.

Readiness foundation evidence:

- CLI `readiness` and MCP `agent_recipes_readiness` expose seven governance axes.
- `tests/golden/readiness_ready_v1.json` locks the ready response contract.
- `tests/test_phase14_readiness.py` covers ready, degraded, blocked, malformed
  review, unverified client, and core/CLI/MCP parity states.

Outcome quality evidence:

- New success/failure/unknown captures record exact recipe id/version/hash and
  a hash of the lock snapshot.
- Legacy captures are reconstructed from their lock for historical confidence,
  but `legacy_inferred_can_enforce=false` prevents retroactive blocking.
- `unknown` changes neither confidence nor failure streaks.
- Two policy-eligible failures with failures >= successes degrade the next lock
  and add a mandatory human-review claim limit.
- Three consecutive policy-eligible failures, or at least three failures at a
  60% failure rate, stop new locks with `AR440`.
- Automatic policy never accepts, rewrites, tombstones, supersedes, or promotes
  a formal recipe.
- CLI `outcome-status` and MCP `agent_recipes_outcome_status` expose the same
  exact-version confidence, maturity, and recommendation contract.
- Doctor rejects explicit binding/lock mismatches with `AR312`.
- `tests/test_phase15_outcomes.py`: 7 tests pass.
- Full local suite after Stage B: 205 tests pass.
- Main doctor: `status=ok`, 267 events, 10 attributable legacy outcomes,
  0 binding errors, and 0 unattributed outcomes.
- Main readiness is intentionally `degraded`, not blocked: two active recipe
  versions have historical failure warnings, while policy-eligible count is 0,
  so no automatic hold is active.
- Project-local client smoke exposes 47 MCP tools including
  `agent_recipes_outcome_status`.

### Stage C: memory boundary

Status: implemented and locally verified on 2026-07-10.

- Define an adapter contract for optional memory recall.
- Memory results remain evidence candidates.
- Core remains fully functional with all memory adapters disabled.

Evidence:

- Added CLI `recall-boundary` and MCP `agent_recipes_recall_boundary` for one
  machine-readable contract across Cognee, Graphiti, and Qwen.
- The contract forbids recall from writing formal recipes, accepting reviews,
  creating locks, changing lifecycle, or changing outcome confidence.
- Activation is explicit-command-only; recall is never auto-invoked by core.
- Broken recall indexes fail closed for the affected adapter and produce doctor
  warning `AR314`, while core readiness remains independent.
- A real `python -S` test completed init -> correction -> compile -> accept ->
  strict lookup -> lock -> success -> doctor -> readiness with all recall
  adapters disabled and no third-party site packages loaded.
- Main project contract: Cognee 21 candidates, Graphiti 152 node/edge
  candidates, Qwen 31 candidates, 0 violations; Zep remains out of scope.
- `tests/test_phase16_recall_boundary.py`: 4 tests pass.
- Full local suite after Stage C: 209 tests pass.
- Project-local client smoke exposes 48 MCP tools including
  `agent_recipes_recall_boundary`.

### Stage D: evidence hardening

Status: implemented and locally verified on 2026-07-10.

- Quarantine malformed candidate/evidence files without deleting them.
- Redact secrets before persistence, not only during open-source export.
- Build budgeted execution evidence packs with explicit omission and privacy
  reasons.
- Keep quarantined or omitted evidence out of formal review decisions until a
  human repairs or releases it.

Evidence:

- Added CLI/MCP `evidence-quarantine` with status, apply, and explicit repaired
  release operations.
- Quarantine moves malformed or secret-bearing candidate files out of active
  paths, stores original hash and reason, redacts credential-shaped content,
  and never auto-promotes released evidence.
- Event payloads are redacted before event hashing; JSON/JSONL/text candidate
  writes use the same persistence guard.
- Formal recipes and locks containing real credential values fail closed with
  `AR450` instead of being silently rewritten.
- Added lock-bound `evidence-pack` with byte budget, `minimal` or
  `project_local` privacy, explicit budget/privacy/quarantine omissions, and no
  formal recipe write path.
- A real main-project minimal pack used 5963/32768 bytes, included lock, exact
  recipe, and one related event, and explicitly omitted 9 candidate files.
- Real testing exposed a self-referential pack-idempotency bug; pack-created
  events are now excluded and the same request returns `unchanged`.
- Main evidence scan: 0 malformed issues, 0 secret findings, 0 active
  quarantines; doctor remains `ok`.
- `tests/test_phase17_evidence_hardening.py`: 5 tests pass.
- Full local suite after Stage D: 214 tests pass.
- Project-local client smoke exposes 50 MCP tools, including both Stage D tools.
- Open-source audit now checks credential-shaped values and finishes with 0
  findings after sanitization.

### Stage E: engineering maturity

Status: local engineering implementation verified on 2026-07-10; repository and
independent-machine gates remain open.

- Split the core monolith by stable domain boundary.
- Add repository history, CI, packaging, migration, and second-machine proof.
- Compare weight, startup time, failure recovery, and upgrade behavior against
  the pinned Remnic reference.

Local evidence:

- Extracted persistence and project-schema migration into independent modules
  while preserving compatibility imports from `agent_recipes.core`.
- Added `pyproject.toml`, a dependency-free wheel, CLI entry point, Python
  3.11/3.12/3.13 CI definition, and an isolated-install smoke script.
- Added schema `1.0`, CLI/MCP migration status and explicit migrate operations,
  doctor reporting, no-history-rewrite proof, idempotency, and fail-closed
  malformed/future/downgrade handling.
- Migrated the real main project from `legacy_unversioned` to `1.0`; all 269 old
  event bytes remained an exact prefix and one migration event was appended.
- Final wheel: 182793 bytes, SHA-256
  `3de5f8fbecdca5eaea9b34e46412854c4a0ce305a1ed990da6a744c5fd9beedc`.
- A fresh temporary virtual environment installed only that wheel and completed
  init -> correction -> compile -> review accept -> strict lookup -> lock ->
  success -> doctor plus MCP doctor.
- Final full suite: 221 tests pass; main doctor is `ok`, 270 events, schema
  current, 0 errors, and 0 warnings; client smoke exposes 52 tools.
- Static weight comparison: Agent Recipes package Python is about 0.9 MB and the
  wheel is about 0.18 MB; pinned Remnic source excluding `.git` is about 72.6 MB
  across 2821 files. These are not equivalent release artifacts.

Still open:

- `core.py` still contains most domain orchestration; this is the first physical
  split, not a claim that modularization is finished.
- No repository commit/history was created, hosted CI was not run, and no public
  release was made.
- Clean-environment proof is from the same machine, not a second physical
  machine.
- Remnic was not installed locally, so its startup/failure/upgrade runtime was
  not fabricated from static source. Only the pinned static boundary is compared.

### Stage F: stronger-and-lighter ratchets

Status: first ledger/lightness slice implemented and locally verified on
2026-07-10; overall Cass + Remnic superiority is not yet proven.

- Added `AGENT_RECIPES_COMPETITIVE_STANDARD.md`: nine executable gates define
  what “stronger” means in the narrowed safe-experience-reuse product.
- Added `ENGINEERING_BUDGET.json`: zero runtime dependencies, zero required
  external services, 256 KiB wheel, 950000-byte package source, 16663-line core,
  and 150 ms local cold-start median ceilings.
- Extracted `agent_recipes/ledger.py` as a deep module. Core compatibility methods
  remain, but locking, idempotency, hashing, inspection, and append now live at
  one seam.
- Ledger corruption or malformed JSONL blocks all later mutation with `AR405`;
  doctor reports `AR299`/`AR300`-`AR303`, and rejected writes leave bytes intact.
- `tests/test_phase19_ledger_and_lightness.py`: 5 tests pass.
- Full suite: 226 tests pass.
- Stage F wheel is 184281 bytes and passes the 262144-byte budget.

Current competitive gate count:

- locally passed: 5/9;
- partial or open: large-sample learning quality, fresh-agent production effect,
  memory/recall quality at scale, hosted CI/version history/second-machine proof;
- prohibited claim: Agent Recipes is not yet proven stronger than Cass and
  Remnic combined.

Next extraction: lifecycle, then outcome and execution locking. Optional search,
LLM, graph, OCR, and ASR remain outside the minimal core.

### Stage G: lifecycle module extraction

Status: implemented and locally verified on 2026-07-10.

- Added `agent_recipes/lifecycle.py` for recipe hashes, operational-content
  hashes, tombstone reconstruction, permanent id retirement, content
  anti-resurrection, revocation state, active version selection, and recipe paths.
- `core.py` keeps compatibility imports and effectful command orchestration, while
  lookup, lock, readiness, doctor, outcomes, and coverage consume the same
  lifecycle policy module.
- Added direct proof that a tombstoned recipe cannot be revived by changing its
  id, that revocation only permits a new id through review, and that the old id
  remains permanently retired.
- Added direct active-selection proof for superseded, retired, and older `_vN`
  recipe versions.
- `tests/test_phase20_lifecycle_module.py`: 4 tests pass.
- Affected lifecycle/readiness/outcome/ledger regression: 35 tests pass.
- Full suite: 230 tests pass in 70.416 seconds.
- `core.py`: 16477 lines; package Python source: 910003 bytes.
- Stage G wheel: 185359 bytes, under the 262144-byte limit.
- This improves reliability and maintainability but does not advance the four
  still-open real-quality/engineering competitive gates; gate count stays 5/9.

Next extraction: exact outcome state, then execution lookup/locking.

### Stage H: exact outcome module extraction

Status: implemented and locally verified on 2026-07-10.

- Added `agent_recipes/outcome.py` for lock binding validation, lock snapshot
  hashes, exact version/hash attribution, success/failure/unknown counts,
  confidence, maturity, historical warnings, degradation, and hard holds.
- Capture, lock creation, outcome status, readiness, doctor, and MCP continue to
  use the same compatibility imports from `core.py`.
- Policy text is now a single `OUTCOME_POLICY` contract instead of being repeated
  inside the status response.
- Direct tests prove v1 failures do not pause v2, legacy failures cannot enforce,
  unknown stays neutral, success resets consecutive-failure counting, aggregate
  failure rate remains independent, and snapshot mismatch is rejected.
- `tests/test_phase21_outcome_module.py`: 5 tests pass.
- Affected outcome/readiness/lifecycle/ledger regression: 33 tests pass.
- Full suite: 235 tests pass in 70.995 seconds.
- `core.py`: 16212 lines; package Python source: 910304 bytes.
- Stage H wheel: 186041 bytes, under the 262144-byte limit.
- Competitive gate count remains 5/9 because this is reliability/module evidence,
  not new large-sample real-task quality evidence.

Next extraction: execution lookup/locking policy.

### Stage I: execution lookup/locking policy extraction

Status: implemented and locally verified on 2026-07-10.

- Added `agent_recipes/execution.py` for strict query parsing, applicability
  scoring, active recipe selection, fail-closed no-match, exact lock documents,
  finite expiry validation, and stale/active lock retirement.
- `core.py` keeps compatibility re-exports and effectful event/file writes while
  lookup, lock creation, lock validation, and retirement delegate to one policy.
- Added direct proof for independent import, weak-query rejection, exact active
  version selection, recipe id/version/hash binding, finite future/expired locks,
  and centralized stale-lock retirement.
- `tests/test_phase22_execution_module.py`: 4 tests pass.
- Affected execution/outcome/lifecycle/ledger regression: 95 tests pass.
- Full suite: 239 tests pass in 80.144 seconds.
- Main doctor: `ok`, 270 events, 0 errors, and 0 warnings; readiness remains
  degraded because outcome-quality evidence is still incomplete, not because of
  the extraction.
- Project-local Codex client smoke: 52 tools and MCP doctor `ok`.
- `core.py`: 15610 lines; package Python source: 912814 bytes.
- Stage I wheel: 187482 bytes, SHA-256
  `dd0421f842e77b002d38bd3e9394eb13c92b97c7b7a2d1142712fa1fee8cc7fe`.
- A fresh temporary virtual environment installed only the wheel and completed
  init -> correction -> compile -> review accept -> strict lookup -> lock ->
  success -> doctor plus MCP doctor.
- Open-source dry-run audit includes the execution module and its direct tests,
  with no findings after sanitization.
- Competitive gate count stays 5/9 because this is structural reliability
  evidence, not large-sample learning or real-task quality proof.

Next major stage: large-sample real-source learning quality. Agent Recipes must
run its own refinery chain; Codex remains the referee and must not write the
candidate recipes on its behalf.

### Stage J: large-sample learning quality gate

Status: implemented and locally verified on 2026-07-10.

- Added independent `agent_recipes/learning_quality.py` plus CLI
  `learning-quality-summary` and MCP `agent_recipes_learning_quality_summary`.
- The gate reads existing self-run, card, review, and candidate-quality evidence.
  It does not read source contents, generate a recipe, accept a review, or write
  a formal recipe.
- Added fixed private cohort `fixtures/learning_quality_cohort_sample_project_v1.json`: 19
  correction/course/artifact/product-experience projects, minimum 50 latest
  self-run targets, minimum 1000 cards, and 100% current accepted-review quality
  coverage.
- First real report `learning_quality_7cd50d880291` failed closed on three real
  gaps: no correction-family self-run, only 923 cards, and one superseded old
  accepted review without a passing quality report.
- Agent Recipes then ran the correction source-refinery chain itself. Report
  `self_run_662a5048e4fa` passed its 9 technical checks and produced 14 cards,
  but candidate-quality report `candidate_quality_f0d5c4d670bd` caught unrelated
  topic mixing and 42 proposed values against a limit of 20. Codex rejected that
  review as referee; no formal recipe changed.
- Final report `learning_quality_2d8284e34e81` passed all 9 gates: 19/19 projects,
  all 4 required source families, 65 current target self-runs with 0 failures,
  1112/1112 complete candidate cards, 19/19 current accepted targets covered by
  passing candidate-quality evidence, and 0 direct formal-write violations.
- Historical failures remain visible: 175 candidate-quality cases include 110
  passes and 65 failures; 115 rejected reviews and 11 pending reviews were not
  hidden to make the summary green.
- `tests/test_phase23_learning_quality_summary.py`: 4 tests pass. Full suite:
  243 tests pass in 76.758 seconds.
- Main doctor: `doctor_4d52a703d6aa`, 272 events, no errors or warnings.
- Codex project client smoke exposes 53 tools and MCP doctor `ok`.
- `core.py` stays at 15610 lines; package Python source is 936940 bytes.
- Stage J wheel is 193624 bytes, SHA-256
  `f2aee1679db9314c57b22325443785161745137d8fe033e1fe2426931b41b854`,
  and passes the isolated full-governance plus MCP smoke.
- Open-source audit includes the module and direct tests, excludes private
  fixtures/runtime data, and has no findings after sanitization.

Competitive gate count advances from 5/9 to 6/9 for this fixed cohort only.
This does not prove live task execution, rendered media quality, universal
course understanding, user acceptance, or overall Cass + Remnic superiority.

Next major stage: expand fresh Codex A/B behavior evidence on genuinely new
tasks, preserving raw outputs and using Agent Recipes as the rule provider and
judge instead of letting the controller write the answers.

### Stage K: three-repository competitive closeout

Status: implemented on 2026-07-10; final regression and package evidence are
recorded in `PROJECT_STATUS.md`.

- Added `THREE_REPO_COMPETITIVE_SCORECARD.json` and a plain-language Markdown
  view covering Cass, Remnic, and TencentDB Agent Memory.
- Replaced informal total scores with a virtual-best rule: each row uses the
  strongest verified upstream capability, and one hard blocker cannot be hidden
  by wins elsewhere.
- Pinned boundaries remain explicit: Remnic and TencentDB are project-local
  read-only MIT references; Cass remains existing-audit-only because its license
  restricts this Codex/OpenAI context.
- Re-ran TencentDB's fixed local snapshot tests: 67/67 passed. Remnic was not
  installed or locally full-tested, and Cass was not downloaded or re-analyzed.
- Added a machine check that keeps both narrowed and overall superiority claims
  false while required blockers remain.
- Narrowed blockers: fresh-agent production effect, recall quality at scale, and
  engineering maturity/distribution.
- Overall-only additional gaps: broad conversation memory, context compression,
  host integration breadth, and recoverable background pipelines.
- Current allowed claim: Agent Recipes has a partial local lead in strict
  governance and execution safety and is lighter; it has not surpassed the
  three-repository virtual best.
- `tests/test_phase24_three_repo_scorecard.py`: 4/4 pass. Full Agent Recipes
  suite: 247/247 pass in 73.495 seconds.
- TencentDB fixed snapshot: 67/67 upstream tests pass in the current local run.
- Main doctor remains `doctor_4d52a703d6aa`, 272 events, no errors or warnings.
- Open-source audit includes both scorecard views and their direct test, excludes
  nested upstream checkouts, and has no findings after sanitization.
- Package Python source: 937028 bytes; `core.py` remains 15610 lines.
- Stage K wheel: 193644 bytes, SHA-256
  `b4ec3afa34f3a1509848c36ed7731efd07bf9a58bb31fb58f202891ade360079`;
  isolated governance and MCP smoke pass.

Next major stage: close the first narrowed blocker with genuine fresh Codex
no-recipe/recipe-aware A/B tasks and untouched raw outputs.

### Stage L: fresh-agent production-effect round 1

Status: implemented on 2026-07-10; blocker remains open.

- V4 preserved a 0 win / 2 tie / 4 baseline-win failure with a documented scope
  ambiguity. V5 fixed project identity and receipt visibility, then produced 3
  recipe wins, 2 ties, 1 baseline win, and one preserved wrong-lock attempt.
- The lost PIP case exposed a real learning-chain bug: correction capture accepted
  a lock but compile discarded its recipe binding and created duplicate recipes.
- Added `agent_recipes/corrections.py`; locked corrections now preserve exact
  recipe id/version/hash, compile into the one bound existing recipe, and never
  classify unverified correction text as `verified_path`.
- Three bad candidates were rejected. Review `review_9a57fb7a65b7` was accepted
  against the active PIP lock and produced PIP recipe version 2.
- V6 preserved a safe but invalid no-match result because Chinese `画中画` was not
  recognized. After the recall fix, V7 fresh Agent locked version 2 with
  `lock_c182e3c5eed8` and reproduced the missing-coordinate hard stop.
- This proves one failure can become a review-gated recipe repair and be reused by
  a fresh Agent. It does not pass the cohort-level production-effect gate.

Next major stage: run a new unseen multi-case V8 A/B cohort. Require at least four
recipe wins, zero baseline wins, zero wrong locks, and correct no-match before
closing the first narrowed blocker.

### Stage M: unseen V8 fresh-agent gate

Status: implemented on 2026-07-11; the predefined local fresh-agent blocker is
closed, while overall superiority remains false.

- Fixed six unseen tasks and rubrics before execution. Controller did not write
  either group answer and did not read or modify the external SampleProject project.
- Baseline and recipe-aware raw outputs were frozen before blind judging.
- Five positive cases selected the expected formal recipe and created real locks;
  the pure-code negative case returned AR242 and created no lock.
- Receipt-aware final blind result: 5 recipe wins, 1 tie, 0 baseline wins, 0 wrong
  locks, and correct no-match. The predefined gate passed.
- Invalid timeouts, one controller command error, local Codex startup noise, a
  model/CLI version mismatch, and a hanging subagent close call remain preserved
  in `invalid_attempts_v8.json`; they count against engineering maturity.
- This result proves local narrow behavior improvement on the fixed V8 cohort. It
  does not prove real editing execution, rendered quality, or arbitrary domains.

Next major stage: close `recall_quality_at_scale` with one fixed same-cohort
comparison across core lookup, Qwen, Cognee, and Graphiti, including precision,
false recall, no-match, latency, and resource use.

### Stage N: deduplicated same-corpus recall gate

Status: implemented on 2026-07-11; the fixed local recall blocker is closed.

- Added independent `agent_recipes/recall_quality.py`, CLI
  `recall-quality-benchmark`, MCP exposure, and direct fail-closed tests.
- All backends receive the same active-recipe cohort through benchmark-only
  projections; no formal recipe, candidate index, or review state is changed.
- The 227-row historical case pool contains 116 exact duplicate expectations.
  The gate scores 111 unique cases: 55 positive, 15 pure no-match, and 41
  target-overreach cases.
- The first raw run failed and remains evidence: llama-server disconnected on a
  long fourth request under its default four-slot setup, and the old client leaked
  a traceback. HTTP disconnects now fail closed; the successful local run used one
  slot and 2048 batch/ubatch.
- Final report `recall_quality_d032c112963d`: core 55/55 positive with zero false
  recall; Cognee/Graphiti projections each 53/55 with 3.6% false recall; live
  Qwen 47/55 with 6.3% false recall and about 31 ms mean query latency; all 15
  pure no-match cases were rejected.
- This closes one fixed SampleProject comparison gap. It does not prove native Cognee or
  Graphiti quality, arbitrary-domain recall, or a production-supervised embedding
  service.
- The executable gate added measured source weight: `core.py` rose from 15610 to
  15687 lines and package Python from 942478 to 972748 bytes. The explicit Stage N
  budget is therefore 15700 lines / 975000 bytes; Stage O may not raise it again
  and must hold or reduce both ceilings. Wheel runtime budget remains 262144 bytes.

Next major stage: close `engineering_maturity_and_distribution` with repeatable
service startup/failure handling, clean install and upgrade evidence, hosted-CI
or equivalent independent execution evidence, and second-environment reproduction.

### Stage O: local distribution, recovery, and rollback

Status: local portion implemented on 2026-07-11; external blocker remains open.

- Added a project-local and wheel-installed Qwen service supervisor. Fake-server
  tests cover lifecycle, missing model, and PID mismatch; a live run covers
  start, query, forced termination, unhealthy status, recovery, repeated query,
  and stop.
- Advanced package/MCP metadata to 0.1.1. `verify-upgrade-rollback` proves real
  0.1.0 -> 0.1.1 -> 0.1.0 -> 0.1.1 transitions without changing event bytes.
- Final wheel installs and runs the governance/MCP chain on Python 3.11.15,
  3.13.13, and 3.14.6. Python 3.11 and 3.13 independently pass all 261 source
  tests.
- Final 0.1.1 wheel is 206164 bytes, SHA-256
  `c4a8eb5ce64ddd33a67a5d6c70572ff87a170ed4fed62d6c577671f71c14bce4`.
- Core/package source budgets remain 15687 lines and 972748 bytes; no Stage N
  ceiling was raised.
- The first upgrade verifier run exposed checkout `egg-info` contamination and
  failed. The final verifier changes to a clean temporary cwd before reading
  installed metadata.

Not closed:

- No hosted CI result exists because no commit or push was performed.
- No second physical machine has installed the wheel.
- No public release or repository history exists.

Next major stage: external distribution proof only. After explicit permission to
commit/push, run hosted CI on the fixed 0.1.1 artifact; then install the same wheel
on a second physical environment and compare its hash and smoke receipt.

### Stage P: public repository and hosted distribution

Status: public and hosted-CI portions passed on 2026-07-11; second-machine proof remains open.

- Published `https://github.com/chnczu-jpg/agent-recipes` under MIT with a
  governed-memory README, contribution guide, security policy, and public topics.
- GitHub Actions run `29115605471` passed 261 tests, wheel build, and isolated
  install on Python 3.11, 3.12, and 3.13.
- Published `v0.1.1`; the 210096-byte wheel has SHA-256
  `ed0faf74ec9ad757df3bf3bc9dddedd458d128de9d39b1df1aa534dd5b4f315a`,
  and a fresh GitHub download matched it.
- The exact release wheel has not yet run on a second physical machine.

Next major stage: second-machine reproduction only. Compare the downloaded
wheel hash, doctor, governance chain, MCP entrypoint, and Qwen service command.

### Competitive extension 1: richer feedback than generic success/failure

Status: implemented and hosted-CI verified on 2026-07-11.

- Added 15 cause-specific feedback kinds without replacing the stable
  success/failure/unknown outcome layer.
- Retrieval mismatch, execution error, and external dependency failures are
  recorded against the exact lock but do not punish the recipe.
- Incorrect, outdated, incomplete, over-broad, conflicting, corrected, or
  excessively costly recipe use can degrade the exact version and recommend the
  appropriate review action.
- CLI and MCP expose the same contract. Feedback cannot directly write, accept,
  supersede, or tombstone a formal recipe.
- Full suite passed 266/266. The sanitized 211488-byte 0.2.0 wheel passed isolated install,
  and a real 0.1.1/0.2.0 upgrade-rollback cycle preserved event bytes.
- Cass still has broader implicit/session feedback. This row remains parity until
  a fixed same-case feedback benchmark compares both contracts.
- Hosted CI `29120637235` passed Python 3.11/3.12/3.13 with 266 tests, wheel
  builds, and isolated install smoke in every job.
- Public `v0.2.0` release is live and its freshly downloaded wheel matches the
  recorded SHA-256.

Next major stage: hybrid retrieval, conflict detection, and recommendation explanations.

## Stop rules

- Do not copy or analyze Cass source without written permission.
- Do not import Remnic as a runtime dependency merely to increase feature count.
- Do not call the system stronger until each matrix row has executable evidence.
- Do not let optional learning or memory modules write formal recipes.
