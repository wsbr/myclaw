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
The MySQL migration gate runs a fresh `0001 → 0038`, a `0038 → 0037 → 0038`
round trip, and a direct cross-Tenant insert that must be rejected by the
database.

