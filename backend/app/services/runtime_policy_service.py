"""Resolve frozen Agent policies and execute their Runner-facing selections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.models.agent_v1 import AgentAssemblyVersion
from app.models.memory_v1 import MemoryRecord
from app.models.registry import PolicyDefinition, PolicyVersion
from app.models.runtime_v1 import DurableRunner, Run
from app.services.context_service import ContextSelection, assemble_standard_context
from app.services.history_service import HistorySelection, select_history
from app.services.memory_service import recall_memories


class RuntimePolicyResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrozenRuntimePolicy:
    definition: PolicyDefinition
    version: PolicyVersion

    def manifest(self) -> dict:
        return {
            "policy_key": self.definition.policy_key,
            "policy_type": self.definition.policy_type,
            "policy_definition_id": self.definition.id,
            "policy_version_id": self.version.id,
            "policy_version_no": self.version.version_no,
            "policy_content_hash": self.version.content_hash,
            "parameters": dict(self.version.parameters or {}),
        }


@dataclass(frozen=True)
class RuntimePolicyInputs:
    context: ContextSelection
    history: HistorySelection
    memories: tuple[MemoryRecord, ...]
    memory_manifest: dict


_POLICY_BINDINGS = {
    "context": ("context_policy_id", "context_policy_version_id"),
    "history": ("history_policy_id", "history_policy_version_id"),
    "memory_write": ("memory_write_policy_id", "memory_write_policy_version_id"),
    "memory_recall": ("memory_recall_policy_id", "memory_recall_policy_version_id"),
}


async def resolve_frozen_runtime_policies(
    db, assembly: AgentAssemblyVersion,
) -> dict[str, FrozenRuntimePolicy]:
    """Load the exact four PolicyVersions sealed into an AgentVersion."""

    resolved: dict[str, FrozenRuntimePolicy] = {}
    for policy_type, (definition_field, version_field) in _POLICY_BINDINGS.items():
        definition_id = getattr(assembly, definition_field)
        version_id = getattr(assembly, version_field)
        if not definition_id or not version_id:
            raise RuntimePolicyResolutionError(
                f"AgentVersion has no frozen {policy_type} policy"
            )
        definition = await db.get(PolicyDefinition, definition_id)
        version = await db.get(PolicyVersion, version_id)
        if (
            definition is None
            or definition.policy_type != policy_type
            or not definition.enabled
            or definition.status != "active"
        ):
            raise RuntimePolicyResolutionError(
                f"frozen {policy_type} PolicyDefinition is unavailable"
            )
        if (
            version is None
            or version.policy_definition_id != definition.id
            or version.status != "published"
        ):
            raise RuntimePolicyResolutionError(
                f"frozen {policy_type} PolicyVersion is unavailable"
            )
        resolved[policy_type] = FrozenRuntimePolicy(definition, version)
    return resolved


async def prepare_runtime_policy_inputs(
    db,
    *,
    run: Run,
    runner: DurableRunner,
    policies: dict[str, FrozenRuntimePolicy],
    model_id: str,
    model_version_id: str | None = None,
    exclude_message_ids: set[str] | None = None,
) -> RuntimePolicyInputs:
    """Run Context, History and Memory Recall policies for one Runner."""

    context_policy = policies["context"]
    history_policy = policies["history"]
    memory_policy = policies["memory_recall"]
    context = await assemble_standard_context(
        db,
        run=run,
        runner=runner,
        policy=context_policy.definition,
        policy_version=context_policy.version,
        parameters=dict(context_policy.version.parameters or {}),
    )
    history = await select_history(
        db,
        run=run,
        runner=runner,
        policy=history_policy.definition,
        policy_version=history_policy.version,
        parameters=dict(history_policy.version.parameters or {}),
        model_id=model_id,
        model_version_id=model_version_id,
        exclude_message_ids=exclude_message_ids,
    )
    memories = tuple(await recall_memories(
        db,
        runner_id=runner.id,
        user_id=run.user_id,
        agent_id=runner.agent_id,
        policy_key=memory_policy.definition.policy_key,
        parameters=dict(memory_policy.version.parameters or {}),
    ))
    memory_value = [{
        "memory_id": item.id,
        "scope": item.scope,
        "fact_key": item.fact_key,
        "content": item.content,
        "content_hash": item.normalized_hash,
        "version": item.version,
    } for item in memories]
    memory_raw = json.dumps(
        memory_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    memory_manifest = {
        **memory_policy.manifest(),
        "memory_ids": [item.id for item in memories],
        "memory_content_hashes": [item.normalized_hash for item in memories],
        "checksum": hashlib.sha256(memory_raw.encode("utf-8")).hexdigest(),
    }
    return RuntimePolicyInputs(
        context=context,
        history=history,
        memories=memories,
        memory_manifest=memory_manifest,
    )
