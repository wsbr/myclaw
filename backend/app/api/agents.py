from __future__ import annotations

import re
import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import CurrentAccount, get_current_account, get_current_builder
from app.models.agent_v1 import (
    AgentAssemblyVersion,
    AgentBlueprint,
    AgentCard,
    AgentVersionBinding,
    AgentVersionSkill,
    AgentVersionTool,
)
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord
from app.models.foundation import AgentApiKey
from app.persistence.reliable_writes import PlatformAuditWriter
from app.schemas.agent_v1 import (
    AgentCreateV1,
    AgentDetailV1,
    AgentDraftPatch,
    AgentDraftResponse,
    AgentPublishResponse,
    AgentSummaryV1,
    PublishAgentRequest,
    PublishedVersionResponse,
    AgentVersionDetailResponse,
    AgentStateCommand,
    AgentApiKeyCreate,
    AgentApiKeyResponse,
    DraftTestRunRequest,
    CapabilityCheckResponse,
)
from app.services.agent_assembly_service import (
    AgentDraftConflict,
    AgentPublishValidationError,
    AssemblySelection,
    create_agent as create_agent_domain,
    publish_agent as publish_agent_domain,
    update_agent_draft,
    create_draft_test_snapshot,
    check_publish_capabilities,
    assembly_content_hash,
)
from app.services.agent_eval_gate_service import agent_eval_gate_issues
from app.services.agent_api_key_service import create_api_key, revoke_api_key
from app.services.chat_service import create_user_session, enqueue_chat_message
from app.services.cursor_service import SortKey, paginate_scalars
from app.models.foundation import UserSession
from app.schemas.runtime_v1 import ChatCommandResponse, MessageResponse, RunResponse

router = APIRouter(prefix="/agents", tags=["agents"])


def _if_match_revision(value: str | None) -> int:
    if value is None:
        raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    match = re.search(r"(\d+)", value)
    if match is None:
        raise HTTPException(status_code=400, detail={"code": "invalid_if_match"})
    return int(match.group(1))


async def _get_owned_agent(db: AsyncSession, agent_id: str, current: CurrentAccount) -> AgentBlueprint:
    agent = await db.scalar(select(AgentBlueprint).where(
        AgentBlueprint.id == agent_id,
        AgentBlueprint.tenant_id == current.tenant_id,
    ))
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_account_id != current.account.id and "admin" not in current.roles:
        raise HTTPException(status_code=403, detail="Access denied")
    return agent


async def _selection(db: AsyncSession, version_id: str) -> AssemblySelection:
    tools = tuple(await db.scalars(select(AgentVersionTool.platform_tool_id).where(AgentVersionTool.agent_version_id == version_id).order_by(AgentVersionTool.sort_order)))
    skills = tuple(await db.scalars(select(AgentVersionSkill.skill_id).where(AgentVersionSkill.agent_version_id == version_id).order_by(AgentVersionSkill.sort_order)))
    bindings = tuple(await db.scalars(select(AgentVersionBinding.bound_agent_id).where(AgentVersionBinding.agent_version_id == version_id).order_by(AgentVersionBinding.sort_order)))
    return AssemblySelection(tools, skills, bindings)


