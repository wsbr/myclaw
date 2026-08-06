"""Idempotent platform registry seed and code-tool reconciliation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import utcnow, uuid7
from app.models.registry import (
    PlatformTool,
    PolicyDefinition,
    PolicyVersion,
    SandboxProfile,
    SandboxProfileVersion,
)
from app.persistence.reliable_writes import PlatformAuditWriter


def _stable_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CodeToolSpec:
    tool_key: str
    display_name: str
    description: str
    input_schema: dict
    output_schema: dict | None
    implementation_ref: str
    implementation_version: str = "1"
    execution_mode: str = "inline"
    side_effect: str = "none"
    initial_confirmation_policy: str = field(default="never", compare=False)
    initial_platform_config: dict = field(default_factory=dict, compare=False)

    @property
    def registration_hash(self) -> str:
        return _stable_hash({
            "tool_key": self.tool_key,
            "display_name": self.display_name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "implementation_ref": self.implementation_ref,
            "implementation_version": self.implementation_version,
            "execution_mode": self.execution_mode,
            "side_effect": self.side_effect,
        })


POLICY_SEEDS = (
    ("context.standard", "context", "Standard context", {"token_budget": 8_000}),
    ("history.recent", "history", "Recent history", {"token_budget": 24_000, "max_messages": 80}),
    ("history.summary_recent", "history", "Summary plus recent history", {
        "token_budget": 24_000, "max_messages": 80, "summary_trigger_tokens": 12_000,
        "summary_token_budget": 2_000, "summary_recent_token_budget": 8_000,
    }),
    ("memory_write.none", "memory_write", "Do not write memory", {}),
    ("memory_write.user_agent_facts", "memory_write", "Write user-agent facts", {}),
    ("memory_write.user_facts", "memory_write", "Write user facts", {}),
    ("memory_recall.none", "memory_recall", "Do not recall memory", {"max_items": 0, "token_budget": 0}),
    ("memory_recall.user_agent", "memory_recall", "Recall user-agent memory", {"max_items": 20, "token_budget": 4_000}),
    ("memory_recall.user_combined", "memory_recall", "Recall combined user memory", {"max_items": 20, "token_budget": 4_000}),
)


async def seed_core_registries(db: AsyncSession) -> None:
    for key, policy_type, name, default_parameters in POLICY_SEEDS:
        existing = await db.scalar(
            select(PolicyDefinition).where(PolicyDefinition.policy_key == key)
        )
        if existing is None:
            existing = PolicyDefinition(
                id=uuid7(),
                policy_key=key,
                policy_type=policy_type,
                name=name,
                description=name,
                implementation_ref=f"app.runtime.policies:{key}",
                implementation_version="1",
                default_parameters=default_parameters,
                parameter_version=1,
                enabled=True,
                status="active",
                next_version_no=1,
            )
            db.add(existing)
            await db.flush()
        if existing.current_version_id is None:
            content = {
                "implementation_ref": existing.implementation_ref,
                "parameters": existing.default_parameters,
                "policy_type": existing.policy_type,
            }
            version = PolicyVersion(
                id=uuid7(),
                policy_definition_id=existing.id,
                version_no=existing.next_version_no,
                implementation_ref=existing.implementation_ref,
                parameters=dict(existing.default_parameters or {}),
                content_hash=_stable_hash(content),
                status="published",
                published_at=utcnow(),
                version=1,
            )
            db.add(version)
            await db.flush()
            existing.current_version_id = version.id
            existing.next_version_no += 1

    sandbox = await db.scalar(
        select(SandboxProfile).where(SandboxProfile.profile_key == "local")
    )
    if sandbox is None:
        sandbox = SandboxProfile(
            id=uuid7(),
            profile_key="local",
            type="local",
            description="Session-scoped local workspace",
            implementation_ref="app.runtime.sandbox:LocalSandbox",
            config={},
            config_version=1,
            enabled=True,
            status="active",
            next_version_no=1,
        )
        db.add(sandbox)
        await db.flush()
    if sandbox.current_version_id is None:
        content = {
            "provider_ref": sandbox.implementation_ref,
            "runtime_class": "local",
            "workspace_protocol_version": "workspace.v1",
            "artifact_protocol_version": "artifact.v1",
            "resource_policy": sandbox.config,
            "egress_policy": {},
            "mount_policy": {"private_agent": True, "group_shared": True},
            "security_level": "dev_directory",
        }
        version = SandboxProfileVersion(
            id=uuid7(),
            sandbox_profile_id=sandbox.id,
            version_no=sandbox.next_version_no,
            provider_ref=sandbox.implementation_ref,
            runtime_class="local",
            image_digest=None,
            workspace_protocol_version="workspace.v1",
            artifact_protocol_version="artifact.v1",
            resource_policy=dict(sandbox.config or {}),
            egress_policy={},
            mount_policy={"private_agent": True, "group_shared": True},
            security_level="dev_directory",
            content_hash=_stable_hash(content),
            status="published",
            published_at=utcnow(),
            version=1,
        )
        db.add(version)
        await db.flush()
        sandbox.current_version_id = version.id
        sandbox.next_version_no += 1
    await db.flush()


async def reconcile_platform_tools(
    db: AsyncSession,
    specs: Iterable[CodeToolSpec],
    *,
    job_run_id: str,
) -> list[PlatformTool]:
    """Reconcile code-owned fields while preserving administrator governance."""

    specs = list(specs)
    reconciled: list[PlatformTool] = []
    audit = PlatformAuditWriter(db)
    for spec in specs:
        tool = await db.scalar(
            select(PlatformTool)
            .where(PlatformTool.tool_key == spec.tool_key)
            .with_for_update()
        )
        action = "platform_tool.reconciled"
        changed_fields: list[str] = []
        if tool is None:
            tool = PlatformTool(
                id=uuid7(),
                tool_key=spec.tool_key,
                display_name=spec.display_name,
                description=spec.description,
                input_schema=spec.input_schema,
                output_schema=spec.output_schema,
                implementation_ref=spec.implementation_ref,
                implementation_version=spec.implementation_version,
                execution_mode=spec.execution_mode,
                side_effect=spec.side_effect,
                enabled=True,
                confirmation_policy=spec.initial_confirmation_policy,
                platform_config=spec.initial_platform_config,
                config_version=1,
                registration_hash=spec.registration_hash,
            )
            db.add(tool)
            changed_fields = ["created"]
        elif tool.registration_hash != spec.registration_hash:
            code_fields = {
                "description": spec.description,
                "input_schema": spec.input_schema,
                "output_schema": spec.output_schema,
                "implementation_ref": spec.implementation_ref,
                "implementation_version": spec.implementation_version,
                "execution_mode": spec.execution_mode,
                "side_effect": spec.side_effect,
                "registration_hash": spec.registration_hash,
            }
            for field, value in code_fields.items():
                if getattr(tool, field) != value:
                    setattr(tool, field, value)
                    changed_fields.append(field)
            tool.config_version += 1
        await db.flush()
        if changed_fields:
            await audit.append(
                category="registry",
                action=action,
                actor_type="service",
                actor_id="registry-reconcile",
                actor_user_id=None,
                resource_type="platform_tool",
                resource_id=tool.id,
                resource_version=tool.config_version,
                outcome="succeeded",
                summary=f"Reconciled platform tool {tool.tool_key}",
                changes={"fields": changed_fields},
                dedupe_key=f"audit:{job_run_id}:platform_tool:{tool.id}:reconcile",
            )
        reconciled.append(tool)
    active_keys = {spec.tool_key for spec in specs}
    stale_code_tools = list(await db.scalars(select(PlatformTool).where(
        PlatformTool.implementation_ref.like("app.core.tool_registry.builtin.%"),
        PlatformTool.tool_key.not_in(active_keys),
        PlatformTool.enabled.is_(True),
    ).with_for_update()))
    for tool in stale_code_tools:
        tool.enabled = False
        tool.config_version += 1
        await audit.append(
            category="registry", action="platform_tool.registration_missing",
            actor_type="service", actor_id="registry-reconcile", actor_user_id=None,
            resource_type="platform_tool", resource_id=tool.id,
            resource_version=tool.config_version, outcome="succeeded",
            summary=f"Disabled platform tool with missing code registration: {tool.tool_key}",
            changes={"fields": ["enabled"], "reason": "registration_missing"},
            dedupe_key=f"audit:{job_run_id}:platform_tool:{tool.id}:registration_missing",
        )
    return reconciled


def legacy_builtin_specs() -> list[CodeToolSpec]:
    """Bridge current built-ins into the one authoritative registry."""

    from app.core.tool_registry.registry import BUILTIN_TOOLS

    governance = {
        "read_file": ("never", {"workspace_access": "read"}, "durable_async", "none"),
        "write_file": ("session_grant", {"workspace_access": "write"}, "durable_async", "idempotent"),
        "execute": ("session_grant", {"workspace_access": "write"}, "durable_async", "non_idempotent"),
        "search_group_history": ("never", {}, "inline", "none"),
        "publish_artifact": ("never", {"workspace_access": "write"}, "durable_async", "idempotent"),
    }
    return [
        CodeToolSpec(
            tool_key=tool.name,
            display_name=tool.name.replace("_", " ").title(),
            description=tool.description,
            input_schema=tool.input_schema,
            output_schema=None,
            implementation_ref=f"{tool.__class__.__module__}:{tool.__class__.__name__}",
            execution_mode=governance[tool.name][2],
            side_effect=governance[tool.name][3],
            initial_confirmation_policy=governance[tool.name][0],
            initial_platform_config=governance[tool.name][1],
        )
        for tool in BUILTIN_TOOLS.values()
        if tool.name in governance
    ]
