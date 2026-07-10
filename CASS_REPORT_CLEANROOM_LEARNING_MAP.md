# CASS Report Clean-room Learning Map

Date: 2026-07-10

## Source and boundary

- User-provided report: `<home>/Desktop/深度系统拆解报告.docx`
- SHA-256: `fb5d994ca32812cf7b96d94f98ae6769c6256577c258842cac04669113e009df`
- Report subject: `Dicklesworthstone/coding_agent_session_search`
- This is not the previously audited `cass_memory_system` repository.
- The report contains 339 paragraphs, 16 tables, and 15 rendered A4 pages.
- All 15 pages were rendered and inspected; no layout clipping or missing table
  relationships affected this review.
- The subject repository carries an OpenAI/Anthropic restriction. Agent Recipes
  will not download or inspect its source. This document converts only general
  product and reliability principles into independently designed requirements.

## Adopt

1. **Authoritative truth vs derived assets**
   - Agent Recipes truth is the append-only event ledger plus formal recipe
     files whose hashes are checked against the ledger.
   - Search indexes, embeddings, memory graphs, reports, and caches are derived
     assets. They must be rebuildable and must never become formal truth.

2. **Multi-axis readiness**
   - Health is not one boolean.
   - Ledger, lifecycle, formal recipes, review queue, optional adapters, and
     real client exposure need separate states and a recommended next action.

3. **Machine-first response contract**
   - CLI and MCP must expose stable JSON shapes, fixed error envelopes, claim
     boundaries, and commands an Agent can execute next.
   - Golden contract tests should fail when a public response shape changes
     silently.

4. **Quarantine instead of silent deletion**
   - Malformed or suspicious candidates should remain inspectable, with reason,
     attempt count, source reference, and explicit release/reject decisions.

5. **Serving degrades; mutation fails closed**
   - Missing optional retrieval quality may fall back with disclosure.
   - Formal recipe writes, lifecycle changes, and outcome attribution must stop
     when required evidence or locks are missing.

6. **Budgeted evidence packs**
   - Agent context should have token/item limits, source freshness, omission
     reasons, privacy state, and claim limits rather than dumping every match.

7. **Security before persistence**
   - Secrets should be detected before capture/index/export where practical.
   - Redaction must report that it occurred without storing the removed secret.

## Reject

- Do not rebuild a general multi-agent session search engine inside Agent Recipes.
- Do not add Tantivy, a vector daemon, a TUI, SSH synchronization, encrypted
  publishing, or an analytics warehouse to the minimal core.
- Do not use a fake hash embedding while calling the result semantic search.
- Do not copy the subject repository's source structure, schemas, field names,
  algorithms, or implementation details.
- Do not repeat its monolithic-file and feature-sprawl problems.

## Modify for Agent Recipes

| Report principle | Agent Recipes interpretation |
|---|---|
| SQLite is source of truth | hash-chained event ledger is authority; formal files are verified projections |
| lexical required, semantic optional | deterministic strict lookup required; embeddings/memory are optional candidate enrichments |
| robot readiness | governance readiness: ledger, lifecycle, recipes, review, adapters, client evidence |
| pack planner | execution evidence pack with source trace, claim limits, omissions, and budget |
| quarantine bad index assets | quarantine malformed candidates/evidence; never silently delete or promote |
| generation publish | formal recipe version/hash plus review/lock/tombstone lifecycle |
| agent triage next command | readiness returns stable recommended actions with blocking priority |

## New opportunities to surpass

The report's system can tell an Agent whether search infrastructure is ready.
Agent Recipes should additionally tell the Agent whether a rule is **allowed to
control execution**. That requires guarantees absent from a search platform:

- candidate/formal isolation;
- human promotion;
- strict no-match;
- exact recipe version/hash execution lock;
- lock-bound outcome attribution;
- tombstone and anti-resurrection;
- claim boundaries attached to every result.

The combined target is therefore not "more search". It is:

> trustworthy retrieval plus governed execution.

## Delivery order

1. Multi-axis readiness and recommended actions. Implemented.
2. Golden CLI/MCP response contracts. Implemented.
3. Stage B attributable outcome quality and confidence. Implemented.
4. Candidate/evidence quarantine. Implemented.
5. Budgeted execution evidence pack. Implemented.
6. Pre-persistence secret redaction. Implemented.
7. Physical module split and CI/reproduction maturity.

No item counts as complete until core, CLI, MCP, doctor/readiness, and failure
tests agree on the same contract.