async def _detail(db: AsyncSession, agent: AgentBlueprint) -> AgentDetailV1:
    draft = await db.get(AgentAssemblyVersion, agent.draft_version_id)
    selected = await _selection(db, draft.id)
    latest_no = None
    if agent.current_published_version_id:
        latest = await db.get(AgentAssemblyVersion, agent.current_published_version_id)
        latest_no = latest.version_no if latest else None
    summary = AgentSummaryV1(
        id=agent.id, tenant_id=agent.tenant_id,
        owner_account_id=agent.owner_account_id, visibility=agent.visibility,
        status=agent.status, version=agent.version, draft_version_id=draft.id,
        current_published_version_id=agent.current_published_version_id,
        name=draft.name, description=draft.description, group_enabled=draft.group_enabled,
        latest_published_version_no=latest_no, created_at=agent.created_at, updated_at=agent.updated_at,
    )
    draft_response = AgentDraftResponse(
        id=draft.id, agent_id=agent.id, draft_revision=draft.draft_revision,
        derived_from_version_id=draft.derived_from_version_id, name=draft.name,
        description=draft.description, instructions=draft.instructions, model_id=draft.model_id,
        sandbox_profile_id=draft.sandbox_profile_id, context_policy_id=draft.context_policy_id,
        history_policy_id=draft.history_policy_id, memory_write_policy_id=draft.memory_write_policy_id,
        memory_recall_policy_id=draft.memory_recall_policy_id, group_enabled=draft.group_enabled,
        discovery_config=dict(draft.discovery_config or {}),
        collaboration_config=dict(draft.collaboration_config or {}),
        tool_ids=list(selected.tool_ids), skill_ids=list(selected.skill_ids),
        bound_agent_ids=list(selected.bound_agent_ids), updated_at=draft.updated_at,
    )
    return AgentDetailV1(agent=summary, draft=draft_response)


async def _published_version_response(
    db: AsyncSession, version: AgentAssemblyVersion,
) -> PublishedVersionResponse:
    tools = list(await db.execute(
        select(
            AgentVersionTool.platform_tool_id,
            AgentVersionTool.tool_version_id,
            AgentVersionTool.tool_version_content_hash,
            AgentVersionTool.connection_revision_id,
            AgentVersionTool.secret_fingerprint,
        )
        .where(AgentVersionTool.agent_version_id == version.id)
        .order_by(AgentVersionTool.sort_order)
    ))
    skills = list(await db.execute(
        select(
            AgentVersionSkill.skill_id,
            AgentVersionSkill.skill_version_id,
            AgentVersionSkill.checksum,
        )
        .where(AgentVersionSkill.agent_version_id == version.id)
        .order_by(AgentVersionSkill.sort_order)
    ))
    card = await db.scalar(
        select(AgentCard).where(AgentCard.agent_version_id == version.id)
    )
    return PublishedVersionResponse(
        id=version.id,
        version_no=version.version_no,
        publication_status=version.publication_status,
        content_hash=version.content_hash,
        published_at=version.published_at,
        model_version_id=version.model_version_id,
        context_policy_version_id=version.context_policy_version_id,
        history_policy_version_id=version.history_policy_version_id,
        memory_write_policy_version_id=version.memory_write_policy_version_id,
        memory_recall_policy_version_id=version.memory_recall_policy_version_id,
        sandbox_profile_version_id=version.sandbox_profile_version_id,
        tools=[
            {
                "tool_definition_id": item[0], "tool_version_id": item[1],
                "content_hash": item[2], "connection_revision_id": item[3],
                "secret_fingerprint": item[4],
            } for item in tools
        ],
        skills=[
            {"skill_id": item[0], "skill_version_id": item[1], "checksum": item[2]}
            for item in skills
        ],
        agent_card_content_hash=card.content_hash if card else None,
    )


async def _summary(
    db: AsyncSession, agent: AgentBlueprint, current: CurrentAccount,
) -> AgentSummaryV1:
    can_manage = "admin" in current.roles or (
        agent.owner_account_id == current.account.id and "builder" in current.roles
    )
    version_id = agent.draft_version_id if can_manage else agent.current_published_version_id
    presentation = await db.get(AgentAssemblyVersion, version_id) if version_id else None
    if presentation is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    latest_no = None
    if agent.current_published_version_id:
        latest = await db.get(AgentAssemblyVersion, agent.current_published_version_id)
        latest_no = latest.version_no if latest else None
    return AgentSummaryV1(
        id=agent.id, tenant_id=agent.tenant_id,
        owner_account_id=agent.owner_account_id, visibility=agent.visibility,
        status=agent.status, version=agent.version, draft_version_id=agent.draft_version_id,
        current_published_version_id=agent.current_published_version_id,
        name=presentation.name, description=presentation.description,
        group_enabled=presentation.group_enabled,
        latest_published_version_no=latest_no,
        created_at=agent.created_at, updated_at=agent.updated_at,
    )


