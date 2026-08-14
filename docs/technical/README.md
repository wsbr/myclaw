# Technical contract index

The technical documentation is split by authority so that one large implementation plan does not compete with the maintained contracts.

## Runtime baseline

```text
Agent = stable identity + governance boundary
AgentVersion = immutable model, instructions, tools, skills, policies, and environment
WorkItem = durable work state
Runner = short-lived execution segment on a shared Runtime Worker

Agent Loop = Observe → Decide → Act → Observe → Adapt → Respond or Wait

Chat  = Agent Loop + direct execution policy
Plan  = Agent Loop + read-only planning and approval policy
Group = Agent Loop + governed membership and directed messaging
```

Within one MyClaw control plane, Agent discovery is a permission-filtered AgentCard query. AgentInvocation creates or resumes durable work; it does not require a permanent service or process for each Agent.

## Maintained contracts

| Concern | Authority |
| --- | --- |
| Product semantics | [Product modules](../README.md#core-concepts) |
| Architecture overview | [Technical overview](../../TECH.md) |
| Tables, fields, constraints, and indexes | [Database schema](database-schema.md) |
| State transitions, ownership, and recovery | [State machines](state-machines.md) |
| Authentication, resources, errors, idempotency, and SSE | [API and SSE](api-sse.md) |
| Machine-readable interface | [OpenAPI 3.1](openapi-v1.yaml) |
| Target production topology | [Production reference architecture](production-reference-architecture.md) |
| Production admission | [Production readiness](../operations/production-readiness.md) |
| Operational procedures | [Production runbook](../operations/production-runbook.md) |

If documents disagree, product modules define behavior, the database and state-machine documents define persistence and progression, and OpenAPI defines the public wire contract. Update all affected contracts in the same change.

## Deployment profiles

### Local development and UAT

The repository ships a single-machine Compose profile with MySQL, Redis, API, Web, Runtime workers, Tool/Sandbox workers, maintenance, and optional observability services. It is intended for development and controlled validation, not untrusted multi-tenant production.

### Production reference

The production documents describe a target multi-zone deployment with verified enterprise identity, isolated Sandbox workers, highly available data services, durable object storage, explicit SLO/RPO/RTO, and tested release and disaster-recovery procedures. Those documents are design requirements, not a statement that the local profile already meets them.

## Historical material

Superseded implementation plans, migration drafts, and raw acceptance reports are available through Git history. They are intentionally excluded from the active documentation tree to prevent stale contracts and environment-specific evidence from being mistaken for the current baseline.
