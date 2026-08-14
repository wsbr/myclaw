# MyClaw roadmap

This roadmap records the dependency-ordered capability delivery from `v0.1.1` through `v0.3.1`. A capability is released only when its implementation, migration, API/UI behavior, tests, documentation, and release evidence agree.

## Current baseline

| Capability | Status | Release |
| --- | --- | --- |
| Group diagnostics and loop guardrails | Core delivered | `v0.1.2` |
| Runtime management console | Core delivered | `v0.1.2` + `v0.3.1` hardening |
| Usage and cost ledger | Core delivered | `v0.2.0` |
| Agent evaluation and version gates | Core delivered | `v0.2.0` |
| Minimum Tenant boundary | Core delivered | `v0.3.0` |
| Managed knowledge base | Core delivered | `v0.3.0` + `v0.3.1` hardening |

The milestones remain intentionally bounded. The ledger is not customer billing, an Eval gate does not prove general model quality, the local Tenant boundary is not a production SaaS identity plane, and Memory is not a document knowledge base.

`Core delivered` means the named user-facing capability, its authoritative
records and its local/UAT safety boundary are implemented. It does not silently
promote the hardening items listed below to delivered status. This distinction
was added by the post-`v0.3.0` completion audit so the roadmap remains a testable
contract rather than an aspirational feature list.

## Dependency order

```text
Group diagnostic facts ──> Runtime console
          │                       │
          └──────────────> Agent Eval evidence

Usage ledger ────────────> cost budgets + Eval cost gates + console rollups

Minimum Tenant boundary ─> managed knowledge ownership and retrieval isolation
```

## v0.1.2 — Group safety and Runtime visibility

### Group diagnostics and loop guardrails

The guardrail layer must remain mechanical and auditable. It may bound or reject actions from persisted causal facts; it must not turn role names, free-text keywords, or an inferred fixed graph into a hidden workflow.

Core delivered:

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

Core delivered:

- add global views for Runs, WorkItems, Runners, ProviderAttempts, ToolInvocations, AgentInvocations and Outbox backlog;
- expose status, age, current wait reason, lease/epoch, attempt count, budget use, last progress fact and terminal reason;
- add one Run detail timeline combining causal messages, Agent turns, Tool/Agent calls, checkpoints, diagnostics and Group interaction edges;
- expose Worker heartbeat/slot health, oldest queued/outbox age and dead/unknown outcomes;
- make the default console read-only; cancellation and retry use the existing governed APIs with idempotency, authorization and PlatformAuditEvent;
- redact credentials, private prompts, raw Chain-of-Thought and cross-principal content from all diagnostics.

Verified core gate:

- an operator can answer “what is running, why is it waiting, who owns the next action, what budget remains, and why did it stop” without reading the database;
- refresh, reconnect and pagination do not duplicate or reorder authoritative events;
- operator actions are scoped, idempotent and auditable; arbitrary status mutation is impossible.

## v0.2.0 — Quality and economics

### Usage and cost ledger

The ledger measures actual platform consumption and estimated/contracted cost. It is not customer billing and must not silently treat unknown pricing as zero.

Core delivered:

- introduce append-only usage entries attributed to Tenant/account, Session, Run, GroupRun, AgentVersion, Turn/Attempt and Tool/Sandbox operation where applicable;
- record produced normalized dimensions for input/output/cached tokens, requests,
  active milliseconds, Tool units and Sandbox time;
- version price snapshots by Provider/model/Tool, currency, unit, effective window and source; bind every calculated amount to the exact snapshot used;
- make retries, failed attempts and unknown outcomes visible rather than charging only successful final answers;
- use stable source-operation keys so replay cannot double count; corrections append adjustment entries instead of rewriting history;
- provide Run, Agent, model, Group and time-window rollups;
- separate `measured`, `estimated`, `contracted` and `unknown` amounts in APIs and UI.

Verified core gate:

- the sum of source entries reconciles exactly with Run and aggregate views;
- duplicate events, retry and Worker restart cannot duplicate usage or cost;
- missing or stale prices remain explicitly unknown;
- Group totals include all members, delegation, summaries, retries and Tool/Sandbox use;

Hardening queued:

- wire storage and egress observations into first-class producers rather than
  only reserving normalized ledger dimensions;
- implement cost warning and hard-stop policies that read the same authoritative
  ledger exposed to the user.

### Agent evaluation and version gates

Provider Conformance answers whether a model adapter follows the protocol. Agent Eval answers whether one exact AgentVersion is safe and effective for its intended work. These remain separate gates.

Core delivered:

- add versioned EvalSuite, EvalCase, EvalRun and case-result records with immutable inputs, assertions, weights and suite thresholds;
- run every case as an isolated real Draft Test Run, then settle deterministic text, length and maximum-cost assertions from authoritative results;
- bind results to the exact candidate Agent content hash, ModelVersion, Tool/Skill/Policy/Sandbox versions and Eval suite revision;
- let Agent publication require configured suites to pass within thresholds; any changed dependency invalidates stale evidence;
- retain all results as release evidence and show failures before the Builder can publish.

Verified core gate:

- a Draft can be tested repeatedly without becoming a published version;
- publication cannot reuse results from another content hash or stale dependency set;
- a published version links back to the exact passing gate evidence.

Hardening queued:

- separate deterministic contract/security suites from declared stochastic
  real-model suites with sample size, rubric/environment fingerprints and
  explicit critical-case semantics;
- add candidate-versus-published-baseline quality, evidence, latency, cost,
  intervention, failure and safety comparisons;
- add narrowly scoped audited overrides for overridable gates while keeping
  isolation, authorization and secret-safety failures non-bypassable.

