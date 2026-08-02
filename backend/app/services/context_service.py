"""Fixed `context.standard` assembly with bounded, explainable sections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select

from app.models.artifact_v1 import Artifact
from app.models.foundation import UserSession
from app.models.registry import PolicyDefinition
from app.models.runtime_v1 import DurableRunner, Run, RunnerCheckpoint, WorkItem
from app.services.history_service import estimate_tokens


DEFAULT_CONTEXT_PARAMETERS = {"token_budget": 8_000, "max_artifacts": 20}


@dataclass(frozen=True)
class ContextSelection:
    content: str
    manifest: dict


async def assemble_standard_context(
    db, *, run: Run, runner: DurableRunner, policy: PolicyDefinition | None,
    parameters: dict | None,
) -> ContextSelection:
    policy_key = policy.policy_key if policy and policy.enabled else "context.standard"
    if policy_key != "context.standard":
        raise RuntimeError(f"unsupported Context Policy: {policy_key}")
    merged = {**DEFAULT_CONTEXT_PARAMETERS, **dict(parameters or {})}
    budget = max(1, min(int(merged["token_budget"]), 32_000))
    max_artifacts = max(0, min(int(merged["max_artifacts"]), 100))
    session = await db.get(UserSession, run.session_id)
    work = await db.get(WorkItem, runner.work_item_id)
    if session is None or work is None or session.user_id != run.user_id:
        raise RuntimeError("Context source is unavailable")
    checkpoint = await db.scalar(select(RunnerCheckpoint).where(
        RunnerCheckpoint.work_item_id == work.id,
        RunnerCheckpoint.safe_to_resume.is_(True),
    ).order_by(RunnerCheckpoint.created_at.desc(), RunnerCheckpoint.id.desc()).limit(1))
    artifacts = list(await db.scalars(select(Artifact).where(
        Artifact.session_id == session.id,
        Artifact.user_id == run.user_id,
        Artifact.status == "published",
    ).order_by(Artifact.created_at.desc(), Artifact.id.desc()).limit(max_artifacts))) if max_artifacts else []
    value = {
        "execution_principal": {
            "user_id": run.user_id,
            "current_agent_id": runner.agent_id,
            "mode": run.mode,
            "delegation_depth": len(runner.principal_snapshot.get("delegation_chain") or []),
        },
        "session": {"session_id": session.id, "origin_type": session.origin_type, "status": session.status},
        "run": {"run_id": run.id, "mode": run.mode, "execution_kind": run.execution_kind},
        "work_item": {
            "work_item_id": work.id, "goal": work.goal, "status": work.status,
            "plan_node_id": work.plan_node_id, "group_run_id": work.group_run_id,
            "group_node_id": work.group_node_id,
        },
        "checkpoint": ({
            "checkpoint_id": checkpoint.id,
            "checkpoint_type": checkpoint.checkpoint_type,
            "last_completed_step": checkpoint.last_completed_step,
        } if checkpoint else None),
        "published_artifacts": [{
            "artifact_id": artifact.id,
            "name": artifact.name,
            "mime_type": artifact.mime_type,
            "checksum": artifact.checksum,
            "version_no": artifact.version_no,
            "parent_artifact_id": artifact.parent_artifact_id,
        } for artifact in reversed(artifacts)],
    }
    content = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    token_estimate = estimate_tokens(content)
    while value["published_artifacts"] and token_estimate > budget:
        value["published_artifacts"].pop(0)
        content = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        token_estimate = estimate_tokens(content)
    if token_estimate > budget:
        raise RuntimeError("prompt_too_large: context.standard exceeds its token budget")
    return ContextSelection(content=content, manifest={
        "policy_key": policy_key,
        "policy_version": policy.parameter_version if policy else None,
        "parameters": merged,
        "source_ids": {
            "session_id": session.id,
            "run_id": run.id,
            "work_item_id": work.id,
            "checkpoint_id": checkpoint.id if checkpoint else None,
            "artifact_ids": [item["artifact_id"] for item in value["published_artifacts"]],
        },
        "token_estimate": token_estimate,
        "checksum": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    })
