# Engineering Maturity

## Stable module boundary

- `agent_recipes/core.py`: governance and recipe domain orchestration.
- `agent_recipes/persistence.py`: errors, hashing, redaction, and atomic JSON/JSONL persistence.
- `agent_recipes/migration.py`: project schema inspection and explicit migration rules.
- `agent_recipes/ledger.py`: append-only event write, idempotency, locking,
  integrity inspection, and fail-closed mutation after corruption.
- `agent_recipes/lifecycle.py`: tombstone reconstruction, permanent recipe-id
  retirement, content anti-resurrection, revocation, and active version selection.
- `agent_recipes/outcome.py`: exact lock snapshot bindings, version-isolated
  success/failure/unknown attribution, confidence, maturity, degradation, and hold policy.
- `agent_recipes/execution.py`: strict lookup/no-match policy, applicability
  scoring, exact lock documents, expiry validation, and stale-lock retirement.

The split preserves the existing `agent_recipes.core` compatibility imports. It
does not claim that every domain has already been extracted from the core
orchestrator.

## Project schema

Fresh projects write `.recipes/project_schema.json` at schema `1.0`.

Legacy projects remain readable but report `legacy_unversioned` until this
explicit command runs:

```bash
agent-recipes migrate --target 1.0 --project /path/to/project --json
```

Migration appends one event and writes the marker. It does not rewrite existing
event rows. Malformed markers, future schemas, unsupported targets, and downgrade
requests fail closed.

## Build and isolated install

```bash
python3 -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist/wheels
./bin/verify-clean-install dist/wheels/agent_recipes_local-*.whl
```

The smoke creates a new virtual environment and a new project outside the
repository, installs only the wheel, then runs init, migration-status, and
doctor. This is clean-environment evidence on the same machine, not proof from a
second physical machine.

## CI boundary

`.github/workflows/ci.yml` defines Python 3.11, 3.12, and 3.13 unit, wheel, and
isolated-install jobs. The workflow file is locally inspectable; it is not a
successful hosted CI run until the repository is pushed and GitHub Actions runs
it.

## 2026-07-10 local measurements

- Final wheel: 182793 bytes; SHA-256
  `3de5f8fbecdca5eaea9b34e46412854c4a0ce305a1ed990da6a744c5fd9beedc`.
- `migration-status` startup: 8 local subprocess runs, median 58.25 ms, range
  54.72-95.16 ms on this Mac.
- Agent Recipes Python package: 10 source files, about 905185 bytes.
- Pinned Remnic reference at
  `019518b7ba11e9582484a147420c92f387b863c6`: 2821 files and about 72630904
  bytes excluding `.git`.

The weight figures show that the narrowed Agent Recipes runtime is materially
smaller, but the wheel and a source checkout are not equivalent artifacts.
Remnic was not installed in this project, so there is no honest local Remnic
startup, failure-recovery, or upgrade runtime number. Agent Recipes failure and
upgrade evidence is executable: malformed/future/downgrade migrations fail
closed, legacy events remain unchanged, and repeated migration is idempotent.

## 2026-07-10 stronger-and-lighter ratchet

- `core.py`: 16663 lines; `ENGINEERING_BUDGET.json` prevents growth above this
  checkpoint.
- Package Python source: 909116 bytes against a 950000-byte ceiling.
- Runtime dependencies: 0; required external services: 0.
- Stage F wheel: 184281 bytes against a 262144-byte ceiling; SHA-256
  `c3ab8a9e13650ce22c570ecdc37518a1fab9d93af94c66ca07fa4ba6a7c1b67f`.
- Corrupted or malformed event ledgers now return doctor evidence and reject all
  later mutations with `AR405` without changing ledger bytes.

The competitive claim and nine hard gates are defined in
`AGENT_RECIPES_COMPETITIVE_STANDARD.md`. Current status is five locally passed
gates and four open gates; this is not yet an overall superiority claim.

## 2026-07-10 lifecycle extraction ratchet

- `core.py`: reduced from 16663 to 16477 lines; the budget ceiling moved down to
  16477.
- Package Python source: 910003 bytes against the unchanged 950000-byte ceiling.
- Stage G wheel: 185359 bytes against the 262144-byte ceiling; SHA-256
  `6f0a8891c2b127f400a0817c4a8f0bc02e3aa66faeaf361ef97b7ec566264aab`.
- Same-content promotion under a renamed recipe id is blocked while a tombstone
  is active. Revocation permits a new id to be reviewed again but never restores
  the retired old id.
- Full suite: 230 tests pass; clean-wheel governance and MCP smoke pass.

## 2026-07-10 outcome extraction ratchet

- `core.py`: reduced from 16477 to 16212 lines; the budget ceiling moved down to
  16212.
- Package Python source: 910304 bytes against the 950000-byte ceiling.
- Stage H wheel: 186041 bytes against the 262144-byte ceiling; SHA-256
  `c1244d9dc5e4e7beff1802ef6824f35822d7c1db22eee96e68f733d35ce35a15`.
