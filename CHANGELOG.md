# Changelog

All notable changes to MyClaw are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use semantic versioning.

## [Unreleased]

### Added

- Defined the post-`v0.1.1` roadmap for Group diagnostics and loop guardrails, the usage/cost ledger, Agent evaluation and version gates, the Runtime management console, a minimum Tenant boundary, and a local knowledge base.
- Added append-only Group diagnostics for rejected delegations, including causal Agent chains, stable reason codes, limit snapshots, idempotent fingerprints, and a user-facing GroupRun diagnostic view.
- Added hard Group fan-out and repeated-delegation guards while preserving healthy asynchronous multi-hop handoffs and distinguishing contained guard actions from business execution failures.
- Added an administrator-only, read-only Runtime Console for global Runs, budget/progress projections, WorkItems, Runners, Provider/Tool/Agent calls, causal events, active lease observations, Group diagnostics, and redacted Outbox health.

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
