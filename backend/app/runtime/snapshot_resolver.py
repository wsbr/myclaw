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
from app.models.registry import (
    ModelVersion, SkillVersion, ToolConnectionRevision, ToolDefinition,
    ToolGovernance, ToolVersion,
)
from app.models.runtime_v1 import CapabilitySnapshot, DurableRunner, RunnerToolSnapshot
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
        tool_rows = list(await db.scalars(select(ToolDefinition).where(
            ToolDefinition.id.in_([item.platform_tool_id for item in frozen_tools])
        ))) if frozen_tools else []
        tools_by_id = {tool.id: tool for tool in tool_rows}
        assembled_keys: list[str] = []
        effective_keys: list[str] = []
        denied: list[dict[str, str]] = []
        definitions: list[dict[str, Any]] = []
        authorized_definitions: list[dict[str, Any]] = []
        governance_by_key: dict[str, int] = {}
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
            version = await db.get(ToolVersion, frozen.tool_version_id)
            governance = await db.get(ToolGovernance, tool.id)
            connection_revision = (
                await db.get(ToolConnectionRevision, frozen.connection_revision_id)
                if frozen.connection_revision_id else None
            )
            reason = None
            if version is None or version.definition_id != tool.id:
                reason = "tool_version_missing"
            elif frozen.tool_version_content_hash != version.content_hash:
                reason = "tool_version_hash_mismatch"
            elif tool.status != "active" or governance is None or not governance.enabled:
                reason = "platform_tool_disabled"
            elif frozen.connection_revision_id and connection_revision is None:
                reason = "connection_revision_missing"
            elif connection_revision and frozen.secret_fingerprint != connection_revision.secret_fingerprint:
                reason = "secret_fingerprint_mismatch"
            elif readonly and version.side_effect != "none":
                reason = "permission_mode_readonly"
            elif budget_blocked:
                reason = "hard_budget_exhausted"
            if reason:
                denied.append({"capability": tool.tool_key, "reason_code": reason})
                continue
            effective_keys.append(tool.tool_key)
            governance_by_key[tool.tool_key] = governance.version
            definition = {
                "name": tool.tool_key,
                "namespace": tool.namespace,
                "description": version.model_description,
                "input_schema": version.input_schema,
                "output_schema": version.output_schema,
                "platform_tool_id": tool.id,
                "tool_version_id": version.id,
                "registration_hash": version.content_hash,
                "tool_version_content_hash": version.content_hash,
                "connection_id": frozen.connection_id,
                "connection_revision_id": frozen.connection_revision_id,
                "secret_fingerprint": frozen.secret_fingerprint,
                "execution_mode": version.execution_mode,
                "side_effect": version.side_effect,
                "confirmation_policy": governance.confirmation_policy,
                "confirmation_policy_version": governance.version,
                "platform_config": {
                    "network_policy": dict(governance.network_policy or {}),
                    "workspace_policy": dict(governance.workspace_policy or {}),
                    "risk_rules": list(governance.risk_rules or []),
                },
                "annotations": dict(version.annotations or {}),
                "source": "platform",
            }
            authorized_definitions.append(definition)
            eager_keys = {
                "list_files", "search_files", "read_file", "write_file",
                "apply_patch", "execute", "publish_artifact", "web_search", "web_fetch",
            }
            if tool.tool_key in eager_keys or bool(version.annotations.get("eager")):
                definitions.append(definition)

        # Coordination Tools are Runtime-owned. They are injected consistently
        # and cannot be selected, replaced, or disabled through Agent assembly.
        is_group_runtime = (
            execution_principal.get("mode") == "group"
            or bool(execution_principal.get("group_run_id"))
        )
        for definition in RUNTIME_COORDINATION_DEFINITIONS:
            if definition.get("group_only") and not is_group_runtime:
                continue
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
        } for item in authorized_definitions + [
            item for item in definitions if item.get("source") == "runtime"
        ]])
        payload = {
            "runner_id": runner.id,
            "agent_version_id": assembly.id,
            "execution_principal": execution_principal,
            "assembled_tool_keys": assembled_keys,
            "effective_tool_keys": effective_keys,
            "authorized_tool_versions": authorized_definitions,
            "denied_capabilities": denied,
            "discoverable_agents_scope": discover_scope,
            "callable_agents_scope": discover_scope,
            "permission_mode": permission_mode,
            "governance_versions": {
                "tools": governance_by_key,
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
            authorized_tool_versions=authorized_definitions,
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
        for sort_order, definition in enumerate(authorized_definitions):
            identity = {
                "runner_id": runner.id,
                "platform_tool_id": definition["platform_tool_id"],
            }
            frozen = await db.get(RunnerToolSnapshot, identity)
            expected = (
                definition["tool_version_id"],
                definition["tool_version_content_hash"],
                definition.get("connection_revision_id"),
                definition.get("secret_fingerprint"),
            )
            if frozen is None:
                db.add(RunnerToolSnapshot(
                    **identity,
                    tool_version_id=expected[0],
                    tool_version_content_hash=expected[1],
                    connection_revision_id=expected[2],
                    secret_fingerprint=expected[3],
                    sort_order=sort_order,
                ))
            elif (
                frozen.tool_version_id,
                frozen.tool_version_content_hash,
                frozen.connection_revision_id,
                frozen.secret_fingerprint,
            ) != expected:
                raise SnapshotResolutionError("Runner ToolVersion snapshot changed during execution")
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
