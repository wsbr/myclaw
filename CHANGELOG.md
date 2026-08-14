# Changelog

All notable changes to MyClaw are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use semantic versioning.

## [Unreleased]

## [0.4.0] - 2026-08-15

### Added

- Added first-class Embedding ModelVersions with a separate OpenAI-compatible
  Conformance suite, frozen dimensions and explicit rejection from Agent chat
  assemblies.
- Added Qdrant-backed semantic vector projection, Tenant/revision payload
  filters and hybrid semantic/lexical retrieval with exact citations.
- Added a durable bounded Knowledge indexing worker with transactional Outbox
  dispatch, leases, heartbeats, fencing, deterministic idempotent point IDs,
  bounded Provider batches and retry/terminal failure evidence.
- Added Knowledge UI controls for Embedding model selection, current/pending
  index state, Qdrant point counts and semantic result evidence.

### Changed

- Moved Knowledge Embedding and vector operations outside database
  transactions; the previous ready revision remains current until the complete
  replacement projection is committed.
- Retained legacy feature-hash revisions for compatibility while making the
  semantic profile available per KnowledgeBase.

### Release scope

- Explicit UI/API ingestion only; MyClaw does not scan or synchronize local
  user directories. OCR and production multi-region Qdrant operation remain
  outside this release.

## [0.3.1] - 2026-08-14

### Added

- Added persisted process-level worker registration with idle heartbeats,
  release identity, bounded capacity/active slots, stale/stopped/unknown health,
  and lease cross-checks in the Runtime Console.
- Added redacted Runtime next-action, queued-age, committed-message, AgentTurn,
  Checkpoint and actual Group-edge projections without exposing message bodies,
  prompts, Tool payloads or Chain-of-Thought.
- Added local Knowledge storage reconciliation for missing, corrupt and
  unreferenced source bodies, fail-closed retrieval, repairable current-source
  availability and an operator-facing reconciliation report.

### Changed

- Tightened the roadmap after a completion audit: shipped capability cores are
  separated from queued Usage budget enforcement and advanced Eval regression,
  stochastic-suite and override work.

## [0.3.0] - 2026-08-14

### Added

- Defined the post-`v0.1.1` roadmap for Group diagnostics and loop guardrails, the usage/cost ledger, Agent evaluation and version gates, the Runtime management console, a minimum Tenant boundary, and a local knowledge base.
- Added append-only Group diagnostics for rejected delegations, including causal Agent chains, stable reason codes, limit snapshots, idempotent fingerprints, and a user-facing GroupRun diagnostic view.
- Added hard Group fan-out and repeated-delegation guards while preserving healthy asynchronous multi-hop handoffs and distinguishing contained guard actions from business execution failures.
- Added an administrator-only, read-only Runtime Console for global Runs, budget/progress projections, WorkItems, Runners, Provider/Tool/Agent calls, causal events, active lease observations, Group diagnostics, and redacted Outbox health.
- Extended the real-model Group UAT gate with a pathological causal re-entry scenario that must be safely bounded by exactly one durable diagnostic while the useful Group work still completes.
- Added an append-only Usage/cost ledger with effective-dated immutable price snapshots, automatic Provider and durable Tool/Sandbox attempt attribution, explicit unknown cost, reconciled rollups, audited adjustments, owned-Run APIs, and an administrator Usage console.
- Added versioned Agent evaluation suites that execute real isolated draft-test Runs, deterministically settle quality and cost assertions, bind evidence to the exact candidate assembly hash, and block publication when an active current suite has not passed that candidate.
- Added a persisted minimum Tenant authority with default-Tenant migration, active membership validation, tenant-scoped user APIs, composite MySQL ownership constraints, Tenant-bound Outbox/Worker fencing, a visible Account-page Tenant identity, and two-Tenant negative isolation tests.
- Added a Tenant-scoped local KnowledgeBase for bounded UTF-8 text, Markdown and PDF ingestion, checksum-idempotent source history, immutable local index revisions, hybrid retrieval with exact citations, Runner retrieval snapshots, explicit per-Agent grants, a governed `knowledge_search` Tool, and a user-facing Knowledge console.

### Changed

- Made the Tenant expand/backfill migration resumable after interrupted MySQL non-transactional DDL, including bounded suspension and exact restoration of legacy immutable-update guards during authority backfill.

### Release scope

- This release delivers the core of the six dependency-ordered roadmap capabilities for the local/UAT profile. It does not claim customer billing, production multi-tenant identity operations, OCR, remote knowledge storage, production HA, or strong untrusted-code isolation.

## [0.1.1] - 2026-08-14

### Added

- Added a 30-second sanitized replay of a completed real-model emergent Group UAT run, preserving the actual Agent roles, message order, and interaction edges without publishing operational identifiers or evidence details.

### Changed

- Reworked the README first screen around the user value, real Group flow, and a three-command local start path.
- Streamlined the public documentation, removed environment-specific acceptance artifacts and superseded internal plans, and added validation, release, link, and metadata hygiene gates.

### Release scope

- This patch release tightens public onboarding and release evidence. It does not change the stated single-machine/UAT trust boundary or claim production HA, strong untrusted-code isolation, or enterprise identity features.

## [0.1.0] - 2026-08-14

### Added

- Unified Agent-native Runtime shared by Chat, Plan, and Group interactions.
- Durable Run, WorkItem, Runner, AgentTurn, ProviderAttempt, Checkpoint, Outbox, lease, heartbeat, and fencing semantics.
- Draft and immutable published AgentVersion assembly with Model, Tool, Skill, Policy, Sandbox, and AgentCard snapshots.
- Model registry, Provider conformance, normalized streaming, native Tool calls, and reusable process-local Provider connection pools.
- Platform Tool governance, MCP, Web, Browser, Workspace, Sandbox, Skill, Artifact, Approval, Clarification, and Verification flows.
- Optional WorkingPlan and explicit Plan read-only approval boundary.
- Post-Chat emergent Group formation from governed Agent names/descriptions.
- Fully asynchronous Group handoffs with directed human-visible request and delivery messages.
- Group history search, shared tasks, quiescence detection, idempotent Reporter summaries, and real interaction graph projection.
- Bounded concurrent Runtime Runner task pool with short database transactions around Provider calls.
- Retry-safe fresh Compose boot across the short MySQL-ready race.
- ExecutionEvent/SSE history, Runner traces, OpenTelemetry export, retention workers, and UAT backup/restore tooling.
- Public-release documentation, local bootstrap commands, repository safety checks, and GitHub CI.

### Known limitations

- The default Compose deployment is a single-machine development profile.
- Local Sandbox is not a production-grade untrusted-code boundary.
- Enterprise multi-tenancy, production OIDC/OBO, multi-AZ infrastructure, and committed SLO/RPO/RTO remain target architecture rather than implemented release claims.
- Real Agent behavior depends on the configured model and must be validated with the real-model UAT suite.
