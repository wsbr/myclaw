# MyClaw roadmap

This roadmap records the dependency-ordered capability delivery from `v0.1.1` through `v0.3.0`. A capability is released only when its implementation, migration, API/UI behavior, tests, documentation, and release evidence agree.

## Current baseline

| Capability | Status | Release |
| --- | --- | --- |
| Group diagnostics and loop guardrails | Delivered | `v0.1.2` |
| Runtime management console | Delivered | `v0.1.2` |
| Usage and cost ledger | Delivered | `v0.2.0` |
| Agent evaluation and version gates | Delivered | `v0.2.0` |
| Minimum Tenant boundary | Delivered | `v0.3.0` |
| Local knowledge base | Delivered | `v0.3.0` |

The milestones remain intentionally bounded. The ledger is not customer billing, an Eval gate does not prove general model quality, the local Tenant boundary is not a production SaaS identity plane, and Memory is not a document knowledge base.

## Dependency order

```text
Group diagnostic facts ──> Runtime console
          │                       │
          └──────────────> Agent Eval evidence

Usage ledger ────────────> cost budgets + Eval cost gates + console rollups

Minimum Tenant boundary ─> local knowledge ownership and retrieval isolation
```

## v0.1.2 — Group safety and Runtime visibility

### Group diagnostics and loop guardrails

The guardrail layer must remain mechanical and auditable. It may bound or reject actions from persisted causal facts; it must not turn role names, free-text keywords, or an inferred fixed graph into a hidden workflow.

Delivered:

- persist a stable diagnostic code, causal chain, triggering limits and safe human-readable explanation for every blocked delegation or forced stop;
- retain the existing self/cycle/depth checks and add bounded fan-out, repeated delegation fingerprint and A↔B ping-pong detection within one Run;
- distinguish structural repetition from semantic suspicion: exact causal/action repetition can block, while uncertain semantic similarity only warns and contributes to no-progress handling;
- warn the active Agent when a soft threshold is reached, then stop new delegation at the hard threshold and require a bounded summary from existing facts;
- explain orphan handoffs, recipient unavailable, undelivered results, stalled SharedTasks, summary wake retries and quiescence decisions;
- keep directed Group semantics intact: a direct request/delivery wakes only its recipient, while a Group note remains non-notifying searchable history;
- project the interaction graph only from committed messages and invocations, never from the intended plan or selected member list.

Verification gate:

- deterministic tests cover self-call, A→B→A repetition, A↔B ping-pong, fan-out exhaustion, no-progress escalation, duplicate delivery and duplicate summary wake;
- every rejected or stopped action has one durable reason and traceable causal source;
- retry, Worker restart and duplicate Outbox delivery cannot bypass a guard or create a second business action;
- a healthy multi-hop Group remains possible within budget and is not mistaken for a loop;
- real-model UAT demonstrates both one healthy collaboration and one safely bounded pathological collaboration.

### Runtime management console

The console is an operational projection over authoritative Runtime records. It must never edit database status directly or invent a second lifecycle.

Delivered:

- add global views for Runs, WorkItems, Runners, ProviderAttempts, ToolInvocations, AgentInvocations and Outbox backlog;
- expose status, age, current wait reason, lease/epoch, attempt count, budget use, last progress fact and terminal reason;
- add one Run detail timeline combining causal messages, Agent turns, Tool/Agent calls, checkpoints, diagnostics and Group interaction edges;
- expose Worker heartbeat/slot health, oldest queued/outbox age and dead/unknown outcomes;
- make the default console read-only; cancellation and retry use the existing governed APIs with idempotency, authorization and PlatformAuditEvent;
- redact credentials, private prompts, raw Chain-of-Thought and cross-principal content from all diagnostics.

Verification gate:

- an operator can answer “what is running, why is it waiting, who owns the next action, what budget remains, and why did it stop” without reading the database;
- refresh, reconnect and pagination do not duplicate or reorder authoritative events;
- operator actions are scoped, idempotent and auditable; arbitrary status mutation is impossible.

## v0.2.0 — Quality and economics

### Usage and cost ledger

The ledger measures actual platform consumption and estimated/contracted cost. It is not customer billing and must not silently treat unknown pricing as zero.

Delivered:

- introduce append-only usage entries attributed to Tenant/account, Session, Run, GroupRun, AgentVersion, Turn/Attempt and Tool/Sandbox operation where applicable;
- record normalized dimensions such as input/output/cached tokens, requests, active milliseconds, Tool units, Sandbox time, storage and egress;
- version price snapshots by Provider/model/Tool, currency, unit, effective window and source; bind every calculated amount to the exact snapshot used;
- make retries, failed attempts and unknown outcomes visible rather than charging only successful final answers;
- use stable source-operation keys so replay cannot double count; corrections append adjustment entries instead of rewriting history;
- provide Run, Agent, model, Group and time-window rollups plus budget warnings/hard stops;
- separate `measured`, `estimated`, `contracted` and `unknown` amounts in APIs and UI.

Verification gate:

- the sum of source entries reconciles exactly with Run and aggregate views;
- duplicate events, retry and Worker restart cannot duplicate usage or cost;
- missing or stale prices remain explicitly unknown;
- Group totals include all members, delegation, summaries, retries and Tool/Sandbox use;
- budget enforcement reads the same authoritative ledger exposed to the user.

### Agent evaluation and version gates

Provider Conformance answers whether a model adapter follows the protocol. Agent Eval answers whether one exact AgentVersion is safe and effective for its intended work. These remain separate gates.

Delivered:

- add versioned EvalSuite, EvalCase, EvalRun and EvalResult records with immutable inputs, fixtures, rubric versions and environment fingerprints;
- run deterministic contract/security cases separately from real-model quality cases;
- bind results to the exact candidate Agent content hash, ModelVersion, Tool/Skill/Policy/Sandbox versions and Eval suite revision;
- compare the candidate with the current published baseline for outcome quality, verified evidence, latency, cost, intervention, failure and safety metrics;
- let Agent publication require configured suites to pass within thresholds; any changed dependency invalidates stale evidence;
- allow audited override only for explicitly overridable gates; isolation, authorization and secret-safety failures cannot be bypassed;
- retain all results as release evidence and show failures before the Builder can publish.

Verification gate:

- a Draft can be tested repeatedly without becoming a published version;
- publication cannot reuse results from another content hash or stale dependency set;
- deterministic critical cases must all pass; stochastic suites use declared sample size and thresholds;
- candidate-versus-baseline regression, latency and cost are visible before publication;
- a published version links back to the exact passing gate evidence.

## v0.3.0 — Data boundary and local knowledge

### Minimum Tenant boundary

This milestone establishes an enforceable data boundary for one or more locally administered Tenants. It does not claim multi-tenant SaaS operations, enterprise OIDC, billing, SCIM or tenant self-service.

Delivered:

- introduce Tenant as a persisted authority and bootstrap one default Tenant for existing installations;
- carry immutable `tenant_id + account_id` through AuthContext, Session creation, Run, AgentInvocation, Tool execution and audit;
- add tenant ownership to aggregate roots including accounts, Agents/registries, Sessions, Groups, Memory, Workspace and Artifacts;
- use tenant-safe composite uniqueness/FKs or equally strong repository enforcement for cross-aggregate references;
- require tenant scope in business queries and commands; remove global naked-ID lookup paths from user-facing APIs;
- migrate by expand → backfill → validate → enforce, preserving existing local data under the default Tenant;
- add two synthetic Tenant fixtures and negative tests across every ownership boundary.

Verification gate:

- the second Tenant cannot enumerate, read, mutate, invoke, retrieve or receive events from the first Tenant;
- guessed IDs, cursors, Artifact references, Group messages, Memory and knowledge searches all fail closed across Tenant boundaries;
- background workers retain the original Tenant/account authority and cannot fall back to a default after dispatch;
- existing single-Tenant installations upgrade without manual data rewriting.

### Local knowledge base

Knowledge is imported document evidence; Memory is a curated fact about a user or Agent. They must use separate models, policies and deletion semantics.

Delivered:

- add KnowledgeBase, source/document, chunk and immutable IndexRevision concepts scoped to Tenant and an explicit owner/sharing policy;
- support local UTF-8 text, Markdown and PDF ingestion first, with checksum deduplication, bounded parsing and failure evidence;
- store source bytes under a dedicated checksum-addressed local knowledge root and keep index metadata in the authoritative database;
- version chunking and embedding configuration; provide a local embedding/vector profile and lexical fallback without sending documents to an external service by default;
- return hybrid retrieval results with source, page/section, checksum, score and index revision so the Agent can cite what it used;
- snapshot retrieved chunk references into the Runner for reproducibility and Eval;
- support re-index, supersession, tombstone/delete and orphaned-body reconciliation;
- require explicit Agent/Group access; importing a document never makes it visible to every Agent.

Verification gate:

- identical source/checksum ingestion is idempotent, while changed content creates a new revision;
- retrieval citations resolve to the exact indexed source bytes and revision;
- deletion prevents future retrieval without rewriting historical Runner evidence;
- cross-account, cross-Agent private and cross-Tenant retrieval tests fail closed;
- the first release works entirely on the local Compose profile without mandatory cloud storage or embedding services.

## Common definition of done

Every roadmap capability must include:

- schema migration and rollback/compatibility strategy;
- authoritative domain model, idempotent writes and append-only evidence where history matters;
- scoped API and usable UI, not only database tables;
- deterministic unit/integration tests plus controlled real-model UAT where behavior depends on a model;
- observability, failure/recovery behavior, security negative tests and public documentation;
- clean `make release-check` and GitHub CI bound to the release commit.

## Deliberately deferred

Until these milestones are proven, the primary roadmap does not include Kubernetes packaging, multi-AZ deployment, production OIDC/OBO/SCIM, customer billing, a plugin marketplace, federated cross-organization Agents or production microVM Sandbox claims. Those remain target-architecture work and must not displace the six capabilities above.
