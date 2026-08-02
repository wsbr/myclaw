"""Explicit Artifact publication and stable public projections."""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from sqlalchemy import select

from app.models.artifact_v1 import Artifact, GroupArtifact
from app.models.common import uuid7
from app.models.group_v1 import GroupAgentNode, GroupRun
from app.models.runtime_v1 import DurableRunner, ToolInvocation, WorkItem
from app.persistence.reliable_writes import ExecutionEventWriter
from app.services.workspace_file_service import atomic_write, resolve_read_path, sha256_file, workspace_root
from app.services.workspace_lock_service import assert_workspace_lock


class ArtifactPublishError(ValueError):
    pass


SAFE_NAME = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,500}$")


def artifact_view(artifact: Artifact) -> dict:
    return {
        "id": artifact.id, "user_id": artifact.user_id, "session_id": artifact.session_id,
        "workspace_id": artifact.workspace_id, "relative_path": artifact.relative_path,
        "name": artifact.name, "mime_type": artifact.mime_type,
        "size_bytes": artifact.size_bytes, "checksum": artifact.checksum,
        "created_by_agent_id": artifact.created_by_agent_id,
        "work_item_id": artifact.work_item_id, "runner_id": artifact.runner_id,
        "tool_invocation_id": artifact.tool_invocation_id,
        "parent_artifact_id": artifact.parent_artifact_id,
        "status": artifact.status, "version_no": artifact.version_no,
        "version": artifact.version,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        "updated_at": artifact.updated_at.isoformat() if artifact.updated_at else None,
    }


async def publish_artifact(db, *, context, args: dict) -> Artifact:
    if not context.tool_invocation_id:
        raise ArtifactPublishError("Artifact publication requires a durable ToolInvocation")
    replay = await db.scalar(select(Artifact).where(
        Artifact.tool_invocation_id == context.tool_invocation_id,
    ))
    if replay is not None:
        return replay
    if not all((context.workspace_id, context.workspace_lock_request_id, context.workspace_lease_token)):
        raise ArtifactPublishError("Artifact publication requires a current Workspace write lease")
    await assert_workspace_lock(
        db, request_id=context.workspace_lock_request_id,
        lease_token=context.workspace_lease_token,
        fencing_token=context.workspace_fencing_token,
    )
    invocation = await db.scalar(select(ToolInvocation).where(
        ToolInvocation.id == context.tool_invocation_id,
        ToolInvocation.runner_id == context.runner_id,
        ToolInvocation.session_id == context.session_id,
        ToolInvocation.status == "running",
    ))
    runner = await db.scalar(select(DurableRunner).where(
        DurableRunner.id == context.runner_id,
        DurableRunner.session_id == context.session_id,
        DurableRunner.agent_id == context.agent_id,
    ))
    work = await db.scalar(select(WorkItem).where(
        WorkItem.id == context.task_id,
        WorkItem.session_id == context.session_id,
        WorkItem.user_id == context.user_id,
    ))
    if invocation is None or runner is None or work is None:
        raise ArtifactPublishError("Artifact publication principal does not match the current execution")
    source, _ = await resolve_read_path(db, context=context, value=args["path"])
    if not source.is_file():
        raise ArtifactPublishError("source file does not exist")
    name = args.get("name") or source.name
    if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
        raise ArtifactPublishError("Artifact name must be a safe filename")
    parent = None
    if args.get("parent_artifact_id"):
        parent = await db.scalar(select(Artifact).where(
            Artifact.id == args["parent_artifact_id"],
            Artifact.session_id == context.session_id,
            Artifact.user_id == context.user_id,
            Artifact.status != "deleted",
        ).with_for_update())
        if parent is None:
            raise ArtifactPublishError("parent Artifact is unavailable")
        existing_child = await db.scalar(select(Artifact.id).where(
            Artifact.parent_artifact_id == parent.id,
        ).limit(1))
        if existing_child is not None:
            raise ArtifactPublishError("version conflict: parent Artifact already has a successor")
        version_no = parent.version_no + 1
    else:
        version_no = 1
    publish_to_group = bool(args.get("publish_to_group", False))
    node = None
    group_session_id = context.group_session_id
    if publish_to_group:
        if not group_session_id or not context.group_node_id:
            raise ArtifactPublishError("publish_to_group requires a current Group node")
        node = await db.scalar(select(GroupAgentNode).where(
            GroupAgentNode.id == context.group_node_id,
            GroupAgentNode.group_session_id == group_session_id,
            GroupAgentNode.agent_id == context.agent_id,
        ))
        if node is None:
            raise ArtifactPublishError("current Agent is not a member of this Group")
        if context.group_run_id:
            valid_run = await db.scalar(select(GroupRun.id).where(
                GroupRun.id == context.group_run_id,
                GroupRun.group_session_id == group_session_id,
            ))
            if valid_run is None:
                raise ArtifactPublishError("current GroupRun does not belong to this Group")
    artifact_id = uuid7()
    root = await workspace_root(db, workspace_id=context.workspace_id, session_id=context.session_id)
    internal_relative = f".myclaw/artifacts/{artifact_id}/{name}"
    target = root.joinpath(*internal_relative.split("/"))
    content = source.read_bytes()
    atomic_write(target, content)
    mime_type = args.get("mime_type") or mimetypes.guess_type(name)[0] or "application/octet-stream"
    artifact = Artifact(
        id=artifact_id, user_id=context.user_id, session_id=context.session_id,
        workspace_id=context.workspace_id, relative_path=internal_relative, name=name,
        mime_type=mime_type, size_bytes=len(content), checksum=sha256_file(target),
        created_by_agent_id=context.agent_id, work_item_id=work.id,
        runner_id=runner.id, tool_invocation_id=invocation.id,
        parent_artifact_id=parent.id if parent else None,
        status="published", version_no=version_no,
    )
    db.add(artifact)
    if node is not None:
        db.add(GroupArtifact(
            group_session_id=group_session_id, artifact_id=artifact.id,
            published_by_node_id=node.id,
        ))
    await db.flush()
    await ExecutionEventWriter(db).append(
        session_id=context.session_id, run_id=work.run_id,
        group_session_id=group_session_id if node else None,
        group_run_id=context.group_run_id if node else None,
        event_type="artifact.published",
        event_dedupe_key=f"transition:artifact:{artifact.id}:published",
        actor_type="agent", actor_id=context.agent_id,
        entity_type="artifact", entity_id=artifact.id, entity_version=artifact.version,
        visibility="group" if node else "user",
        summary=f"Artifact published: {name}",
        payload={"artifact_id": artifact.id, "name": name, "version_no": version_no},
        correlation_id=work.run_id,
    )
    return artifact