async def _get_accessible_agent(
    db: AsyncSession, agent_id: str, current: CurrentAccount,
) -> AgentBlueprint:
    agent = await db.scalar(select(AgentBlueprint).where(
        AgentBlueprint.id == agent_id,
        AgentBlueprint.tenant_id == current.tenant_id,
    ))
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if (
        agent.owner_account_id != current.account.id
        and "admin" not in current.roles
        and not (agent.visibility == "enterprise" and agent.status == "published")
    ):
        raise HTTPException(status_code=403, detail="Access denied")
    return agent


def _api_key_response(key: AgentApiKey, secret: str | None = None) -> AgentApiKeyResponse:
    return AgentApiKeyResponse(
        id=key.id, tenant_id=key.tenant_id, agent_id=key.agent_id,
        name=key.name, caller_label=key.caller_label,
        prefix=key.prefix, status=key.status, expires_at=key.expires_at,
        last_used_at=key.last_used_at, rotated_from_id=key.rotated_from_id,
        version=key.version, created_at=key.created_at, secret=secret,
    )


async def _idempotency_record(
    db: AsyncSession, *, current: CurrentAccount, operation: str,
    key: str, request: dict,
) -> tuple[IdempotencyRecord, bool]:
    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    record = await db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.principal_type == "account",
        IdempotencyRecord.principal_id == current.account.id,
        IdempotencyRecord.operation == operation,
        IdempotencyRecord.idempotency_key == key,
    ))
    if record is not None:
        if record.request_hash != request_hash:
            raise HTTPException(status_code=409, detail={"code": "idempotency_mismatch"})
        return record, True
    record = IdempotencyRecord(
        id=uuid7(), principal_type="account", principal_id=current.account.id,
        operation=operation, idempotency_key=key, request_hash=request_hash,
        expires_at=utcnow() + timedelta(days=7),
    )
    db.add(record)
    await db.flush()
    return record, False


