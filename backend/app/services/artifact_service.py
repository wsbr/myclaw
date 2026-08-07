"""Explicit Artifact publication and stable public projections."""

from __future__ import annotations

import mimetypes
import re

from sqlalchemy import select

from app.models.artifact_v1 import Artifact, GroupArtifact
from app.models.common import uuid7
from app.models.group_v1 import GroupMembership, GroupRun
from app.models.runtime_v1 import DurableRunner, ToolInvocation, WorkItem, Workspace
from app.persistence.reliable_writes import ExecutionEventWriter
from app.services.workspace_file_service import (
    atomic_write, group_shared_root, private_agent_root, resolve_read_path,
    sha256_file, workspace_root,
)
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
    if replay is not None and replay.status == "published":
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
    source, source_relative = await resolve_read_path(
        db, context=context, value=args["path"],
    )
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
    membership = None
    group_session_id = context.group_session_id
    if publish_to_group:
        if not group_session_id or not context.group_membership_id:
            raise ArtifactPublishError("publish_to_group requires a current Group membership")
        membership = await db.scalar(select(GroupMembership).where(
            GroupMembership.id == context.group_membership_id,
            GroupMembership.group_session_id == group_session_id,
            GroupMembership.agent_id == context.agent_id,
            GroupMembership.status == "joined",
        ))
        if membership is None:
            raise ArtifactPublishError("current Agent is not a member of this Group")
        if context.group_run_id:
            valid_run = await db.scalar(select(GroupRun.id).where(
                GroupRun.id == context.group_run_id,
                GroupRun.group_session_id == group_session_id,
            ))
            if valid_run is None:
                raise ArtifactPublishError("current GroupRun does not belong to this Group")

    content = source.read_bytes()
    source_checksum = sha256_file(source)
    artifact_id = replay.id if replay is not None else uuid7()
    root = await workspace_root(
        db, workspace_id=context.workspace_id, session_id=context.session_id,
    )
    internal_relative = f".myclaw/artifacts/{artifact_id}/{name}"
    target = root.joinpath(*internal_relative.split("/"))
    mime_type = args.get("mime_type") or mimetypes.guess_type(name)[0] or "application/octet-stream"
    if replay is None:
        artifact = Artifact(
            id=artifact_id, user_id=context.user_id, session_id=context.session_id,
            workspace_id=context.workspace_id, relative_path=internal_relative,
            source_relative_path=source_relative, name=name,
            mime_type=mime_type, size_bytes=len(content), checksum=source_checksum,
            created_by_agent_id=context.agent_id, work_item_id=work.id,
            runner_id=runner.id, tool_invocation_id=invocation.id,
            parent_artifact_id=parent.id if parent else None,
            status="publishing", version_no=version_no,
        )
        db.add(artifact)
        if membership is not None:
            db.add(GroupArtifact(
                group_session_id=group_session_id, artifact_id=artifact.id,
                published_by_membership_id=membership.id,
            ))
        await db.flush()
        # Durable intent precedes the immutable file snapshot.
        await db.commit()
    else:
        artifact = replay
        if (
            artifact.checksum != source_checksum
            or artifact.size_bytes != len(content)
            or artifact.relative_path != internal_relative
        ):
            artifact.status = "unavailable"
            artifact.version += 1
            await db.commit()
            raise ArtifactPublishError("publishing Artifact source changed after registration")

    if target.exists():
        if (
            not target.is_file() or target.stat().st_size != artifact.size_bytes
            or sha256_file(target) != artifact.checksum
        ):
            artifact.status = "unavailable"
            artifact.version += 1
            await db.commit()
            raise ArtifactPublishError("immutable Artifact target has conflicting bytes")
    else:
        atomic_write(target, content)

    artifact = await db.scalar(select(Artifact).where(
        Artifact.id == artifact.id,
    ).with_for_update())
    if artifact.status == "published":
        await db.commit()
        return artifact
    if artifact.status != "publishing":
        await db.commit()
        raise ArtifactPublishError("Artifact publication cannot be resumed")
    artifact.status = "published"
    artifact.version += 1
    await db.flush()
    await ExecutionEventWriter(db).append(
        session_id=context.session_id, run_id=work.run_id,
        group_session_id=group_session_id if membership else None,
        group_run_id=context.group_run_id if membership else None,
        event_type="artifact.published",
        event_dedupe_key=f"transition:artifact:{artifact.id}:published",
        actor_type="agent", actor_id=context.agent_id,
        entity_type="artifact", entity_id=artifact.id, entity_version=artifact.version,
        visibility="group" if membership else "user",
        summary=f"Artifact published: {name}",
        payload={"artifact_id": artifact.id, "name": name, "version_no": version_no},
        correlation_id=work.run_id,
    )
    await db.commit()
    return artifact


async def reconcile_one_publishing_artifact(db) -> bool:
    """Finalize or quarantine one crash-interrupted immutable publication."""
    artifact = await db.scalar(select(Artifact).where(
        Artifact.status == "publishing",
    ).order_by(Artifact.created_at).with_for_update(skip_locked=True).limit(1))
    if artifact is None:
        return False
    workspace = await db.get(Workspace, artifact.workspace_id)
    work = await db.get(WorkItem, artifact.work_item_id) if artifact.work_item_id else None
    if workspace is None:
        artifact.status = "unavailable"
        artifact.version += 1
        return True
    root = await workspace_root(
        db, workspace_id=artifact.workspace_id, session_id=artifact.session_id,
    )
    target = root.joinpath(*artifact.relative_path.split("/"))
    valid = bool(
        target.is_file()
        and target.stat().st_size == artifact.size_bytes
        and sha256_file(target) == artifact.checksum
    )
    if not valid and artifact.source_relative_path:
        group_link = await db.scalar(select(GroupArtifact).where(
            GroupArtifact.artifact_id == artifact.id,
        ))
        source_root = (
            await group_shared_root(
                db, workspace_id=artifact.workspace_id,
                session_id=artifact.session_id,
                group_session_id=group_link.group_session_id,
            )
            if group_link is not None else
            await private_agent_root(
                db, workspace_id=artifact.workspace_id,
                session_id=artifact.session_id,
                agent_id=artifact.created_by_agent_id,
            )
        )
        source = source_root.joinpath(*artifact.source_relative_path.split("/"))
        if (
            source.is_file() and source.stat().st_size == artifact.size_bytes
            and sha256_file(source) == artifact.checksum and not target.exists()
        ):
            atomic_write(target, source.read_bytes())
            valid = True
    artifact.status = "published" if valid else "unavailable"
    artifact.version += 1
    await ExecutionEventWriter(db).append(
        session_id=artifact.session_id, run_id=work.run_id if work else None,
        event_type=("artifact.published" if valid else "artifact.unavailable"),
        event_dedupe_key=f"artifact:{artifact.id}:reconciled:{artifact.status}",
        actor_type="service", actor_id="artifact-reconciler",
        entity_type="artifact", entity_id=artifact.id,
        entity_version=artifact.version, visibility="user",
        summary=(
            f"Artifact published: {artifact.name}"
            if valid else f"Artifact unavailable: {artifact.name}"
        ),
        payload={"artifact_id": artifact.id, "reconciled": True},
        correlation_id=work.run_id if work else artifact.id,
    )
    return True
