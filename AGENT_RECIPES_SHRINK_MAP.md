# Agent Recipes Module Shrink Map

Date: 2026-07-10

This map decides ownership before further code is moved or deleted. The first
stable split now exists; the remaining labels describe the target boundary.

## Minimal core

| Current location | Keep | Target responsibility | Next structural action |
|---|---|---|---|
| `agent_recipes/ledger.py` and the `RecipesProject` compatibility methods | yes | append-only ledger, idempotency, file locking, chain inspection, corrupted-ledger write stop | extracted and verified |
| `capture`, `compile`, review decisions | yes | candidate/formal isolation and human promotion | extract to `governance.py` |
| `agent_recipes/execution.py` plus effectful lock writes in `core.py` | yes | strict applicability, no-match, exact execution lock, expiry validation, stale-lock retirement | policy extracted and verified; event/lock writes remain in core |
| `agent_recipes/lifecycle.py` plus lifecycle command orchestration in `core.py` | yes | tombstone state, revocation, anti-resurrection, active-version selection | policy extracted and verified; command side effects remain in core |
| `agent_recipes/outcome.py` plus capture/lock orchestration in `core.py` | yes | exact lock bindings, success/failure/unknown attribution, confidence, degradation and hold policy | policy extracted and verified; event/lock writes remain in core |
| focused `doctor` checks | yes | ledger, lifecycle, lock, source-reference health | keep a small core doctor; adapter checks move outward |
| `agent_recipes/cli.py` | yes | thin adapter over core commands | split optional command registration from core commands |
| `agent_recipes/mcp.py` | yes | thin MCP adapter over the same core contract | publish a minimal tool profile plus optional profiles |
| `agent_recipes/persistence.py` | yes | errors, hashing, redaction, atomic JSON/JSONL persistence | extracted and verified |
| `agent_recipes/migration.py` | yes | project schema status and explicit fail-closed migration | extracted and verified |

## Distribution shell

| Current location | Keep | Boundary |
|---|---|---|
| `agent_recipes/install.py` | yes | installs local skill/MCP wiring; must not become domain logic |
| `agent_recipes/client_config.py` | yes | client-specific configuration and smoke only |
| `agent_recipes/release.py` | yes | package/audit/export; cannot claim real client or product quality |
| `agent_recipes/dependencies.py` | split | minimal core runtime report separate from optional adapter lock |
| `bin/agent-recipes` | yes | stable executable entrypoint |

## Optional evidence adapters

These capabilities stay available but cannot be required by the minimal core
and cannot write formal recipes directly.

| Current `core.py` area | Target adapter |
|---|---|
| `convert_doc`, `detect_scenes`, `transcribe`, `ocr_image` | `adapters/source_conversion.py` |
| `memory_*` for Cognee and Graphiti | `adapters/memory.py` |
| `cloud_*` for DeepSeek | `adapters/cloud_llm.py` |
| `embedding_*` for Qwen | `adapters/embedding.py` |
| source refinery and knowledge fusion | `adapters/source_refinery.py` |
| course skill draft and completeness audit | `experiments/course_skill.py` |

Adapter outputs remain candidate evidence. Disabling every adapter must leave
capture, review, lookup, lock, lifecycle, outcome capture, and doctor usable.

## Development and benchmark tools

| Current area | Target | Runtime status |
|---|---|---|
| lookup/lock/real pressure reports | `benchmarks/consumption.py` | development only |
| repeat-error and output-quality benchmark | `benchmarks/outcomes.py` | development only |
| quality benchmark and duplicate governance | `benchmarks/quality.py` | development only |
| `tests/test_phase6_*` through `test_phase12_*` | optional/benchmark suites | not part of minimal smoke |
| `tests/test_phase0a.py`, `0b.py`, `0c.py`, `1.py`, `13_lifecycle.py` | core contract suite | required minimal CI |

## Local references

| Path | Status |
|---|---|
| `references/upstream/remnic` | ignored, pinned, read-only architecture reference; never packaged |
| `references/UPSTREAM_SOURCES.md` | tracked source and license manifest |
| `THIRD_PARTY_NOTICES.md` | tracked attribution for adapted MIT concepts |
| Cass checkout | forbidden without written permission; intentionally absent |

## Next files to move

`agent_recipes/core.py` is 15,610 lines after extracting persistence, migration,
ledger, lifecycle, outcome, and execution policy. The next structural work must
serve an open quality gate, not continue splitting merely to lower a line count:

1. establish the large-sample real-source learning benchmark;
2. extract recall/evidence reporting only when that benchmark needs a stable boundary;
3. separate optional adapter command registration from minimal CLI/MCP when distribution work resumes;
4. run minimal and full suites after every extraction;
5. keep optional adapter internals outside the minimal governance core.

## Definition of a successful shrink

- minimal install uses only the Python standard library;
- core tests run without Cognee, Graphiti, DeepSeek, Qwen, OCR, or ASR;
- core CLI/MCP expose the same governance contract;
- optional adapters can fail or be absent without breaking doctor core status;
- formal recipes still require review and inactive recipes cannot be looked up
  or locked;
- no current feature is claimed deleted or migrated until its import path and
  tests prove the move.
