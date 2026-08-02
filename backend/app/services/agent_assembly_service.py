"""Draft, optimistic update, and immutable Agent publication service."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_v1 import (
    AgentAssemblyVersion,
    AgentBlueprint,
    AgentVersionBinding,
    AgentVersionSkill,
    AgentVersionTool,
)
from app.models.common import utcnow, uuid7
from app.models.registry import (
    PlatformTool,
    PolicyDefinition,
    RegisteredModel,
    RegisteredSkill,
    SandboxProfile,
)
from app.persistence.reliable_writes import PlatformAuditWriter


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


async def create_agent(
    db: AsyncSession,
    *,
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
    db: AsyncSession, version_id: str, selection: AssemblySelection
) -> None:
    version = await db.get(AgentAssemblyVersion, version_id)
    if version is None:
        raise LookupError("agent version not found")
    if version.agent_id in selection.bound_agent_ids:
        raise ValueError("agent cannot bind itself")
    await db.execute(delete(AgentVersionTool).where(AgentVersionTool.agent_version_id == version_id))
    await db.execute(delete(AgentVersionSkill).where(AgentVersionSkill.agent_version_id == version_id))
    await db.execute(delete(AgentVersionBinding).where(AgentVersionBinding.agent_version_id == version_id))
    db.add_all(
        AgentVersionTool(agent_version_id=version_id, platform_tool_id=value, sort_order=index)
        for index, value in enumerate(selection.tool_ids)
    )
    db.add_all(
        AgentVersionSkill(agent_version_id=version_id, skill_id=value, sort_order=index)
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
    issues = await _validate_publishable(db, draft, selection)
    if issues:
        raise AgentPublishValidationError(issues)

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
        sandbox_profile_id=draft.sandbox_profile_id,
        context_policy_id=draft.context_policy_id,
        history_policy_id=draft.history_policy_id,
        memory_write_policy_id=draft.memory_write_policy_id,
        memory_recall_policy_id=draft.memory_recall_policy_id,
        group_enabled=draft.group_enabled,
        created_by_account_id=actor_account_id,
    )
    db.add(published)
    await db.flush()
    await _replace_selection(db, published.id, selection)
    # Selection rows must exist before the version is sealed. MySQL triggers
    # intentionally reject any relation INSERT once immutable_at is set.
    await db.flush()
    published.content_hash = _content_hash(published, selection)
    published.immutable_at = utcnow()
    published.published_at = published.immutable_at

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


async def _validate_publishable(
    db: AsyncSession, draft: AgentAssemblyVersion, selection: AssemblySelection
) -> list[str]:
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
    if draft.model_id and not await db.scalar(select(RegisteredModel.id).where(RegisteredModel.id == draft.model_id, RegisteredModel.enabled.is_(True))):
        issues.append("model is unavailable")
    if draft.sandbox_profile_id and not await db.scalar(select(SandboxProfile.id).where(SandboxProfile.id == draft.sandbox_profile_id, SandboxProfile.enabled.is_(True))):
        issues.append("sandbox is unavailable")
    for label, policy_id, expected_type in (
        ("context policy", draft.context_policy_id, "context"),
        ("history policy", draft.history_policy_id, "history"),
        ("memory write policy", draft.memory_write_policy_id, "memory_write"),
        ("memory recall policy", draft.memory_recall_policy_id, "memory_recall"),
    ):
        if policy_id and not await db.scalar(select(PolicyDefinition.id).where(PolicyDefinition.id == policy_id, PolicyDefinition.policy_type == expected_type, PolicyDefinition.enabled.is_(True))):
            issues.append(f"{label} is unavailable")
    if selection.tool_ids:
        count = len(list(await db.scalars(select(PlatformTool.id).where(PlatformTool.id.in_(selection.tool_ids), PlatformTool.enabled.is_(True)))))
        if count != len(set(selection.tool_ids)):
            issues.append("one or more tools are unavailable")
    if selection.skill_ids:
        count = len(list(await db.scalars(select(RegisteredSkill.id).where(
            RegisteredSkill.id.in_(selection.skill_ids), RegisteredSkill.status == "active",
            RegisteredSkill.current_published_version_id.is_not(None),
        ))))
        if count != len(set(selection.skill_ids)):
            issues.append("one or more skills are unavailable")
    return issues


def _content_hash(version: AgentAssemblyVersion, selection: AssemblySelection) -> str:
    payload = {
        "name": version.name,
        "description": version.description,
        "instructions": version.instructions,
        "model_id": version.model_id,
        "sandbox_profile_id": version.sandbox_profile_id,
        "policies": [version.context_policy_id, version.history_policy_id, version.memory_write_policy_id, version.memory_recall_policy_id],
        "group_enabled": version.group_enabled,
        "tools": selection.tool_ids,
        "skills": selection.skill_ids,
        "bindings": selection.bound_agent_ids,
    }
    value = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    issues = await _validate_publishable(db, draft, selection)
    if issues:
        raise AgentPublishValidationError(issues)
    snapshot = AgentAssemblyVersion(
        id=uuid7(), agent_id=agent.id, version_no=None,
        publication_status="draft_test_snapshot", draft_revision=None,
        derived_from_version_id=draft.derived_from_version_id,
        name=draft.name, description=draft.description, instructions=draft.instructions,
        model_id=draft.model_id, sandbox_profile_id=draft.sandbox_profile_id,
        context_policy_id=draft.context_policy_id, history_policy_id=draft.history_policy_id,
        memory_write_policy_id=draft.memory_write_policy_id,
        memory_recall_policy_id=draft.memory_recall_policy_id,
        group_enabled=False, created_by_account_id=actor_account_id,
    )
    db.add(snapshot)
    await db.flush()
    await _replace_selection(db, snapshot.id, selection)
    snapshot.content_hash = _content_hash(snapshot, selection)
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