## v0.3.0 — Data boundary and local knowledge

### Minimum Tenant boundary

This milestone establishes an enforceable data boundary for one or more locally administered Tenants. It does not claim multi-tenant SaaS operations, enterprise OIDC, billing, SCIM or tenant self-service.

Core delivered:

- introduce Tenant as a persisted authority and bootstrap one default Tenant for existing installations;
- carry immutable `tenant_id + account_id` through AuthContext, Session creation, Run, AgentInvocation, Tool execution and audit;
- add tenant ownership to accounts, user Agents, Sessions, Groups, Memory,
  Workspace, Artifacts and execution/evidence records; deployment-level Model,
  Tool, Skill, Policy and Sandbox registries remain intentionally global;
- use tenant-safe composite uniqueness/FKs or equally strong repository enforcement for cross-aggregate references;
- require tenant scope in business queries and commands; remove global naked-ID lookup paths from user-facing APIs;
- migrate by expand → backfill → validate → enforce, preserving existing local data under the default Tenant;
- add two synthetic Tenant fixtures and negative tests across the core ownership boundaries.

Verification gate:

- the second Tenant cannot enumerate, read, mutate, invoke, retrieve or receive events from the first Tenant;
- guessed IDs, cursors, Artifact references, Group messages, Memory and knowledge searches all fail closed across Tenant boundaries;
- background workers retain the original Tenant/account authority and cannot fall back to a default after dispatch;
- existing single-Tenant installations upgrade without manual data rewriting.

### Local knowledge base

Knowledge is imported document evidence; Memory is a curated fact about a user or Agent. They must use separate models, policies and deletion semantics.

Core delivered:

- add KnowledgeBase, source/document, chunk and immutable IndexRevision concepts scoped to Tenant and an explicit owner/sharing policy;
- support local UTF-8 text, Markdown and PDF ingestion first, with checksum deduplication, bounded parsing and failure evidence;
- store source bytes under a dedicated checksum-addressed local knowledge root and keep index metadata in the authoritative database;
- version chunking and embedding configuration; provide a local embedding/vector profile and lexical fallback without sending documents to an external service by default;
- return hybrid retrieval results with source, page/section, checksum, score and index revision so the Agent can cite what it used;
- snapshot retrieved chunk references into the Runner for reproducibility and Eval;
- support re-index, supersession, tombstone/delete and orphaned-body reconciliation;
- require explicit access for every Agent, including each Agent participating in
  a Group; a Group never receives ambient access and importing a document never
  makes it visible to every Agent.

Verification gate:

- identical source/checksum ingestion is idempotent, while changed content creates a new revision;
- retrieval citations resolve to the exact indexed source bytes and revision;
- deletion prevents future retrieval without rewriting historical Runner evidence;
- cross-account, cross-Agent private and cross-Tenant retrieval tests fail closed;
- the first release works entirely on the local Compose profile without mandatory cloud storage or embedding services.

## v0.4.0 — Semantic Embedding and vector retrieval

This milestone advances Knowledge from the compatibility feature-hash profile
to a real semantic retrieval path without turning the platform into a local
directory crawler.

Core delivered:

- register Chat and Embedding as distinct immutable ModelVersions while
  allowing the same Provider credential to be stored for both;
- validate and execute an OpenAI-compatible Embedding Conformance suite, with
  a Bailian `text-embedding-v4` / 1024-dimensional operator preset;
- add Qdrant as a dedicated persistent vector projection with Tenant and exact
  index-revision payload filters;
- enqueue semantic indexing through the transactional Outbox and execute it in
  a bounded `knowledge-worker` pool with leases, heartbeats, fencing,
  deterministic point IDs and retry/backoff;
- keep Provider and Qdrant calls outside long database transactions and switch
  the current revision only after the complete projection is acknowledged;
- provide hybrid semantic/lexical retrieval, exact citations and Runner
  snapshots while keeping legacy revisions readable;
- expose model selection, current/pending revision state, vector point counts
  and failure evidence in the Knowledge console;
- accept only explicit UI/API uploads; no filesystem watcher, local directory
  scan or scheduled directory synchronization is introduced.

Verification gate:

- schema, adapter and durable worker tests cover dimensions, Provider wire
  shape, Tenant filters, idempotent indexing and publish-after-vector ordering;
- clean MySQL migration reaches `0041`, all Compose profiles parse, and the
  frontend builds;
- local UAT uses the configured real Bailian credential to pass Embedding
  Conformance, index a document into Qdrant and retrieve a semantically related
  query with revision/checksum citations.

## v0.4.1 — Three-route hybrid retrieval

Core delivered:

- project independent named Dense, sparse BM25 with revision-frozen IDF, and conditional
  domain-lexicon vectors instead of refining only Dense candidates;
- freeze tokenizer/BM25 parameters, lexicon identity and RRF parameters into
  every immutable IndexRevision;
- publish owner-managed immutable LexiconRevisions and rebuild the complete
  projection before advancing the current index pointer;
- fuse independent route ranks with deterministic RRF and retain per-route
  rank/raw score plus matched canonical-term evidence;
- degrade an unavailable query Embedding route to BM25/Lexicon with explicit
  evidence while keeping Qdrant authority/projection errors fail-closed;
- keep legacy local and dense-only Qdrant revisions readable.

Verification gate:

- deterministic tests cover Unicode/CJK tokenization, sparse vectors, alias
  ambiguity, Qdrant named-vector wire shape, RRF evidence, publish ordering and
  Dense degradation;
- clean MySQL migration reaches `0042`, frontend/API contracts build, and real
  UAT independently proves Dense, exact-code BM25 and cross-language alias
  retrieval against one immutable revision.

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