- Failures bind to exact recipe id/version/hash, so an old version hold cannot
  pause a newer hash/version.
- Legacy inferred outcomes remain warnings only; `unknown` is neutral; snapshot
  mismatches are rejected without changing confidence.
- Full suite: 235 tests pass; clean-wheel governance and MCP smoke pass.

## 2026-07-10 execution extraction ratchet

- `core.py`: reduced from 16212 to 15610 lines; the budget ceiling moved down to
  15610.
- Package Python source: 912814 bytes against the 950000-byte ceiling.
- Stage I wheel: 187482 bytes against the 262144-byte ceiling; SHA-256
  `dd0421f842e77b002d38bd3e9394eb13c92b97c7b7a2d1142712fa1fee8cc7fe`.
- Lookup and lock now share one independent execution policy for active recipe
  selection, strict no-match, version/hash binding, finite expiry, and retirement.
- A latent finite-expiry validation bug was removed: expiry parsing now has an
  explicit timezone-aware implementation and malformed or expired locks fail closed.
- Full suite: 239 tests pass in 80.144 seconds; clean-wheel governance, MCP,
  project doctor, Codex client smoke, and open-source audit pass.

## 2026-07-10 large-sample learning quality ratchet

- Added `agent_recipes/learning_quality.py` as an independent read-mostly
  evidence evaluator; `core.py` remains at its 15610-line ceiling.
- Package Python source: 936940 bytes against the 950000-byte ceiling.
- The fixed 19-project cohort passes all 9 learning-quality gates with 65 latest
  target self-runs, 1112 complete cards, 19/19 current accepted targets covered,
  and no direct formal recipe write.
- Full suite: 243 tests pass in 76.758 seconds.
- Project-local Codex MCP smoke exposes 53 tools, including the new summary tool.
- Stage J wheel: 193624 bytes against the 262144-byte ceiling; SHA-256
  `f2aee1679db9314c57b22325443785161745137d8fe033e1fe2426931b41b854`.
- A fresh temporary environment installed only the wheel and passed the full
  governance chain plus MCP entrypoint. Open-source audit passed after
  sanitization and excluded the private cohort fixture.

This advances one quality gate, not an engineering-distribution gate. Hosted CI,
version history, and a second physical machine are still unproven.

## 2026-07-10 three-repository scorecard ratchet

- Added machine-readable and human-readable three-repository scorecards without
  adding a runtime command or dependency.
- The machine check forbids point totals and keeps superiority claims false while
  any required blocker remains.
- Full suite: 247 tests pass in 73.495 seconds.
- Package Python source: 937028 bytes against the 950000-byte ceiling;
  `core.py` remains 15610 lines.
- Stage K wheel: 193644 bytes against the 262144-byte ceiling; SHA-256
  `b4ec3afa34f3a1509848c36ed7731efd07bf9a58bb31fb58f202891ade360079`.
- Fresh temporary install passed the full governance chain and MCP entrypoint.
- Open-source audit includes the scorecards but excludes all nested upstream
  checkouts and project runtime state.

This stage improves claim discipline, not product capability. Hosted CI, version
history, public distribution, and second-machine evidence remain open.

## 2026-07-11 same-corpus recall ratchet

- Added one executable quality gate and MCP tool without adding a runtime
  dependency or required external service. Qwen remains optional loopback-only.
- Fixed cohort evidence: 24 recipes, 227 source rows, 111 unique scored cases;
  report `recall_quality_d032c112963d` passed all four backend gates.
- Full suite: 258 tests pass in 92.943 seconds.
- Main doctor `doctor_4d52a703d6aa`: 272 events, no errors or warnings.
- Large-scale test-pool doctor `doctor_c1732e6807d9`: 152 events, no errors; its
  one warning is the preserved legacy-unversioned schema.
- Project-local Codex MCP smoke passes with 54 tools and exposes
  `agent_recipes_recall_quality_benchmark`.
- Package Python source: 972748 bytes; `core.py`: 15687 lines. This exceeds the
  previous Stage M source ceilings, so `ENGINEERING_BUDGET.json` records one
  explicit Stage N decision at 975000 bytes / 15700 lines. Stage O may not raise
  either ceiling.
- Stage N wheel: 203027 bytes, SHA-256
  `b387ccc114933193d2f5697013ff3836b67f099a586283f5f677e4f9e385813d`.
- Fresh temporary installation passes the full governance chain, doctor, MCP
  entrypoint, 54-tool exposure, and the wheel-size gate.
- Open-source audit includes 67 files and has no findings after sanitization.

Preserved engineering failures:

- The first Qwen service run disconnected under llama-server's default four-slot
  batching on a long recipe projection. The successful run used one slot and
  2048 batch/ubatch; repeatable service supervision is still missing.
- The first wheel command used the project `.venv`, which intentionally has no
  pip. The wheel was built with the existing system pip and no build isolation.
- A post-smoke summary script treated tool-name strings as objects and failed;
  the saved smoke JSON itself was valid and was re-read correctly.

