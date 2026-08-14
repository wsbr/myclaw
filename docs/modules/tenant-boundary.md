# Minimum Tenant boundary

MyClaw `v0.3.0` persists a Tenant authority even for the default local
installation. Existing data is migrated into one stable `default` Tenant; the
boundary does not depend on a request header or on a caller-provided Tenant ID.

This is an enforceable local data boundary, not a claim of production
multi-customer SaaS. Tenant self-service, OIDC federation, SCIM, billing,
per-Tenant quotas and platform-operator delegation remain outside this
milestone. Model, Tool, Policy and Sandbox registries are still governed at the
deployment level; user Agents and all working data are Tenant-owned.

## Authority flow

Authentication resolves one active `PlatformAccount`, its active `Tenant`, and
an active `TenantMembership`. Access tokens bind `tenant_id`, `account_id`,
`user_id` and `auth_revision`; refresh performs the same live validation as a
normal authenticated request. Agent API keys are bound to an Agent and Tenant,
and raw user IDs are resolved only inside that Tenant.

The resulting `tenant_id + account_id` pair is fixed when a Session and Run are
created. It is propagated through WorkItems, Runners, AgentInvocation,
ToolInvocation, Groups, Memory, Workspace, Artifacts, Usage entries, audit and
tenant-owned Outbox commands. A target Agent or Tool cannot replace that
original authority.

## Enforcement

- User-facing repositories scope Tenant-owned resources before checking owner
  account or user identity. A guessed cross-Tenant ID therefore behaves like a
  missing resource.
- Core aggregate chains use composite parent identity such as
  `(id, tenant_id)` and MySQL composite foreign keys. A child cannot reference a
  valid parent ID from another Tenant.
- Tenant-owned Outbox commands carry explicit nullable columns
  `tenant_id + account_id`. Both must be present together. Runtime, Sandbox,
  maintenance and delivery workers compare the command envelope with the
  loaded aggregate graph before doing work and dead-letter a mismatch.
- Platform-global maintenance commands, currently governed Skill
  synchronization, are the intentional no-Tenant Outbox exception.
- Existing installations upgrade by creating the default Tenant, backfilling
  account/session/runtime roots, validating ownership, then enforcing non-null
  columns and foreign keys in migration `0038`.

The Account page shows the current Tenant, and Tenant administrators only list
accounts and operational data in that same boundary. Creating or moving
Tenants is deliberately not exposed as self-service in this release.

## Verification

Deterministic tests use two synthetic Tenants and prove that Session, Run,
Group, Agent, Memory, Artifact, Usage and authentication lookups fail closed.
The MySQL migration gate runs a fresh `0001 → 0040`, including a
`0040 → 0039 → 0040` worker-heartbeat round trip and the Tenant/Knowledge
`0039 → 0037 → 0039` round trip, and a direct cross-Tenant insert that must be rejected by the
database. Migration `0038` probes committed schema objects before every
expand/enforcement step, so an interrupted MySQL non-transactional DDL attempt
resumes from its durable boundary. Existing immutable-update guards are dropped
only inside the bounded authority backfill and recreated with their exact prior
definitions before the migration settles.
