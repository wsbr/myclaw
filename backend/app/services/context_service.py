"""Policy-controlled context assembly with bounded, explainable sections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import and_, or_, select

from app.models.artifact_v1 import Artifact, MessageArtifact
from app.models.foundation import UserSession
from app.models.group_v1 import GroupRun, GroupRunMember
from app.models.registry import PolicyDefinition, PolicyVersion
from app.models.runtime_v1 import DurableRunner, Run, RunnerCheckpoint, WorkItem
from app.services.history_service import VisibleRecordRepository, estimate_tokens
from app.services.context_policy_service import resolve_context_parameters


@dataclass(frozen=True)
class ContextSelection:
    content: str
    manifest: dict


async def _directly_visible_artifacts(db, *, run, runner, work, limit: int) -> list[Artifact]:
    """Select metadata under the same direct-message boundary as History.

    Publishing for the user or Group catalog is not a delivery to every Agent.
    Other Agents' artifacts enter this projection only through an explicit
    attachment on a directly visible message. Invocation context/results remain
    on their own continuation path and never authorize ambient Session data.
    """
    if not limit:
        return []
    if run.mode == "group":
        scope = VisibleRecordRepository(db).runner_scope(run, runner)
        if (
            scope["group_run_id"] != work.group_run_id
            or scope["group_membership_id"] != work.group_membership_id
            or await db.scalar(select(GroupRunMember.agent_id).join(
                GroupRun, GroupRun.id == GroupRunMember.group_run_id,
            ).where(
                GroupRun.run_id == run.id,
                GroupRunMember.group_run_id == work.group_run_id,
                GroupRunMember.membership_id == work.group_membership_id,
                GroupRunMember.agent_id == runner.agent_id,
            )) is None
        ):
            raise RuntimeError("Context Group principal is unavailable")
    repository = VisibleRecordRepository(db)
    visibility = ("direct", "group") if run.mode == "group" else ("user", "direct")
    attachment = select(MessageArtifact.artifact_id).where(
        MessageArtifact.artifact_id == Artifact.id,
        MessageArtifact.visibility.in_(visibility),
    ).exists()
    # Delegated Chat/Plan Agents have no ambient parent-message visibility.
    may_receive_messages = run.mode == "group" or not (runner.input_snapshot or {}).get("agent_invocation_id")
    eligibility = Artifact.created_by_agent_id == runner.agent_id
    if may_receive_messages:
        eligibility = or_(eligibility, attachment)
    statement = select(Artifact).where(
        Artifact.tenant_id == run.tenant_id,
        Artifact.session_id == run.session_id,
        Artifact.user_id == run.user_id,
        Artifact.status == "published",
        eligibility,
    )
    selected = []
    before = None
    while len(selected) < limit:
        page = statement
        if before is not None:
            page = page.where(or_(Artifact.created_at < before[0], and_(
                Artifact.created_at == before[0], Artifact.id < before[1],
            )))
        candidates = list(await db.scalars(page.order_by(
            Artifact.created_at.desc(), Artifact.id.desc(),
        ).limit(100)))
        if not candidates:
            break
        other_ids = [item.id for item in candidates if item.created_by_agent_id != runner.agent_id]
        delivered = set()
        if other_ids:
            # Page relation rows as well: a popular artifact can be attached to
            # arbitrarily many old messages. Exact lookup avoids History's
            # recent-window limit silently dropping an earlier delivery.
            after_link = None
            while True:
                links_query = select(MessageArtifact.message_id, MessageArtifact.artifact_id).where(
                    MessageArtifact.artifact_id.in_(other_ids),
                    MessageArtifact.visibility.in_(visibility),
                )
                if after_link is not None:
                    links_query = links_query.where(or_(
                        MessageArtifact.message_id > after_link[0],
                        and_(MessageArtifact.message_id == after_link[0], MessageArtifact.artifact_id > after_link[1]),
                    ))
                links = list((await db.execute(links_query.order_by(
                    MessageArtifact.message_id, MessageArtifact.artifact_id,
                ).limit(200))).all())
                if not links:
                    break
                visible = {item.id for item in await repository.read_for_runner(
                    run=run, runner=runner, message_ids=list({row.message_id for row in links}),
                )}
                delivered.update(row.artifact_id for row in links if row.message_id in visible)
                after_link = (links[-1].message_id, links[-1].artifact_id)
                if len(links) < 200 or delivered.issuperset(other_ids):
                    break
        selected.extend(item for item in candidates if (
            item.created_by_agent_id == runner.agent_id or item.id in delivered
        ))
        before = (candidates[-1].created_at, candidates[-1].id)
        if len(candidates) < 100:
            break
    return selected[:limit]


async def assemble_standard_context(
    db, *, run: Run, runner: DurableRunner, policy: PolicyDefinition | None,
    parameters: dict | None, policy_version: PolicyVersion | None = None,
) -> ContextSelection:
    policy_key = policy.policy_key if policy and policy.enabled else "context.standard"
    merged = resolve_context_parameters(policy_key, parameters, frozen=policy_version is not None)
    budget = merged["token_budget"]
    max_artifacts = merged["max_artifacts"]
    session = await db.get(UserSession, run.session_id)
    work = await db.get(WorkItem, runner.work_item_id)
    if (
        session is None or work is None or session.user_id != run.user_id
        or session.tenant_id != run.tenant_id or runner.tenant_id != run.tenant_id
        or work.tenant_id != run.tenant_id or runner.run_id != run.id
        or runner.session_id != run.session_id or work.run_id != run.id
        or work.session_id != run.session_id or work.user_id != run.user_id
        or work.owner_agent_id != runner.agent_id
        or work.agent_version_id != runner.agent_version_id
    ):
        raise RuntimeError("Context source is unavailable")
    checkpoint = await db.scalar(select(RunnerCheckpoint).where(
        RunnerCheckpoint.work_item_id == work.id,
        RunnerCheckpoint.safe_to_resume.is_(True),
    ).order_by(RunnerCheckpoint.created_at.desc(), RunnerCheckpoint.id.desc()).limit(1))
    artifacts = await _directly_visible_artifacts(
        db, run=run, runner=runner, work=work, limit=max_artifacts,
    )
    value = {
        "execution_principal": {
            "user_id": run.user_id,
            "current_agent_id": runner.agent_id,
            "mode": run.mode,
            "delegation_depth": len(runner.principal_snapshot.get("delegation_chain") or []),
        },
        "session": {"session_id": session.id, "origin_type": session.origin_type, "status": session.status},
        "run": {"run_id": run.id, "mode": run.mode, "status": run.status},
        "work_item": {
            "work_item_id": work.id, "goal": work.goal, "status": work.status,
            "group_run_id": work.group_run_id,
            "group_membership_id": work.group_membership_id,
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
        raise RuntimeError(f"prompt_too_large: {policy_key} exceeds its token budget")
    return ContextSelection(content=content, manifest={
        "policy_key": policy_key,
        "policy_definition_id": policy.id if policy else None,
        "policy_version_id": policy_version.id if policy_version else None,
        "policy_version_no": policy_version.version_no if policy_version else None,
        "policy_content_hash": policy_version.content_hash if policy_version else None,
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
