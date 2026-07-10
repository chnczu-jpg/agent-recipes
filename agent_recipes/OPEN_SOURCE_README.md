# Agent Recipes

**Stop teaching your agents the same lesson twice.**

[![CI](https://github.com/chnczu-jpg/agent-recipes/actions/workflows/ci.yml/badge.svg)](https://github.com/chnczu-jpg/agent-recipes/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/interface-MCP-111111)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Agent Recipes is a governed procedural-memory layer for AI agents. It turns
corrections, source material, and proven workflows into review-gated, versioned
recipes that agents can retrieve, lock, execute, and improve without silently
rewriting the truth.

It is not another vector database, prompt folder, or RAG notebook. It is the
missing control layer between **"the agent once learned this"** and **"the next
agent can safely do it again."**

## The problem

Most agent teams are paying an invisible tax every day:

- Corrections disappear with the session, so the same expensive mistake returns.
- Prompt files become junk drawers where outdated and conflicting advice looks equally valid.
- Retrieval systems surface plausible text, but cannot prove it applies to the current task.
- Automatic memory writes can promote one bad guess into a permanent team-wide failure.
- Courses, documents, and successful outputs remain passive knowledge instead of executable experience.
- Teams can count stored memories, but cannot show that agents actually make fewer mistakes.

Agent Recipes is designed to break that loop.

## What changes

```text
correction / course / document / proven run
                    |
                    v
          source-traced candidates
                    |
             human review gate
                    |
                    v
             versioned recipe
                    |
        strict lookup -> no-match or lock
                    |
                    v
      execution evidence -> outcome -> improvement
```

- **Candidate before truth.** Extracted knowledge cannot become a formal recipe by accident.
- **No-match is a feature.** Weak retrieval is rejected instead of dressed up as confidence.
- **Execution is pinned.** Locks bind recipe id, version, hash, claim, and expiry.
- **History stays auditable.** Events are append-only, idempotent, integrity-checked, and fail closed.
- **Outcomes are attributable.** Success and failure bind to the exact locked recipe snapshot.
- **Claims have boundaries.** Source traces and `cannot_claim` travel with candidate evidence.
- **Quality is measurable.** Recall, repeat-error, candidate quality, and output quality have executable gates.

## Why not just use memory or RAG?

| Capability | Prompt file | Vector memory / RAG | Agent Recipes |
|---|---:|---:|---:|
| Finds relevant text | Manual | Yes | Yes |
| Rejects weak matches | Rarely | Usually threshold-only | Strict no-match policy |
| Separates candidate from formal truth | No | Usually no | Yes |
| Requires review before promotion | No | Optional | Enforced |
| Pins execution to an exact version/hash | No | No | Yes |
| Attributes outcomes to what was used | No | Rarely | Yes |
| Prevents silent history rewrites | No | Backend-dependent | Append-only ledger |
| Exposes claim limits to the agent | No | No | Yes |

Use your existing memory or RAG stack for broad recall. Use Agent Recipes to
decide what is trusted enough to act on.

## Verified local baseline

Release line `0.2.x` currently has:

- 261 unit and integration tests passing locally on Python 3.11 and 3.13.
- Clean wheel installs verified on Python 3.11, 3.13, and 3.14.
- An approximately 210 KB wheel with zero required runtime dependencies or external services.
- A fixed 111-unique-case local recall gate across core, projection, and optional embedding paths.
- A tested `0.1.0 -> 0.1.1 -> 0.1.0 -> 0.1.1` upgrade/rollback cycle with event-ledger bytes preserved.

These are bounded engineering results, not a claim that every domain, model,
memory backend, or production environment is already solved.

## Quick start

```bash
git clone https://github.com/chnczu-jpg/agent-recipes.git
cd agent-recipes
python3 -m pip install .

agent-recipes init --project . --json
agent-recipes capture \
  --type correction \
  --task "release checklist" \
  --text "Never claim a release is complete until hosted CI is green." \
  --project . --json
agent-recipes compile --project . --json
agent-recipes doctor --project . --json
```

`compile` produces candidates. Review a returned candidate id before it can
become formal:

```bash
agent-recipes review --accept <candidate-id> --project . --json
agent-recipes lookup "prepare a release" --strict --project . --json
agent-recipes lock --recipe <recipe-id> --task "publish v0.2.0" --project . --json
```

## Agent integration

Install the project skill and MCP entrypoint:

```bash
agent-recipes install-skill --agent codex --scope project --project . --json
agent-recipes install-client --agent codex --project . --json
```

Client installers are also available for Claude and Hermes:

```bash
agent-recipes install-client --agent claude --project . --json
agent-recipes install-client --agent hermes --project . --json
claude mcp list
hermes mcp test agent_recipes
```

User configurations are written to `~/.codex/config.toml`, `~/.claude.json`,
or `~/.hermes/config.yaml`. Project data remains under `.recipes/`.

## Optional intelligence, governed by the same gate

The core runs without external services. Optional adapters can improve candidate
generation or recall, but none can bypass review or write formal recipes:

- **Qwen3 Embedding** for loopback-only local semantic recall.
- **DeepSeek** for cloud-assisted text refinement through environment-held credentials.
- **Cognee** for candidate memory retrieval.
- **Graphiti** for candidate relationship and correction-path retrieval.

Optional adapter output remains candidate evidence. Availability is not truth,
and a successful model call is not a successful recipe.

## Operations

Cause-specific feedback keeps agent mistakes from poisoning recipe quality:

```bash
agent-recipes capture --type failure \
  --feedback-kind execution_error \
  --text "The agent clicked the wrong control." \
  --lock <lock-id> --project . --json

agent-recipes capture --type failure \
  --feedback-kind recipe_incorrect \
  --text "Step three contradicts the current software version." \
  --lock <lock-id> --project . --json
```

Retrieval, execution, dependency, recipe, cost, conflict, evidence, and user
correction feedback remain bound to the exact recipe snapshot. Non-recipe
failures do not degrade the recipe. No feedback automatically edits or promotes
a formal recipe.

```bash
agent-recipes migration-status --project . --json
agent-recipes readiness --project . --json
agent-recipes outcome-status --project . --json
agent-recipes recall-boundary --project . --json
agent-recipes evidence-quarantine --action status --project . --json
agent-recipes doctor --project . --json
```

For legacy projects, inspect status first and migrate explicitly:

```bash
agent-recipes migrate --target 1.0 --project . --json
```

Migration does not rewrite historical event rows.

## Safety model

Agent Recipes deliberately chooses refusal over invented certainty:

- malformed, corrupted, future-version, and unsupported state fails closed;
- expired or mismatched locks cannot be used;
- retired recipe ids cannot silently resurrect;
- candidate memory cannot directly overwrite formal recipes;
- credentials and private runtime state are excluded from the public export path.

Run the release checks yourself:

```bash
python3 -m unittest discover -s tests
python3 -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist/wheels
./bin/verify-clean-install dist/wheels/agent_recipes_local-*.whl
./bin/agent-recipes open-source-audit --project . --json
```

## Status

Agent Recipes is an early public release with a deliberately narrow promise:
make high-value agent experience reviewable, reusable, enforceable, and
measurable. It does not claim to be a universal long-term memory system or an
autonomous replacement for human judgment.

See [AGENT_RECIPES_PLAN.md](AGENT_RECIPES_PLAN.md) for the system plan and
[ENGINEERING_MATURITY.md](ENGINEERING_MATURITY.md) for evidence and remaining gaps.

## License

MIT. See [LICENSE](LICENSE). Third-party attribution is recorded in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
