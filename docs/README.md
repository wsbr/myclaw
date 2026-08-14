# MyClaw documentation

Start with the repository [README](../README.md). It contains the supported local quick start, current maturity, and non-production boundaries.

## Product and architecture

- [Product overview](../PRODUCT.md): user experience and product semantics.
- [Roadmap](../ROADMAP.md): dependency-ordered delivery record through `v0.3.0`.
- [Design philosophy](DESIGN_PHILOSOPHY.md): principles that constrain every module.
- [Technical overview](../TECH.md): runtime architecture and deployment boundaries.
- [Validation](VALIDATION.md): CI, local release gates, real-model acceptance, and evidence policy.

## Core concepts

| Topic | Document |
| --- | --- |
| Agent identity, versions, and assembly | [Agent assembly](modules/agent-assembly.md) |
| Agent discovery and long-term network direction | [Enterprise Agent Network](modules/enterprise-agent-network.md) |
| Delegation, messages, and AgentInvocation | [Agent collaboration](modules/agent-collaboration.md) |
| Plan permission mode and WorkingPlan | [Plan mode](modules/plan-mode.md) |
| Emergent membership and Group collaboration | [Group mode](modules/group-mode.md) |
| Unified Agent Loop, turns, actions, and recovery | [Runtime execution](modules/runtime-execution.md) |
| Queueing, leases, fencing, and worker concurrency | [Runtime scheduling](modules/runtime-scheduling.md) |
| Tool registration and governance | [Tool system](modules/tool-system.md) |
| Skill packaging and synchronization | [Skill system](modules/skill-system.md) |
| Model registry and Provider behavior | [Model system](modules/model-system.md) |
| Context and memory scopes | [Context and memory](modules/context-memory.md) |
| Workspace, Sandbox, and Artifact | [Sandbox and Artifact](modules/sandbox-artifact.md) |
| Identity and Agent API | [Identity API](modules/identity-api.md) |
| Persisted local Tenant authority and isolation | [Minimum Tenant boundary](modules/tenant-boundary.md) |
| Local document ingestion, retrieval, and citations | [Local knowledge base](modules/local-knowledge-base.md) |
| Channels and notifications | [Channel system](modules/channel-system.md) |
| Events, checkpoints, and telemetry | [Observability](modules/observability.md) |

## Technical contracts

- [Technical contract index](technical/README.md)
- [Database schema](technical/database-schema.md)
- [State machines](technical/state-machines.md)
- [API and SSE semantics](technical/api-sse.md)
- [OpenAPI 3.1](technical/openapi-v1.yaml)

These documents describe the intended contract. The [README maturity table](../README.md#project-maturity) and passing CI for a specific commit determine what is currently verified.

## Operations

- [Production reference architecture](technical/production-reference-architecture.md) — target architecture, not the local deployment profile.
- [Production readiness](operations/production-readiness.md) — requirements that must be satisfied before production claims.
- [Production runbook](operations/production-runbook.md) — target operational procedures.
- [Release process](operations/releasing.md) — public source release steps.

## Demo

- [Emergent Group demo](demo/emergent-group.md)

## Maintenance rules

- Define a concept once and link to it elsewhere.
- Keep current implementation claims separate from target architecture.
- Keep raw UAT evidence, screenshots, reports, traces, identifiers, and environment metadata outside Git.
- Use Git history for superseded designs and historical acceptance reports; do not keep them in the active documentation tree.
- Bind release claims to a clean commit or immutable tag and its CI result.
