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
    ToolDefinition,
    ToolConnection,
    ToolConnectionRevision,
    ToolGovernance,
    ToolVersion,
    PolicyDefinition,
    PolicyVersion,
    SandboxProfile,
    SandboxProfileVersion,
)
from app.core.tool_registry.schema import assert_valid_schema, builtin_result_schema
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
    output_schema: dict
    implementation_ref: str
    namespace: str = "platform"
    origin: str = "builtin"
    display_name_zh: str = ""
    examples: list = field(default_factory=list)
    error_catalog: list = field(default_factory=list)
    annotations: dict = field(default_factory=dict)
    implementation_version: str = "1"
    execution_mode: str = "inline"
    side_effect: str = "none"
    supports_cancel: bool = False
    supports_status_lookup: bool = False
    initial_confirmation_policy: str = field(default="never", compare=False)
    initial_platform_config: dict = field(default_factory=dict, compare=False)

    @property
    def content_hash(self) -> str:
        return _stable_hash({
            "tool_key": self.tool_key,
            "namespace": self.namespace,
            "origin": self.origin,
            "model_description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "examples": self.examples,
            "error_catalog": self.error_catalog,
            "annotations": self.annotations,
            "implementation_ref": self.implementation_ref,
            "implementation_version": self.implementation_version,
            "execution_mode": self.execution_mode,
            "side_effect": self.side_effect,
            "supports_cancel": self.supports_cancel,
            "supports_status_lookup": self.supports_status_lookup,
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
    browser_connection = await db.scalar(select(ToolConnection).where(
        ToolConnection.connection_key == "browser.local",
    ))
    if browser_connection is None:
        browser_connection = ToolConnection(
            id=uuid7(), connection_key="browser.local", display_name="Isolated Chromium Worker",
            connection_type="browser", auth_type="none", status="active",
            is_primary=False, next_revision_no=1, health_status="untested",
        )
        db.add(browser_connection)
        await db.flush()
    if browser_connection.current_revision_id is None:
        payload = {
            "endpoint": "https://local-browser-worker", "auth_type": "none",
            "secret_fingerprint": None, "config": {
                "isolated_context_per_session": True, "inherits_user_profile": False,
            },
        }
        browser_revision = ToolConnectionRevision(
            id=uuid7(), connection_id=browser_connection.id,
            revision_no=browser_connection.next_revision_no,
            endpoint=payload["endpoint"], auth_type="none", config=payload["config"],
            content_hash=_stable_hash(payload),
        )
        db.add(browser_revision)
        await db.flush()
        browser_connection.current_revision_id = browser_revision.id
        browser_connection.next_revision_no += 1
    await db.flush()


async def reconcile_platform_tools(
    db: AsyncSession,
    specs: Iterable[CodeToolSpec],
    *,
    job_run_id: str,
) -> list[ToolDefinition]:
    """Publish immutable versions while preserving administrator governance."""

    specs = list(specs)
    reconciled: list[ToolDefinition] = []
    audit = PlatformAuditWriter(db)
    for spec in specs:
        assert_valid_schema(spec.input_schema, label=f"{spec.tool_key} input_schema")
        assert_valid_schema(spec.output_schema, label=f"{spec.tool_key} output_schema")
        tool = await db.scalar(
            select(ToolDefinition)
            .where(ToolDefinition.tool_key == spec.tool_key)
            .with_for_update()
        )
        action = "platform_tool.reconciled"
        changed_fields: list[str] = []
        if tool is None:
            tool = ToolDefinition(
                id=uuid7(),
                tool_key=spec.tool_key,
                namespace=spec.namespace,
                origin=spec.origin,
                display_name=spec.display_name,
                display_name_zh=spec.display_name_zh,
                summary=spec.description,
                status="active",
                next_version_no=1,
            )
            db.add(tool)
            await db.flush()
            changed_fields = ["created"]
        for field_name, value in {
            "namespace": spec.namespace,
            "origin": spec.origin,
            "display_name": spec.display_name,
            "display_name_zh": spec.display_name_zh,
            "summary": spec.description,
            "status": "active",
        }.items():
            if getattr(tool, field_name) != value:
                setattr(tool, field_name, value)
                changed_fields.append(field_name)
        version = await db.scalar(select(ToolVersion).where(
            ToolVersion.definition_id == tool.id,
            ToolVersion.content_hash == spec.content_hash,
        ))
        default_connection_id = None
        if spec.tool_key.startswith("browser_"):
            default_connection_id = await db.scalar(select(ToolConnection.id).where(
                ToolConnection.connection_key == "browser.local",
                ToolConnection.status == "active",
            ))
        if version is None:
            version = ToolVersion(
                id=uuid7(), definition_id=tool.id, version_no=tool.next_version_no,
                model_description=spec.description,
                input_schema=spec.input_schema, output_schema=spec.output_schema,
                examples=spec.examples, error_catalog=spec.error_catalog,
                annotations=spec.annotations,
                implementation_ref=spec.implementation_ref,
                implementation_version=spec.implementation_version,
                execution_mode=spec.execution_mode, side_effect=spec.side_effect,
                supports_cancel=spec.supports_cancel,
                supports_status_lookup=spec.supports_status_lookup,
                default_connection_id=default_connection_id,
                content_hash=spec.content_hash, published_at=utcnow(),
            )
            db.add(version)
            await db.flush()
            tool.next_version_no += 1
            changed_fields.append("tool_version")
        if tool.current_version_id != version.id:
            tool.current_version_id = version.id
            changed_fields.append("current_version_id")
        governance = await db.get(ToolGovernance, tool.id)
        if governance is None:
            governance = ToolGovernance(
                tool_definition_id=tool.id, enabled=True,
                confirmation_policy=(
                    "per_call" if spec.initial_confirmation_policy == "always"
                    else spec.initial_confirmation_policy
                ),
                network_policy=dict(spec.initial_platform_config.get("network_policy") or {}),
                workspace_policy={
                    key: value for key, value in spec.initial_platform_config.items()
                    if key != "network_policy"
                },
                risk_rules=[],
            )
            db.add(governance)
            changed_fields.append("governance")
        if changed_fields:
            tool.version += 1
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
                resource_version=tool.version,
                outcome="succeeded",
                summary=f"Reconciled platform tool {tool.tool_key}",
                changes={"fields": changed_fields},
                dedupe_key=f"audit:{job_run_id}:platform_tool:{tool.id}:reconcile",
            )
        reconciled.append(tool)
    active_keys = {spec.tool_key for spec in specs}
    stale_code_tools = list(await db.scalars(select(ToolDefinition).where(
        ToolDefinition.origin == "builtin",
        ToolDefinition.tool_key.not_in(active_keys),
        ToolDefinition.status == "active",
    ).with_for_update()))
    for tool in stale_code_tools:
        tool.status = "inactive"
        tool.version += 1
        governance = await db.get(ToolGovernance, tool.id)
        if governance is not None and governance.enabled:
            governance.enabled = False
            governance.version += 1
        await audit.append(
            category="registry", action="platform_tool.registration_missing",
            actor_type="service", actor_id="registry-reconcile", actor_user_id=None,
            resource_type="platform_tool", resource_id=tool.id,
            resource_version=tool.version, outcome="succeeded",
            summary=f"Disabled platform tool with missing code registration: {tool.tool_key}",
            changes={"fields": ["enabled"], "reason": "registration_missing"},
            dedupe_key=f"audit:{job_run_id}:platform_tool:{tool.id}:registration_missing",
        )
    return reconciled


def builtin_tool_specs() -> list[CodeToolSpec]:
    """Project native built-ins into the one authoritative registry."""

    from app.core.tool_registry.registry import BUILTIN_TOOLS

    governance = {
        "list_files": ("never", {"workspace_access": "read"}, "durable_async", "none"),
        "search_files": ("never", {"workspace_access": "read"}, "durable_async", "none"),
        "read_file": ("never", {"workspace_access": "read"}, "durable_async", "none"),
        "write_file": ("session_grant", {"workspace_access": "write"}, "durable_async", "idempotent"),
        "apply_patch": ("session_grant", {"workspace_access": "write"}, "durable_async", "idempotent"),
        "delete_path": ("per_call", {"workspace_access": "write"}, "durable_async", "non_idempotent"),
        "execute": ("session_grant", {
            "workspace_access": "write", "network_policy": {"egress": "none"},
        }, "durable_async", "non_idempotent"),
        "search_group_history": ("never", {}, "inline", "none"),
        "publish_artifact": ("never", {"workspace_access": "write"}, "durable_async", "idempotent"),
        "memory_search": ("never", {}, "inline", "none"),
        "memory_propose": ("never", {}, "inline", "idempotent"),
        "web_search": ("never", {"network_policy": {"egress": "configured_provider_only"}}, "durable_async", "none"),
        "web_fetch": ("never", {"network_policy": {"egress": "public_http_https"}}, "durable_async", "none"),
        "browser_navigate": ("never", {"network_policy": {"egress": "public_http_https"}}, "durable_async", "idempotent"),
        "browser_observe": ("never", {"network_policy": {"egress": "current_browser_page"}}, "durable_async", "none"),
        "browser_interact": ("never", {"network_policy": {"egress": "current_browser_origin"}, "workspace_access": "write"}, "durable_async", "non_idempotent"),
        "browser_screenshot": ("never", {"network_policy": {"egress": "current_browser_page"}, "workspace_access": "write"}, "durable_async", "idempotent"),
    }
    return [
        CodeToolSpec(
            tool_key=tool.name,
            display_name=tool.name.replace("_", " ").title(),
            description=tool.description,
            input_schema=tool.input_schema,
            output_schema=builtin_result_schema(tool.name),
            display_name_zh=tool.name.replace("_", " "),
            error_catalog=[
                {"category": "validation", "recovery": "Correct arguments and retry."},
                {"category": "authorization", "recovery": "Use an authorized scope or ask the user."},
                {"category": "conflict", "recovery": "Refresh checksum/revision and retry."},
                {"category": "timeout", "recovery": "Narrow the request or retry when marked retryable."},
            ],
            annotations={
                "untrusted_external_content": tool.name.startswith(("web_", "browser_")),
                **({
                    "retrieval_profile": "balanced",
                    "model_visible_total_tokens": 12_000,
                    "model_visible_input_fraction": 0.20,
                    "per_turn_limit": 2 if tool.name == "web_search" else 3,
                    "normal_run_limit": 2 if tool.name == "web_search" else 3,
                    "per_run_limit": 6 if tool.name == "web_search" else 8,
                } if tool.name in {"web_search", "web_fetch"} else {}),
            },
            implementation_ref=f"{tool.__class__.__module__}:{tool.__class__.__name__}",
            implementation_version=(
                "platform-tools.v3.web-fetch-balanced-4"
                if tool.name == "web_fetch"
                else "platform-tools.v3.web-search-balanced-2"
                if tool.name == "web_search"
                else "platform-tools.v3.execute-2"
                if tool.name == "execute"
                else "platform-tools.v3.publish-artifact-2"
                if tool.name == "publish_artifact"
                else "platform-tools.v3.memory-propose-2"
                if tool.name == "memory_propose"
                else "platform-tools.v3.memory-search-2"
                if tool.name == "memory_search"
                else "platform-tools.v3.browser-interact-3"
                if tool.name == "browser_interact"
                else "platform-tools.v3.browser-screenshot-2"
                if tool.name == "browser_screenshot"
                else "platform-tools.v3"
            ),
            execution_mode=governance[tool.name][2],
            side_effect=governance[tool.name][3],
            supports_cancel=tool.name == "execute",
            initial_confirmation_policy=governance[tool.name][0],
            initial_platform_config=governance[tool.name][1],
        )
        for tool in BUILTIN_TOOLS.values()
        if tool.name in governance
    ]