This closes recall-quality evidence, not engineering maturity. Hosted CI,
version history, public release, supervised optional services, and a second
physical environment remain open.

## 2026-07-11 local distribution and recovery gate

- Release candidate version advanced from 0.1.0 to 0.1.1. MCP server metadata
  and local client smoke metadata match 0.1.1.
- Added wheel-installed `agent-recipes-qwen-service` with project-local
  start/status/restart/stop, loopback health checks, one-slot 2048 batch/ubatch,
  PID ownership verification, stale-process recovery, and refusal to signal a
  reused/mismatched PID.
- Real Qwen recovery passed: PID 54084 served a live query, was deliberately
  terminated, status returned nonzero and unhealthy, PID 54378 was started from
  preserved project state, the same live query returned the same top score
  0.748131, and final stop left no service process.
- Added `verify-upgrade-rollback`. The first run correctly failed because source
  checkout `egg-info` shadowed installed wheel metadata; the verifier now changes
  to its temporary directory before reading installed versions.
- Final upgrade chain passed: 0.1.0 -> 0.1.1 -> 0.1.0 -> 0.1.1, all doctors
  `ok`, with event hash
  `33ece082602c4680af6c3109e8c9c98dea7be99c744f76e3a107d7caa239ee51`
  unchanged at every step.
- Final 0.1.1 wheel clean-install governance and MCP passed on Python 3.11.15,
  3.13.13, and 3.14.6. Every installed environment also exposed the Qwen service
  command.
- Python 3.11.15 full suite: 261 tests in 92.081 seconds. Python 3.13.13 full
  suite: 261 tests in 132.126 seconds.
- Focused maturity/recall/distribution regression: 26 tests pass.
- Main doctor `doctor_943ee34d3600`: 273 events, no errors or warnings. Codex
  project smoke: 54 tools, doctor `ok`.
- Open-source audit: 70 included files, no findings after sanitization.
- Package Python remains 972748 bytes and `core.py` remains 15687 lines, so Stage
  O did not raise either Stage N source ceiling.
- Final 0.1.1 wheel: 206164 bytes, SHA-256
  `c4a8eb5ce64ddd33a67a5d6c70572ff87a170ed4fed62d6c577671f71c14bce4`.

What is still not proven:

- `.github/workflows/ci.yml` is defined but has not run on GitHub because this
  workspace has not been committed or pushed.
- Three Python versions on this Mac are independent interpreter environments,
  not a second physical machine.
- There is no public release or repository history. These remain the final
  narrowed engineering/distribution blocker and cannot be manufactured locally.

## 2026-07-11 public distribution gate

- Public repository: `https://github.com/chnczu-jpg/agent-recipes` under MIT.
- Hosted CI run `29115605471` passed Python 3.11, 3.12, and 3.13. Every job ran
  261 tests, built a wheel, and passed isolated wheel smoke.
- The workflow uses `actions/checkout@v7` and `actions/setup-python@v6`; the
  earlier Node runtime deprecation warning is no longer present.
- Public release `v0.1.1` contains a 210096-byte wheel with SHA-256
  `ed0faf74ec9ad757df3bf3bc9dddedd458d128de9d39b1df1aa534dd5b4f315a`.
- A fresh download from the release produced the same SHA-256 locally.

Hosted CI, public history, licensing, and downloadable distribution are now
proven. A second physical machine has not installed the exact release wheel, so
the final narrowed engineering/distribution blocker remains open.

## 2026-07-11 cause-specific feedback gate

- Outcome schema 1.1 preserves success/failure/unknown while adding 15 explicit
  feedback kinds across recipe, retrieval, execution, dependency, cost,
  conflict, result, evidence, and evaluation scopes.
- Non-recipe failures remain attributable but cannot degrade or hold a recipe.
- Recipe failures remain bound to exact id/version/hash and use the existing
  fail-closed degradation policy.
- Feedback only recommends review actions; it cannot edit or promote a formal
  recipe.
- New rich-feedback tests: 5/5. Focused new/legacy outcome and readiness tests:
  23/23. Broader affected regression: 110/110. Full suite: 266/266.
- `core.py` is 15695 lines against the 15700 ceiling. Package Python source is
  972860 bytes against the 975000 ceiling.

- Sanitized public 0.2.0 wheel candidate: 211488 bytes, SHA-256
  `fcb3e28d92b01073b147b631f7aed759de015a26c33adf5007d55d24c1c856d4`;
  isolated governance, doctor, MCP, and Qwen service entrypoint passed.
- Upgrade/rollback 0.1.1 -> 0.2.0 -> 0.1.1 -> 0.2.0 preserved the exact event
  hash `dfa211f4700ce6ba7708d02233d3d4d4daac791800854c16b48ca8a42789aad2`.

The 77-file sanitized export passed all 266 tests and isolated install. Hosted CI
run `29120637235` passed Python 3.11, 3.12, and 3.13, including all tests, wheel
builds, and isolated install smoke. Competitive extension stage 1 is ready for
the public 0.2.0 release.
