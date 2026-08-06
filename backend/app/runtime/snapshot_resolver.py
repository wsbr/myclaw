"""Resolve an immutable AgentVersion into one Runner capability snapshot."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.agent_v1 import AgentAssemblyVersion, AgentVersionSkill, AgentVersionTool
from app.models.common import uuid7
from app.models.registry import ModelVersion, PlatformTool, SkillVersion
from app.models.runtime_v1 import CapabilitySnapshot, DurableRunner
from app.runtime.coordination_tools import RUNTIME_COORDINATION_DEFINITIONS
from app.services.agent_directory_service import discover_agent_cards


class SnapshotResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedRuntimeSnapshot:
    assembly: AgentAssemblyVersion
    model_version: ModelVersion
    capability: CapabilitySnapshot
    tool_definitions: tuple[dict[str, Any], ...]
    skill_versions: tuple[SkillVersion, ...]


def _hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class SnapshotResolver:
    async def resolve(
        self,
        db: AsyncSession,
        *,
        runner: DurableRunner,
        execution_principal: dict[str, Any],
        permission_mode: str,
        hard_budget: dict[str, Any] | None = None,
    ) -> ResolvedRuntimeSnapshot:
        assembly = await db.get(AgentAssemblyVersion, runner.agent_version_id)
        if (
            assembly is None or assembly.immutable_at is None
            or assembly.publication_status not in {"published", "draft_test_snapshot"}
        ):
            raise SnapshotResolutionError("Runner must reference an immutable AgentVersion")
        if not assembly.model_version_id:
            raise SnapshotResolutionError("AgentVersion has no exact ModelVersion")
        model_version = await db.get(ModelVersion, assembly.model_version_id)
        if model_version is None:
            raise SnapshotResolutionError("AgentVersion ModelVersion is missing")

        frozen_tools = list(await db.scalars(
            select(AgentVersionTool)
            .where(AgentVersionTool.agent_version_id == assembly.id)
            .order_by(AgentVersionTool.sort_order)
        ))
        tool_rows = list(await db.scalars(select(PlatformTool).where(
            PlatformTool.id.in_([item.platform_tool_id for item in frozen_tools])
        ))) if frozen_tools else []
        tools_by_id = {tool.id: tool for tool in tool_rows}
        assembled_keys: list[str] = []
        effective_keys: list[str] = []
        denied: list[dict[str, str]] = []
        definitions: list[dict[str, Any]] = []
        runtime_coordination_names = {
            item["name"] for item in RUNTIME_COORDINATION_DEFINITIONS
        }
        readonly = permission_mode in {"readonly", "plan_readonly"}
        budget_blocked = bool((hard_budget or {}).get("tool_calls_remaining") == 0)
        for frozen in frozen_tools:
            tool = tools_by_id.get(frozen.platform_tool_id)
            if tool is None:
                raise SnapshotResolutionError(f"frozen Tool {frozen.platform_tool_id} is missing")
            if tool.tool_key in runtime_coordination_names:
                # Runtime coordination semantics win over an older assembly's
                # Platform Tool of the same name. Provider schemas must contain
                # each Tool name exactly once.
                continue
            assembled_keys.append(tool.tool_key)
            reason = None
            if frozen.tool_registration_hash != tool.registration_hash:
                reason = "registration_hash_changed"
            elif not tool.enabled:
                reason = "platform_tool_disabled"
            elif readonly and tool.side_effect != "none":
                reason = "permission_mode_readonly"
            elif budget_blocked:
                reason = "hard_budget_exhausted"
            if reason:
                denied.append({"capability": tool.tool_key, "reason_code": reason})
                continue
            effective_keys.append(tool.tool_key)
            definitions.append({
                "name": tool.tool_key,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
                "platform_tool_id": tool.id,
                "registration_hash": tool.registration_hash,
                "execution_mode": tool.execution_mode,
                "side_effect": tool.side_effect,
                "confirmation_policy": tool.confirmation_policy,
                "confirmation_policy_version": tool.config_version,
                "platform_config": dict(tool.platform_config or {}),
                "source": "platform",
            })

        # Coordination Tools are Runtime-owned. They are injected consistently
        # and cannot be selected, replaced, or disabled through Agent assembly.
        for definition in RUNTIME_COORDINATION_DEFINITIONS:
            name = definition["name"]
            assembled_keys.append(name)
            effective_keys.append(name)
            definitions.append(dict(definition))

        discoverable_cards = await discover_agent_cards(
            db,
            account_id=execution_principal.get("account_id"),
            actor_agent_id=runner.agent_id,
            limit=100,
        )
        discover_scope = {
            "agent_ids": [card.agent_id for card in discoverable_cards],
            "agent_version_ids": [card.agent_version_id for card in discoverable_cards],
            "source": "current_published_agent_cards",
        }

        frozen_skills = list(await db.scalars(
            select(AgentVersionSkill)
            .where(AgentVersionSkill.agent_version_id == assembly.id)
            .order_by(AgentVersionSkill.sort_order)
        ))
        skill_versions: list[SkillVersion] = []
        for frozen in frozen_skills:
            version = await db.get(SkillVersion, frozen.skill_version_id)
            if version is None or version.checksum != frozen.checksum:
                raise SnapshotResolutionError("frozen SkillVersion checksum mismatch")
            skill_versions.append(version)

        tool_schema_bundle_hash = _hash([{
            "name": item["name"], "input_schema": item["input_schema"],
            "registration_hash": item["registration_hash"],
        } for item in definitions])
        payload = {
            "runner_id": runner.id,
            "agent_version_id": assembly.id,
            "execution_principal": execution_principal,
            "assembled_tool_keys": assembled_keys,
            "effective_tool_keys": effective_keys,
            "denied_capabilities": denied,
            "discoverable_agents_scope": discover_scope,
            "callable_agents_scope": discover_scope,
            "permission_mode": permission_mode,
            "governance_versions": {
                "tools": {tool.tool_key: tool.config_version for tool in tool_rows},
            },
            "budget_snapshot": hard_budget or {},
            "runtime_release_id": settings.runtime_release_id,
            "harness_policy_versions": {
                "context": settings.context_policy_version,
                "compaction": settings.compaction_policy_version,
                "continuation": settings.continuation_policy_version,
            },
            "provider_adapter_version": settings.provider_adapter_version,
            "tool_schema_bundle_hash": tool_schema_bundle_hash,
        }
        capability = CapabilitySnapshot(
            id=uuid7(), runner_id=runner.id, work_item_id=runner.work_item_id,
            agent_version_id=assembly.id, execution_principal=execution_principal,
            assembled_tool_keys=assembled_keys, effective_tool_keys=effective_keys,
            denied_capabilities=denied,
            discoverable_agents_scope=discover_scope,
            callable_agents_scope=discover_scope,
            permission_mode=permission_mode,
            governance_versions=payload["governance_versions"],
            budget_snapshot=hard_budget or {}, runtime_release_id=settings.runtime_release_id,
            harness_policy_versions=payload["harness_policy_versions"],
            provider_adapter_version=str(settings.provider_adapter_version),
            tool_schema_bundle_hash=tool_schema_bundle_hash, content_hash=_hash(payload),
        )
        db.add(capability)
        await db.flush()
        runner.capability_snapshot_id = capability.id
        runner.assembly_snapshot = {
            "agent_version_id": assembly.id,
            "content_hash": assembly.content_hash,
            "model_version_id": model_version.id,
            "policy_version_ids": {
                "context": assembly.context_policy_version_id,
                "history": assembly.history_policy_version_id,
                "memory_write": assembly.memory_write_policy_version_id,
                "memory_recall": assembly.memory_recall_policy_version_id,
            },
            "sandbox_profile_version_id": assembly.sandbox_profile_version_id,
        }
        await db.flush()
        return ResolvedRuntimeSnapshot(
            assembly=assembly, model_version=model_version, capability=capability,
            tool_definitions=tuple(definitions), skill_versions=tuple(skill_versions),
        )
