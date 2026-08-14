"""Draft, optimistic update, and immutable Agent publication service."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.agent_v1 import (
    AgentAssemblyVersion,
    AgentBlueprint,
    AgentCard,
    AgentVersionBinding,
    AgentVersionSkill,
    AgentVersionTool,
)
from app.models.common import utcnow, uuid7
from app.models.registry import (
    ConformanceRun,
    ModelVersion,
    ToolConnection,
    ToolConnectionRevision,
    ToolDefinition,
    ToolGovernance,
    ToolVersion,
    PolicyDefinition,
    PolicyVersion,
    RegisteredModel,
    RegisteredSkill,
    SandboxProfile,
    SandboxProfileVersion,
    SkillVersion,
)
from app.persistence.reliable_writes import PlatformAuditWriter
from app.services.skill_catalog_service import (
    SkillCatalogOverflow,
    build_skill_catalog_sections,
)
from app.services.agent_eval_gate_service import agent_eval_gate_issues


class AgentDraftConflict(RuntimeError):
    pass


class AgentPublishValidationError(ValueError):
    def __init__(self, issues: list[str]) -> None:
        super().__init__("Agent draft is not publishable")
        self.issues = issues


@dataclass(frozen=True)
class AssemblySelection:
    tool_ids: tuple[str, ...] = ()
    skill_ids: tuple[str, ...] = ()
    bound_agent_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedAssembly:
    model_version: ModelVersion
    policy_versions: dict[str, PolicyVersion]
    sandbox_version: SandboxProfileVersion
    tools: tuple["ResolvedTool", ...]
    skills: tuple[tuple[RegisteredSkill, SkillVersion], ...]

    def safe_snapshot(self) -> dict[str, Any]:
        return {
            "model": {
                "definition_id": self.model_version.model_id,
                "version_id": self.model_version.id,
                "content_hash": self.model_version.content_hash,
            },
            "policies": {
                key: {
                    "definition_id": value.policy_definition_id,
                    "version_id": value.id,
                    "content_hash": value.content_hash,
                }
                for key, value in sorted(self.policy_versions.items())
            },
            "sandbox": {
                "definition_id": self.sandbox_version.sandbox_profile_id,
                "version_id": self.sandbox_version.id,
                "content_hash": self.sandbox_version.content_hash,
            },
            "tools": [
                {
                    "tool_definition_id": tool.definition.id,
                    "tool_version_id": tool.version.id,
                    "content_hash": tool.version.content_hash,
                    "connection_id": tool.connection.id if tool.connection else None,
                    "connection_revision_id": tool.connection_revision.id if tool.connection_revision else None,
                    "secret_fingerprint": tool.connection_revision.secret_fingerprint if tool.connection_revision else None,
                }
                for tool in self.tools
            ],
            "skills": [
                {
                    "skill_id": skill.id,
                    "skill_version_id": version.id,
                    "checksum": version.checksum,
                }
                for skill, version in self.skills
            ],
        }


@dataclass(frozen=True)
class ResolvedTool:
    definition: ToolDefinition
    version: ToolVersion
    governance: ToolGovernance
    connection: ToolConnection | None = None
    connection_revision: ToolConnectionRevision | None = None


async def create_agent(
    db: AsyncSession,
    *,
    tenant_id: str,
    owner_account_id: str,
    created_by_account_id: str,
    name: str = "",
    description: str = "",
    visibility: str = "private",
    initial_values: dict[str, Any] | None = None,
    selection: AssemblySelection | None = None,
) -> tuple[AgentBlueprint, AgentAssemblyVersion]:
    agent = AgentBlueprint(
        id=uuid7(),
        tenant_id=tenant_id,
        owner_account_id=owner_account_id,
        visibility=visibility,
        status="draft",
        next_version_no=1,
    )
    db.add(agent)
    await db.flush()
    draft = AgentAssemblyVersion(
        id=uuid7(),
        agent_id=agent.id,
        version_no=None,
        publication_status="draft",
        draft_revision=1,
        name=name,
        description=description,
        instructions="",
        discovery_config={},
        collaboration_config={},
        group_enabled=False,
        created_by_account_id=created_by_account_id,
    )
    for key, value in (initial_values or {}).items():
        setattr(draft, key, value)
    db.add(draft)
    await db.flush()
    if selection is not None:
        await _replace_selection(db, draft.id, selection)
    agent.draft_version_id = draft.id
    await db.flush()
    return agent, draft


async def update_agent_draft(
    db: AsyncSession,
    *,
    agent_id: str,
    expected_draft_revision: int,
    values: dict[str, Any],
    selection: AssemblySelection | None = None,
) -> AgentAssemblyVersion:
    agent = await db.scalar(
        select(AgentBlueprint).where(AgentBlueprint.id == agent_id).with_for_update()
    )
    if agent is None or agent.draft_version_id is None:
        raise LookupError("agent draft not found")
    draft = await db.scalar(
        select(AgentAssemblyVersion)
        .where(AgentAssemblyVersion.id == agent.draft_version_id)
        .with_for_update()
    )
    if draft is None or draft.draft_revision != expected_draft_revision:
        raise AgentDraftConflict("draft revision changed")

    allowed = {
        "name", "description", "instructions", "model_id", "sandbox_profile_id",
        "context_policy_id", "history_policy_id", "memory_write_policy_id",
        "memory_recall_policy_id", "group_enabled",
        "discovery_config", "collaboration_config",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unsupported draft fields: {', '.join(sorted(unknown))}")
    for key, value in values.items():
        setattr(draft, key, value)
    if selection is not None:
        await _replace_selection(db, draft.id, selection)
    draft.draft_revision += 1
    await db.flush()
    return draft


async def _replace_selection(
    db: AsyncSession, version_id: str, selection: AssemblySelection,
    resolved: ResolvedAssembly | None = None,
) -> None:
    version = await db.get(AgentAssemblyVersion, version_id)
    if version is None:
        raise LookupError("agent version not found")
    if version.agent_id in selection.bound_agent_ids:
        raise ValueError("agent cannot bind itself")
    owner = await db.get(AgentBlueprint, version.agent_id)
    if owner is None:
        raise LookupError("agent not found")
    if selection.bound_agent_ids:
        visible_bound_ids = set(await db.scalars(select(AgentBlueprint.id).where(
            AgentBlueprint.id.in_(selection.bound_agent_ids),
            AgentBlueprint.tenant_id == owner.tenant_id,
        )))
        if visible_bound_ids != set(selection.bound_agent_ids):
            raise ValueError("bound Agent is outside the current Tenant boundary")
    await db.execute(delete(AgentVersionTool).where(AgentVersionTool.agent_version_id == version_id))
    await db.execute(delete(AgentVersionSkill).where(AgentVersionSkill.agent_version_id == version_id))
    await db.execute(delete(AgentVersionBinding).where(AgentVersionBinding.agent_version_id == version_id))
    db.add_all(
        AgentVersionTool(
            agent_version_id=version_id,
            platform_tool_id=value,
            tool_version_id=(next(tool.version.id for tool in resolved.tools if tool.definition.id == value) if resolved is not None else None),
            tool_version_content_hash=(next(tool.version.content_hash for tool in resolved.tools if tool.definition.id == value) if resolved is not None else None),
            connection_id=(next((tool.connection.id if tool.connection else None) for tool in resolved.tools if tool.definition.id == value) if resolved is not None else None),
            connection_revision_id=(next((tool.connection_revision.id if tool.connection_revision else None) for tool in resolved.tools if tool.definition.id == value) if resolved is not None else None),
            secret_fingerprint=(next((tool.connection_revision.secret_fingerprint if tool.connection_revision else None) for tool in resolved.tools if tool.definition.id == value) if resolved is not None else None),
            sort_order=index,
        )
        for index, value in enumerate(selection.tool_ids)
    )
    db.add_all(
        AgentVersionSkill(
            agent_version_id=version_id,
            skill_id=value,
            skill_version_id=(
                next(item.id for skill, item in resolved.skills if skill.id == value)
                if resolved is not None else None
            ),
            checksum=(
                next(item.checksum for skill, item in resolved.skills if skill.id == value)
                if resolved is not None else None
            ),
            sort_order=index,
        )
        for index, value in enumerate(selection.skill_ids)
    )
    db.add_all(
        AgentVersionBinding(agent_version_id=version_id, bound_agent_id=value, sort_order=index)
        for index, value in enumerate(selection.bound_agent_ids)
    )


async def publish_agent(
    db: AsyncSession,
    *,
    agent_id: str,
    expected_draft_revision: int,
    actor_account_id: str,
    idempotency_record_id: str,
) -> AgentAssemblyVersion:
    agent = await db.scalar(
        select(AgentBlueprint).where(AgentBlueprint.id == agent_id).with_for_update()
    )
    if agent is None or agent.draft_version_id is None:
        raise LookupError("agent draft not found")
    draft = await db.scalar(
        select(AgentAssemblyVersion)
        .where(AgentAssemblyVersion.id == agent.draft_version_id)
        .with_for_update()
    )
    if draft is None or draft.draft_revision != expected_draft_revision:
        raise AgentDraftConflict("draft revision changed")

    selection = await _load_selection(db, draft.id)
    issues, resolved = await check_publish_capabilities(db, draft, selection)
    if issues:
        raise AgentPublishValidationError(issues)
    assert resolved is not None
    candidate_content_hash = _content_hash(draft, selection, resolved)
    gate_issues = await agent_eval_gate_issues(
        db, agent_id=agent.id, candidate_content_hash=candidate_content_hash,
        lock_suites=True,
    )
    if gate_issues:
        raise AgentPublishValidationError(gate_issues)

    published = AgentAssemblyVersion(
        id=uuid7(),
        agent_id=agent.id,
        version_no=agent.next_version_no,
        publication_status="published",
        draft_revision=None,
        derived_from_version_id=draft.derived_from_version_id,
        name=draft.name,
        description=draft.description,
        instructions=draft.instructions,
        model_id=draft.model_id,
        model_version_id=resolved.model_version.id,
        sandbox_profile_id=draft.sandbox_profile_id,
        sandbox_profile_version_id=resolved.sandbox_version.id,
        context_policy_id=draft.context_policy_id,
        context_policy_version_id=resolved.policy_versions["context"].id,
        history_policy_id=draft.history_policy_id,
        history_policy_version_id=resolved.policy_versions["history"].id,
        memory_write_policy_id=draft.memory_write_policy_id,
        memory_write_policy_version_id=resolved.policy_versions["memory_write"].id,
        memory_recall_policy_id=draft.memory_recall_policy_id,
        memory_recall_policy_version_id=resolved.policy_versions["memory_recall"].id,
        group_enabled=draft.group_enabled,
        discovery_config=dict(draft.discovery_config or {}),
        collaboration_config=dict(draft.collaboration_config or {}),
        created_by_account_id=actor_account_id,
    )
    db.add(published)
    await db.flush()
    await _replace_selection(db, published.id, selection, resolved)
    # Selection rows must exist before the version is sealed. MySQL triggers
    # intentionally reject any relation INSERT once immutable_at is set.
    await db.flush()
    published.content_hash = candidate_content_hash
    published.immutable_at = utcnow()
    published.published_at = published.immutable_at
    db.add(_agent_card(agent, published, resolved))

    agent.current_published_version_id = published.id
    agent.next_version_no += 1
    agent.version += 1
    if agent.status == "draft":
        agent.status = "published"
    draft.derived_from_version_id = published.id
    draft.draft_revision += 1
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="agent",
        action="agent.published",
        actor_type="account",
        actor_id=actor_account_id,
        actor_user_id=None,
        resource_type="agent",
        resource_id=agent.id,
        resource_version=agent.version,
        outcome="succeeded",
        summary=f"Published Agent version {published.version_no}",
        changes={"version_no": published.version_no, "content_hash": published.content_hash},
        dedupe_key=f"audit:publish_agent:{idempotency_record_id}",
    )
    return published


async def _load_selection(db: AsyncSession, version_id: str) -> AssemblySelection:
    tools = list(await db.scalars(select(AgentVersionTool.platform_tool_id).where(AgentVersionTool.agent_version_id == version_id).order_by(AgentVersionTool.sort_order)))
    skills = list(await db.scalars(select(AgentVersionSkill.skill_id).where(AgentVersionSkill.agent_version_id == version_id).order_by(AgentVersionSkill.sort_order)))
    bindings = list(await db.scalars(select(AgentVersionBinding.bound_agent_id).where(AgentVersionBinding.agent_version_id == version_id).order_by(AgentVersionBinding.sort_order)))
    return AssemblySelection(tuple(tools), tuple(skills), tuple(bindings))


async def check_publish_capabilities(
    db: AsyncSession, draft: AgentAssemblyVersion, selection: AssemblySelection
) -> tuple[list[str], ResolvedAssembly | None]:
    issues: list[str] = []
    for field in ("name", "description", "instructions"):
        if not (getattr(draft, field) or "").strip():
            issues.append(f"{field} is required")
    required_ids = {
        "model": draft.model_id,
        "sandbox": draft.sandbox_profile_id,
        "context policy": draft.context_policy_id,
        "history policy": draft.history_policy_id,
        "memory write policy": draft.memory_write_policy_id,
        "memory recall policy": draft.memory_recall_policy_id,
    }
    issues.extend(f"{label} is required" for label, value in required_ids.items() if not value)
    model = await db.get(RegisteredModel, draft.model_id) if draft.model_id else None
    model_version = (
        await db.get(ModelVersion, model.current_version_id)
        if model is not None and model.current_version_id else None
    )
    if model is None or not model.enabled or model.status != "active" or model_version is None:
        issues.append("model is unavailable or has no immutable current version")
    else:
        embedding_model = (model_version.capabilities or {}).get("model_kind") == "embedding"
        if embedding_model:
            issues.append("embedding models cannot be selected as an Agent chat model")
        latest_conformance = None if embedding_model else await db.scalar(
            select(ConformanceRun)
            .where(
                ConformanceRun.subject_type == "model_version",
                ConformanceRun.subject_id == model_version.id,
                ConformanceRun.suite_version == settings.provider_conformance_suite_version,
                ConformanceRun.status.in_(("passed", "failed")),
            )
            .order_by(ConformanceRun.ended_at.desc(), ConformanceRun.created_at.desc())
            .limit(1)
        )
        if not embedding_model and (
            latest_conformance is None or latest_conformance.status != "passed"
        ):
            issues.append(
                f"model version has not passed {settings.provider_conformance_suite_version}"
            )

    sandbox = await db.get(SandboxProfile, draft.sandbox_profile_id) if draft.sandbox_profile_id else None
    sandbox_version = (
        await db.get(SandboxProfileVersion, sandbox.current_version_id)
        if sandbox is not None and sandbox.current_version_id else None
    )
    if (
        sandbox is None or not sandbox.enabled or sandbox.status != "active"
        or sandbox_version is None or sandbox_version.status != "published"
    ):
        issues.append("sandbox is unavailable or has no published immutable version")

    policy_versions: dict[str, PolicyVersion] = {}
    for label, policy_id, expected_type in (
        ("context policy", draft.context_policy_id, "context"),
        ("history policy", draft.history_policy_id, "history"),
        ("memory write policy", draft.memory_write_policy_id, "memory_write"),
        ("memory recall policy", draft.memory_recall_policy_id, "memory_recall"),
    ):
        if not policy_id:
            continue
        policy = await db.get(PolicyDefinition, policy_id)
        version = (
            await db.get(PolicyVersion, policy.current_version_id)
            if policy is not None and policy.current_version_id else None
        )
        if (
            policy is None or policy.policy_type != expected_type
            or not policy.enabled or policy.status != "active"
            or version is None or version.status != "published"
        ):
            issues.append(f"{label} is unavailable or has no published immutable version")
        else:
            policy_versions[expected_type] = version

    tools: list[ResolvedTool] = []
    if selection.tool_ids:
        rows = list(await db.scalars(select(ToolDefinition).where(
            ToolDefinition.id.in_(selection.tool_ids), ToolDefinition.status == "active",
        )))
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(set(selection.tool_ids)):
            issues.append("one or more tools are unavailable")
        else:
            for value in selection.tool_ids:
                definition = by_id[value]
                version = await db.get(ToolVersion, definition.current_version_id)
                governance = await db.get(ToolGovernance, definition.id)
                if version is None or governance is None or not governance.enabled:
                    issues.append(f"tool {definition.tool_key} is disabled or has no immutable current version")
                    continue
                connection = None
                connection_revision = None
                connection_id = version.default_connection_id
                if definition.tool_key == "web_search" and connection_id is None:
                    connection = await db.scalar(select(ToolConnection).where(
                        ToolConnection.connection_type.in_(["bocha", "brave", "bailian"]),
                        ToolConnection.is_primary.is_(True), ToolConnection.status == "active",
                    ))
                elif connection_id:
                    connection = await db.get(ToolConnection, connection_id)
                if connection is not None:
                    connection_revision = await db.get(ToolConnectionRevision, connection.current_revision_id)
                    if connection.status != "active" or connection_revision is None:
                        issues.append(f"tool {definition.tool_key} connection is unavailable")
                        continue
                elif definition.tool_key == "web_search" or definition.origin == "mcp":
                    issues.append(f"tool {definition.tool_key} requires an active connection")
                    continue
                tools.append(ResolvedTool(definition, version, governance, connection, connection_revision))

    skills: list[tuple[RegisteredSkill, SkillVersion]] = []
    if selection.skill_ids:
        rows = list(await db.scalars(select(RegisteredSkill).where(
            RegisteredSkill.id.in_(selection.skill_ids), RegisteredSkill.status == "active",
            RegisteredSkill.current_published_version_id.is_not(None),
        )))
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(set(selection.skill_ids)):
            issues.append("one or more skills are unavailable")
        else:
            for skill_id in selection.skill_ids:
                skill = by_id[skill_id]
                version = await db.get(SkillVersion, skill.current_published_version_id)
                if version is None or version.status != "published":
                    issues.append(f"skill {skill_id} has no published immutable version")
                    continue
                skills.append((skill, version))
    if skills:
        try:
            build_skill_catalog_sections(
                [version for _, version in skills],
                max_tokens=settings.skill_catalog_max_tokens,
            )
        except SkillCatalogOverflow as error:
            issues.append(str(error))
    if issues or model_version is None or sandbox_version is None or len(policy_versions) != 4:
        return issues, None
    return issues, ResolvedAssembly(
        model_version=model_version,
        policy_versions=policy_versions,
        sandbox_version=sandbox_version,
        tools=tuple(tools),
        skills=tuple(skills),
    )


def _content_hash(
    version: AgentAssemblyVersion,
    selection: AssemblySelection,
    resolved: ResolvedAssembly,
) -> str:
    payload = {
        "name": version.name,
        "description": version.description,
        "instructions": version.instructions,
        "model": resolved.safe_snapshot()["model"],
        "sandbox": resolved.safe_snapshot()["sandbox"],
        "policies": resolved.safe_snapshot()["policies"],
        "group_enabled": version.group_enabled,
        "discovery_config": version.discovery_config,
        "collaboration_config": version.collaboration_config,
        "tools": resolved.safe_snapshot()["tools"],
        "skills": resolved.safe_snapshot()["skills"],
        "bindings": selection.bound_agent_ids,
    }
    value = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def assembly_content_hash(
    version: AgentAssemblyVersion,
    selection: AssemblySelection,
    resolved: ResolvedAssembly,
) -> str:
    """Public name for the exact draft/publish candidate identity."""

    return _content_hash(version, selection, resolved)


def _agent_card(
    agent: AgentBlueprint,
    version: AgentAssemblyVersion,
    resolved: ResolvedAssembly,
) -> AgentCard:
    discovery = dict(version.discovery_config or {})
    collaboration = dict(version.collaboration_config or {})
    declaration = {
        "description": version.description,
        "domain_tags": list(discovery.get("domain_tags") or []),
        "supported_outcomes": list(discovery.get("supported_outcomes") or []),
        "accepted_request_summary": str(discovery.get("accepted_request_summary") or ""),
    }
    capabilities = {
        "model_version_id": resolved.model_version.id,
        "tool_keys": [tool.definition.tool_key for tool in resolved.tools],
        "skill_version_ids": [item.id for _, item in resolved.skills],
        "sandbox_profile_version_id": resolved.sandbox_version.id,
    }
    attestation = {
        "assembly_content_hash": version.content_hash,
        "conformance_suite_version": settings.provider_conformance_suite_version,
        "model_version_id": resolved.model_version.id,
    }
    payload = {
        "agent_id": agent.id,
        "agent_version_id": version.id,
        "visibility": agent.visibility,
        "name": version.name,
        "declaration": declaration,
        "capabilities": capabilities,
        "collaboration": collaboration,
        "attestation": attestation,
        "group_enabled": version.group_enabled,
    }
    content_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return AgentCard(
        id=uuid7(),
        agent_id=agent.id,
        agent_version_id=version.id,
        visibility=agent.visibility,
        name=version.name,
        description=version.description,
        identity_kind=str(discovery.get("identity_kind") or "agent"),
        domain_tags=declaration["domain_tags"],
        supported_outcomes=declaration["supported_outcomes"],
        accepted_request_summary=declaration["accepted_request_summary"],
        capabilities_summary=capabilities,
        collaboration_summary=collaboration,
        declaration=declaration,
        attestation=attestation,
        availability="available",
        group_enabled=version.group_enabled,
        content_hash=content_hash,
        published_at=version.published_at,
    )


async def create_draft_test_snapshot(
    db: AsyncSession,
    *,
    agent_id: str,
    expected_draft_revision: int,
    actor_account_id: str,
    idempotency_record_id: str,
) -> AgentAssemblyVersion:
    agent = await db.scalar(
        select(AgentBlueprint).where(AgentBlueprint.id == agent_id).with_for_update()
    )
    if agent is None or agent.draft_version_id is None:
        raise LookupError("agent draft not found")
    draft = await db.scalar(
        select(AgentAssemblyVersion)
        .where(AgentAssemblyVersion.id == agent.draft_version_id)
        .with_for_update()
    )
    if draft is None or draft.draft_revision != expected_draft_revision:
        raise AgentDraftConflict("draft revision changed")
    selection = await _load_selection(db, draft.id)
    issues, resolved = await check_publish_capabilities(db, draft, selection)
    if issues:
        raise AgentPublishValidationError(issues)
    assert resolved is not None
    snapshot = AgentAssemblyVersion(
        id=uuid7(), agent_id=agent.id, version_no=None,
        publication_status="draft_test_snapshot", draft_revision=None,
        derived_from_version_id=draft.derived_from_version_id,
        name=draft.name, description=draft.description, instructions=draft.instructions,
        model_id=draft.model_id, model_version_id=resolved.model_version.id,
        sandbox_profile_id=draft.sandbox_profile_id,
        sandbox_profile_version_id=resolved.sandbox_version.id,
        context_policy_id=draft.context_policy_id,
        context_policy_version_id=resolved.policy_versions["context"].id,
        history_policy_id=draft.history_policy_id,
        history_policy_version_id=resolved.policy_versions["history"].id,
        memory_write_policy_id=draft.memory_write_policy_id,
        memory_write_policy_version_id=resolved.policy_versions["memory_write"].id,
        memory_recall_policy_id=draft.memory_recall_policy_id,
        memory_recall_policy_version_id=resolved.policy_versions["memory_recall"].id,
        discovery_config=dict(draft.discovery_config or {}),
        collaboration_config=dict(draft.collaboration_config or {}),
        # Runtime draft-test isolation prevents Plan/Group entry. Preserve the
        # assembly bit so this snapshot hash is the exact publish candidate.
        group_enabled=draft.group_enabled, created_by_account_id=actor_account_id,
    )
    db.add(snapshot)
    await db.flush()
    await _replace_selection(db, snapshot.id, selection, resolved)
    # Relationship triggers reject writes after the parent version is sealed.
    # Flush the copied Tool/Skill/Binding rows before setting immutable_at;
    # otherwise SQLAlchemy may order the parent UPDATE before child INSERTs in
    # the final flush.
    await db.flush()
    snapshot.content_hash = _content_hash(snapshot, selection, resolved)
    snapshot.immutable_at = utcnow()
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="agent", action="agent.draft_test_snapshot_created",
        actor_type="account", actor_id=actor_account_id, actor_user_id=None,
        resource_type="agent_version", resource_id=snapshot.id, outcome="succeeded",
        summary="Created immutable draft test snapshot",
        changes={"content_hash": snapshot.content_hash},
        dedupe_key=f"audit:draft_test_snapshot:{idempotency_record_id}",
    )
    return snapshot