@router.get("", response_model=dict)
async def list_agents(
    status: str | None = None,
    visibility: str | None = None,
    owner: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    statement = select(AgentBlueprint).where(
        AgentBlueprint.tenant_id == current.tenant_id,
    )
    if "admin" not in current.roles and "builder" in current.roles:
        statement = statement.where(or_(
            AgentBlueprint.owner_account_id == current.account.id,
            (AgentBlueprint.visibility == "enterprise") & (AgentBlueprint.status == "published"),
        ))
    elif "admin" not in current.roles:
        statement = statement.where(
            AgentBlueprint.status == "published",
            or_(
                AgentBlueprint.owner_account_id == current.account.id,
                AgentBlueprint.visibility == "enterprise",
            ),
        )
    if status:
        statement = statement.where(AgentBlueprint.status == status)
    if visibility:
        statement = statement.where(AgentBlueprint.visibility == visibility)
    if owner:
        owner_id = current.account.id if owner == "me" else owner
        if owner_id != current.account.id and "admin" not in current.roles:
            raise HTTPException(status_code=403, detail="Owner filter not allowed")
        statement = statement.where(AgentBlueprint.owner_account_id == owner_id)
    filters = {"status": status, "visibility": visibility, "owner": owner}
    agents, next_cursor = await paginate_scalars(
        db, statement,
        sort=(SortKey(AgentBlueprint.updated_at), SortKey(AgentBlueprint.id)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource="agents", filters=filters,
    )
    return {
        "items": [(await _summary(db, agent, current)).model_dump(mode="json") for agent in agents],
        "next_cursor": next_cursor,
    }


@router.post("", response_model=AgentDetailV1, status_code=201)
async def create_agent(
    body: AgentCreateV1,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    draft_input = body.draft
    agent, draft = await create_agent_domain(
        db, tenant_id=current.tenant_id,
        owner_account_id=current.account.id, created_by_account_id=current.account.id,
        name=draft_input.name, description=draft_input.description, visibility=body.visibility,
        initial_values=draft_input.model_dump(exclude={"tool_ids", "skill_ids", "bound_agent_ids", "name", "description"}),
        selection=AssemblySelection(tuple(draft_input.tool_ids), tuple(draft_input.skill_ids), tuple(draft_input.bound_agent_ids)),
    )
    await db.commit()
    return await _detail(db, agent)


@router.get("/{agent_id}/summary", response_model=AgentSummaryV1)
async def get_agent_summary(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    return await _summary(db, await _get_accessible_agent(db, agent_id, current), current)


@router.get("/{agent_id}", response_model=AgentDetailV1)
async def get_agent(agent_id: str, db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder)):
    return await _detail(db, await _get_owned_agent(db, agent_id, current))


@router.get("/{agent_id}/draft", response_model=AgentDraftResponse)
async def get_draft(agent_id: str, db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder)):
    return (await _detail(db, await _get_owned_agent(db, agent_id, current))).draft


@router.patch("/{agent_id}/draft", response_model=AgentDraftResponse)
async def patch_draft(
    agent_id: str, body: AgentDraftPatch, if_match: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    await _get_owned_agent(db, agent_id, current)
    dumped = body.model_dump(exclude_unset=True)
    selection_keys = {"tool_ids", "skill_ids", "bound_agent_ids"}
    selection = None
    if selection_keys.intersection(dumped):
        current_detail = await _detail(db, await db.get(AgentBlueprint, agent_id))
        selection = AssemblySelection(
            tuple(dumped.pop("tool_ids", current_detail.draft.tool_ids)),
            tuple(dumped.pop("skill_ids", current_detail.draft.skill_ids)),
            tuple(dumped.pop("bound_agent_ids", current_detail.draft.bound_agent_ids)),
        )
    try:
        await update_agent_draft(db, agent_id=agent_id, expected_draft_revision=_if_match_revision(if_match), values=dumped, selection=selection)
        await db.commit()
    except AgentDraftConflict:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "draft_revision_conflict"})
    except ValueError as error:
        await db.rollback()
        raise HTTPException(status_code=422, detail={"code": "invalid_agent_assembly", "message": str(error)})
    return (await _detail(db, await db.get(AgentBlueprint, agent_id))).draft


@router.get("/{agent_id}/draft/capability-check", response_model=CapabilityCheckResponse)
async def check_agent_draft_capabilities(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    agent = await _get_owned_agent(db, agent_id, current)
    draft = await db.get(AgentAssemblyVersion, agent.draft_version_id)
    selection = await _selection(db, draft.id)
    issues, resolved = await check_publish_capabilities(db, draft, selection)
    candidate_hash = None
    if resolved is not None:
        candidate_hash = assembly_content_hash(draft, selection, resolved)
        issues.extend(await agent_eval_gate_issues(
            db, agent_id=agent.id, candidate_content_hash=candidate_hash,
        ))
    resolved_snapshot = resolved.safe_snapshot() if resolved else None
    if resolved_snapshot is not None:
        resolved_snapshot["candidate_content_hash"] = candidate_hash
    return CapabilityCheckResponse(
        publishable=not issues,
        issues=issues,
        resolved_dependencies=resolved_snapshot,
    )


@router.post("/{agent_id}/publish", response_model=AgentPublishResponse)
async def publish_agent(
    agent_id: str, body: PublishAgentRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    agent = await _get_owned_agent(db, agent_id, current)
    operation = "publish_agent"
    request_hash = hashlib.sha256(
        json.dumps(
            {"agent_id": agent_id, "expected_draft_revision": body.expected_draft_revision},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    record = await db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.principal_type == "account",
            IdempotencyRecord.principal_id == current.account.id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
    )
    if record is not None:
        if record.request_hash != request_hash:
            raise HTTPException(status_code=409, detail={"code": "idempotency_mismatch"})
        if record.response_snapshot is not None:
            return AgentPublishResponse.model_validate(record.response_snapshot)
    else:
        record = IdempotencyRecord(
            id=uuid7(), principal_type="account", principal_id=current.account.id,
            operation=operation, idempotency_key=idempotency_key, request_hash=request_hash,
            expires_at=utcnow() + timedelta(days=7),
        )
        db.add(record)
        await db.flush()
    try:
        published = await publish_agent_domain(
            db, agent_id=agent_id, expected_draft_revision=body.expected_draft_revision,
            actor_account_id=current.account.id, idempotency_record_id=record.id,
        )
    except AgentDraftConflict:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "draft_revision_conflict"})
    except AgentPublishValidationError as error:
        await db.rollback()
        raise HTTPException(status_code=422, detail={"code": "agent_not_publishable", "issues": error.issues})
    detail = await _detail(db, agent)
    response = AgentPublishResponse(
        agent=detail.agent, draft=detail.draft,
        published_version=await _published_version_response(db, published),
    )
    record.resource_type = "agent_version"
    record.resource_id = published.id
    record.http_status = 200
    record.response_snapshot = response.model_dump(mode="json")
    await db.commit()
    return response


@router.get("/{agent_id}/versions", response_model=dict)
async def list_versions(
    agent_id: str, publication_status: str | None = None,
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    await _get_owned_agent(db, agent_id, current)
    allowed = {"published", "deprecated"}
    if publication_status and publication_status not in allowed:
        raise HTTPException(status_code=422, detail={"code": "invalid_publication_status"})
    query = select(AgentAssemblyVersion).where(
        AgentAssemblyVersion.agent_id == agent_id,
        AgentAssemblyVersion.publication_status.in_(("published", "deprecated")),
    )
    if publication_status:
        query = query.where(AgentAssemblyVersion.publication_status == publication_status)
    versions, next_cursor = await paginate_scalars(
        db, query,
        sort=(SortKey(AgentAssemblyVersion.version_no), SortKey(AgentAssemblyVersion.id)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource=f"agent:{agent_id}:versions",
        filters={"publication_status": publication_status},
    )
    return {
        "items": [
            (await _published_version_response(db, version)).model_dump(mode="json")
            for version in versions
        ],
        "next_cursor": next_cursor,
    }


@router.get("/{agent_id}/versions/{version_id}", response_model=AgentVersionDetailResponse)
async def get_version(
    agent_id: str, version_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    await _get_owned_agent(db, agent_id, current)
    version = await db.get(AgentAssemblyVersion, version_id)
    if version is None or version.agent_id != agent_id or version.publication_status == "draft":
        raise HTTPException(status_code=404, detail="Agent version not found")
    selected = await _selection(db, version.id)
    frozen = await _published_version_response(db, version)
    return AgentVersionDetailResponse(
        id=version.id, agent_id=version.agent_id, version_no=version.version_no,
        publication_status=version.publication_status, content_hash=version.content_hash,
        derived_from_version_id=version.derived_from_version_id,
        published_at=version.published_at, created_at=version.created_at, updated_at=version.updated_at,
        name=version.name, description=version.description, instructions=version.instructions,
        model_id=version.model_id, sandbox_profile_id=version.sandbox_profile_id,
        context_policy_id=version.context_policy_id, history_policy_id=version.history_policy_id,
        memory_write_policy_id=version.memory_write_policy_id,
        memory_recall_policy_id=version.memory_recall_policy_id,
        group_enabled=version.group_enabled, tool_ids=list(selected.tool_ids),
        skill_ids=list(selected.skill_ids), bound_agent_ids=list(selected.bound_agent_ids),
        discovery_config=dict(version.discovery_config or {}),
        collaboration_config=dict(version.collaboration_config or {}),
        model_version_id=version.model_version_id,
        context_policy_version_id=version.context_policy_version_id,
        history_policy_version_id=version.history_policy_version_id,
        memory_write_policy_version_id=version.memory_write_policy_version_id,
        memory_recall_policy_version_id=version.memory_recall_policy_version_id,
        sandbox_profile_version_id=version.sandbox_profile_version_id,
        frozen_tools=frozen.tools,
        frozen_skills=frozen.skills,
        agent_card_content_hash=frozen.agent_card_content_hash,
    )


@router.post("/{agent_id}/draft/test-runs", response_model=ChatCommandResponse, status_code=202)
async def create_draft_test_run(
    agent_id: str, body: DraftTestRunRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    agent = await _get_owned_agent(db, agent_id, current)
    record, replay = await _idempotency_record(
        db, current=current, operation="create_draft_test_run", key=idempotency_key,
        request={"agent_id": agent_id, **body.model_dump(mode="json")},
    )
    if replay and record.response_snapshot:
        return ChatCommandResponse.model_validate(record.response_snapshot)
    try:
        snapshot = await create_draft_test_snapshot(
            db, agent_id=agent_id,
            expected_draft_revision=body.expected_draft_revision,
            actor_account_id=current.account.id, idempotency_record_id=record.id,
        )
        if body.session_id:
            session = await db.get(UserSession, body.session_id)
            if (
                session is None
                or session.tenant_id != current.tenant_id
                or session.account_id != current.account.id
                or session.user_id != current.account.user_id
                or session.entry_agent_id != agent_id
            ):
                raise LookupError("session not found")
        else:
            session = await create_user_session(
                db, tenant_id=current.tenant_id, account_id=current.account.id,
                user_id=current.account.user_id, entry_agent_id=agent_id,
                origin_type="web", title=f"Draft test · {snapshot.name}",
                allow_unpublished=True, agent_version_id=snapshot.id,
            )
        message, run, _, _ = await enqueue_chat_message(
            db, session_id=session.id, user_id=current.account.user_id,
            content=body.content, idempotency_record_id=record.id,
            agent_version_id=snapshot.id, execution_kind="draft_test",
        )
    except AgentDraftConflict:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "draft_revision_conflict"})
    except AgentPublishValidationError as error:
        await db.rollback()
        raise HTTPException(status_code=422, detail={"code": "draft_not_testable", "issues": error.issues})
    except (LookupError, ValueError, RuntimeError) as error:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "draft_test_rejected", "message": str(error)})
    response = ChatCommandResponse(
        message=MessageResponse.model_validate(message), run=RunResponse.model_validate(run),
        required_actions=[], links={"events": f"/api/v1/sessions/{session.id}/events/stream"},
    )
    record.resource_type, record.resource_id, record.http_status = "run", run.id, 202
    record.response_snapshot = response.model_dump(mode="json")
    await db.commit()
    return response


async def _change_agent_state(
    *, agent_id: str, target: str, body: AgentStateCommand, idempotency_key: str,
    db: AsyncSession, current: CurrentAccount,
) -> AgentSummaryV1:
    agent = await _get_owned_agent(db, agent_id, current)
    if target != "inactive" and agent.current_published_version_id is None:
        raise HTTPException(status_code=409, detail={"code": "agent_has_no_published_version"})
    record, replay = await _idempotency_record(
        db, current=current, operation=f"agent_{target}", key=idempotency_key,
        request={"agent_id": agent_id, "target": target, "reason": body.reason},
    )
    if replay and record.response_snapshot:
        return AgentSummaryV1.model_validate(record.response_snapshot)
    if target == "inactive":
        agent.status = "inactive"
        action = "agent.deactivated"
    else:
        agent.status = "published"
        action = "agent.reactivated"
    agent.version += 1
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="agent", action=action, actor_type="account", actor_id=current.account.id,
        actor_user_id=None, resource_type="agent", resource_id=agent.id,
        resource_version=agent.version, outcome="succeeded", summary=action.replace(".", " "),
        changes={"status": agent.status, "reason_present": bool(body.reason)},
        dedupe_key=f"audit:{action}:{record.id}",
    )
    summary = (await _detail(db, agent)).agent
    record.resource_type = "agent"
    record.resource_id = agent.id
    record.http_status = 200
    record.response_snapshot = summary.model_dump(mode="json")
    await db.commit()
    return summary


@router.post("/{agent_id}/deactivate", response_model=AgentSummaryV1)
async def deactivate_agent(
    agent_id: str, body: AgentStateCommand,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    return await _change_agent_state(agent_id=agent_id, target="inactive", body=body, idempotency_key=idempotency_key, db=db, current=current)


@router.post("/{agent_id}/reactivate", response_model=AgentSummaryV1)
async def reactivate_agent(
    agent_id: str, body: AgentStateCommand,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    return await _change_agent_state(agent_id=agent_id, target="published", body=body, idempotency_key=idempotency_key, db=db, current=current)


@router.get("/{agent_id}/api-keys", response_model=dict)
async def list_agent_api_keys(
    agent_id: str, limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    await _get_owned_agent(db, agent_id, current)
    keys, next_cursor = await paginate_scalars(
        db, select(AgentApiKey).where(AgentApiKey.agent_id == agent_id),
        sort=(SortKey(AgentApiKey.created_at), SortKey(AgentApiKey.id)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource=f"agent:{agent_id}:api-keys", filters={},
    )
    return {"items": [_api_key_response(key).model_dump(mode="json") for key in keys],
            "next_cursor": next_cursor}


@router.post("/{agent_id}/api-keys", response_model=AgentApiKeyResponse, status_code=201)
async def create_agent_api_key(
    agent_id: str, body: AgentApiKeyCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    await _get_owned_agent(db, agent_id, current)
    record, replay = await _idempotency_record(
        db, current=current, operation="create_agent_api_key", key=idempotency_key,
        request={"agent_id": agent_id, **body.model_dump(mode="json")},
    )
    if replay and record.response_snapshot:
        return AgentApiKeyResponse.model_validate(record.response_snapshot)
    key, plaintext = await create_api_key(
        db, agent_id=agent_id, tenant_id=current.tenant_id,
        name=body.name, caller_label=body.caller_label,
        created_by_account_id=current.account.id, expires_at=body.expires_at,
        idempotency_record_id=record.id,
    )
    response = _api_key_response(key, plaintext)
    record.resource_type = "agent_api_key"
    record.resource_id = key.id
    record.http_status = 201
    record.response_snapshot = response.model_dump(mode="json")
    await db.commit()
    return response


@router.post("/{agent_id}/api-keys/{key_id}/rotate", response_model=AgentApiKeyResponse, status_code=201)
async def rotate_agent_api_key(
    agent_id: str, key_id: str, body: AgentApiKeyCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    await _get_owned_agent(db, agent_id, current)
    old = await db.get(AgentApiKey, key_id)
    if old is None or old.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="API key not found")
    record, replay = await _idempotency_record(
        db, current=current, operation="rotate_agent_api_key", key=idempotency_key,
        request={"agent_id": agent_id, "key_id": key_id, **body.model_dump(mode="json")},
    )
    if replay and record.response_snapshot:
        return AgentApiKeyResponse.model_validate(record.response_snapshot)
    new_key, plaintext = await create_api_key(
        db, agent_id=agent_id, tenant_id=current.tenant_id,
        name=body.name, caller_label=body.caller_label,
        created_by_account_id=current.account.id, expires_at=body.expires_at,
        idempotency_record_id=record.id, rotated_from_id=old.id,
    )
    response = _api_key_response(new_key, plaintext)
    record.resource_type = "agent_api_key"
    record.resource_id = new_key.id
    record.http_status = 201
    record.response_snapshot = response.model_dump(mode="json")
    await db.commit()
    return response


@router.delete("/{agent_id}/api-keys/{key_id}", status_code=204)
async def delete_agent_api_key(
    agent_id: str, key_id: str, idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    await _get_owned_agent(db, agent_id, current)
    key = await db.get(AgentApiKey, key_id)
    if key is None or key.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="API key not found")
    record, replay = await _idempotency_record(
        db, current=current, operation="revoke_agent_api_key", key=idempotency_key,
        request={"agent_id": agent_id, "key_id": key_id},
    )
    if not replay:
        await revoke_api_key(db, key_id=key_id, actor_account_id=current.account.id, idempotency_record_id=record.id)
        record.resource_type = "agent_api_key"
        record.resource_id = key_id
        record.http_status = 204
        record.response_snapshot = {"revoked": True}
        await db.commit()
    return None
