from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import CurrentAccount, get_current_builder
from app.models.agent_eval_v1 import (
    AgentEvalCase, AgentEvalCaseRun, AgentEvalRun, AgentEvalSuite,
    AgentEvalSuiteVersion,
)
from app.models.agent_v1 import AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord
from app.models.runtime_v1 import Message, Run
from app.persistence.reliable_writes import PlatformAuditWriter
from app.schemas.agent_eval_v1 import (
    AgentEvalCaseResponse, AgentEvalCaseRunResponse, AgentEvalRunCreate,
    AgentEvalRunResponse, AgentEvalSuiteDefinition, AgentEvalSuiteResponse,
)
from app.services.agent_assembly_service import AgentDraftConflict, AgentPublishValidationError
from app.services.agent_eval_service import (
    create_eval_suite, create_eval_suite_version, start_eval_run,
)
from app.services.agent_eval_workbench import compare_eval_results, import_run_preview, source_runs_query


router = APIRouter(prefix="/agents", tags=["agent-evals"])


async def _owned_agent(
    db: AsyncSession, *, agent_id: str, current: CurrentAccount,
) -> AgentBlueprint:
    agent = await db.scalar(select(AgentBlueprint).where(
        AgentBlueprint.id == agent_id,
        AgentBlueprint.tenant_id == current.tenant_id,
    ))
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_account_id != current.account.id and "admin" not in current.roles:
        raise HTTPException(status_code=403, detail="Access denied")
    return agent


async def _owned_suite(
    db: AsyncSession, *, agent_id: str, suite_id: str, current: CurrentAccount,
    lock: bool = False,
) -> AgentEvalSuite:
    await _owned_agent(db, agent_id=agent_id, current=current)
    statement = select(AgentEvalSuite).where(
        AgentEvalSuite.id == suite_id, AgentEvalSuite.agent_id == agent_id,
    )
    if lock:
        statement = statement.with_for_update()
    suite = await db.scalar(statement)
    if suite is None:
        raise HTTPException(status_code=404, detail="Eval suite not found")
    return suite


async def _idempotency(
    db: AsyncSession, *, current: CurrentAccount, operation: str, key: str, request: dict,
) -> tuple[IdempotencyRecord, bool]:
    request_hash = hashlib.sha256(json.dumps(
        request, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()
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


async def _suite_response(
    db: AsyncSession, suite: AgentEvalSuite,
) -> AgentEvalSuiteResponse:
    version = await db.get(AgentEvalSuiteVersion, suite.current_version_id)
    if version is None:
        raise LookupError("eval suite current version missing")
    cases = list(await db.scalars(select(AgentEvalCase).where(
        AgentEvalCase.suite_version_id == version.id,
    ).order_by(AgentEvalCase.sort_order, AgentEvalCase.id)))
    return AgentEvalSuiteResponse(
        id=suite.id, agent_id=suite.agent_id, name=suite.name, status=suite.status,
        version=suite.version, current_version_id=version.id,
        current_version_no=version.version_no, description=version.description,
        gate_policy=dict(version.gate_policy or {}), content_hash=version.content_hash,
        cases=[AgentEvalCaseResponse(
            id=case.id, case_key=case.case_key, name=case.name,
            input_text=case.input_text, assertions=dict(case.assertions or {}),
            weight=case.weight, sort_order=case.sort_order,
            tags=(case.case_metadata or {}).get("tags", []),
            source_run_id=(case.case_metadata or {}).get("source_run_id"),
            source_snapshot=(case.case_metadata or {}).get("source_snapshot"),
        ) for case in cases],
        created_at=suite.created_at, updated_at=suite.updated_at,
    )


async def _eval_run_response(
    db: AsyncSession, eval_run: AgentEvalRun,
) -> AgentEvalRunResponse:
    version = await db.get(AgentEvalSuiteVersion, eval_run.suite_version_id)
    rows = list(await db.execute(select(AgentEvalCaseRun, AgentEvalCase, Message.content, Run.status).join(
        AgentEvalCase, AgentEvalCase.id == AgentEvalCaseRun.eval_case_id,
    ).outerjoin(Message, Message.id == AgentEvalCaseRun.response_message_id).outerjoin(
        Run, Run.id == AgentEvalCaseRun.run_id,
    ).where(AgentEvalCaseRun.eval_run_id == eval_run.id).order_by(
        AgentEvalCase.sort_order, AgentEvalCase.id,
    )))
    return AgentEvalRunResponse(
        id=eval_run.id, agent_id=eval_run.agent_id,
        suite_version_id=eval_run.suite_version_id,
        suite_name=version.name if version else "Unavailable suite",
        suite_version_no=version.version_no if version else 0,
        candidate_agent_version_id=eval_run.candidate_agent_version_id,
        candidate_content_hash=eval_run.candidate_content_hash,
        status=eval_run.status, case_count=eval_run.case_count,
        passed_count=eval_run.passed_count, failed_count=eval_run.failed_count,
        error_count=eval_run.error_count, score=eval_run.score,
        gate_policy_snapshot=dict(eval_run.gate_policy_snapshot or {}),
        case_runs=[AgentEvalCaseRunResponse(
            id=case_run.id, eval_case_id=case.id, case_key=case.case_key,
            name=case.name, session_id=case_run.session_id, run_id=case_run.run_id,
            status=case_run.status, score=case_run.score,
            response_message_id=case_run.response_message_id,
            assertion_results=list(case_run.assertion_results or []),
            failure_code=case_run.failure_code, started_at=case_run.started_at,
            ended_at=case_run.ended_at,
            input_text=case.input_text, response_text=response_text,
            tags=(case.case_metadata or {}).get("tags", []), runtime_status=runtime_status,
        ) for case_run, case, response_text, runtime_status in rows],
        started_at=eval_run.started_at, ended_at=eval_run.ended_at,
        created_at=eval_run.created_at, updated_at=eval_run.updated_at,
    )


async def _case_definitions(db, *, agent_id, current, body, suite=None):
    existing = {}
    if suite is not None:
        previous = list(await db.scalars(select(AgentEvalCase).where(
            AgentEvalCase.suite_version_id == suite.current_version_id,
        )))
        existing = {item.case_key: item.case_metadata or {} for item in previous}
    definitions = []
    for item in body.cases:
        definition = item.model_dump(mode="json")
        if item.source_run_id:
            old = existing.get(item.case_key, {})
            if old.get("source_run_id") == item.source_run_id and old.get("source_snapshot"):
                snapshot = old["source_snapshot"]
            else:
                try:
                    preview = await import_run_preview(
                        db, agent_id=agent_id, tenant_id=current.tenant_id,
                        account_id=current.account.id, user_id=current.account.user_id,
                        run_id=item.source_run_id,
                    )
                except LookupError as error:
                    raise HTTPException(status_code=404, detail=str(error))
                snapshot = preview["source_snapshot"]
            definition["case_metadata"] = {
                "source_run_id": item.source_run_id, "source_snapshot": snapshot,
            }
        definitions.append(definition)
    return definitions


@router.get("/{agent_id}/eval-sources", response_model=dict)
async def list_eval_sources(
    agent_id: str, limit: int = Query(default=30, ge=1, le=100),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    await _owned_agent(db, agent_id=agent_id, current=current)
    rows = await db.execute(source_runs_query(
        agent_id=agent_id, tenant_id=current.tenant_id,
        account_id=current.account.id, user_id=current.account.user_id,
    ).order_by(Run.created_at.desc(), Run.id.desc()).limit(limit))
    return {"items": [{
        "run_id": run.id, "session_id": run.session_id, "status": run.status,
        "input_preview": work.goal[:200], "created_at": run.created_at,
    } for run, work in rows]}


@router.get("/{agent_id}/eval-sources/{run_id}", response_model=dict)
async def preview_eval_source(
    agent_id: str, run_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    await _owned_agent(db, agent_id=agent_id, current=current)
    try:
        return await import_run_preview(
            db, agent_id=agent_id, tenant_id=current.tenant_id,
            account_id=current.account.id, user_id=current.account.user_id, run_id=run_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error))


@router.get("/{agent_id}/eval-comparison", response_model=dict)
async def compare_eval_runs(
    agent_id: str, baseline_id: str, candidate_id: str,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    await _owned_agent(db, agent_id=agent_id, current=current)
    responses = []
    for run_id in (baseline_id, candidate_id):
        run = await db.get(AgentEvalRun, run_id)
        if run is None or run.agent_id != agent_id:
            raise HTTPException(status_code=404, detail="Eval run not found")
        responses.append((await _eval_run_response(db, run)).model_dump(mode="json"))
    try:
        return compare_eval_results(*responses)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))


@router.get("/{agent_id}/eval-suites", response_model=dict)
async def list_eval_suites(
    agent_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    await _owned_agent(db, agent_id=agent_id, current=current)
    suites = list(await db.scalars(select(AgentEvalSuite).where(
        AgentEvalSuite.agent_id == agent_id,
    ).order_by(AgentEvalSuite.created_at, AgentEvalSuite.id)))
    return {"items": [
        (await _suite_response(db, suite)).model_dump(mode="json") for suite in suites
    ]}


@router.post("/{agent_id}/eval-suites", response_model=AgentEvalSuiteResponse, status_code=201)
async def create_suite(
    agent_id: str, body: AgentEvalSuiteDefinition,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    await _owned_agent(db, agent_id=agent_id, current=current)
    request = {"agent_id": agent_id, **body.model_dump(mode="json")}
    record, replay = await _idempotency(
        db, current=current, operation="create_agent_eval_suite",
        key=idempotency_key, request=request,
    )
    if replay and record.response_snapshot:
        return AgentEvalSuiteResponse.model_validate(record.response_snapshot)
    try:
        suite, _ = await create_eval_suite(
            db, agent_id=agent_id, actor_account_id=current.account.id,
            name=body.name, description=body.description,
            gate_policy=body.gate_policy,
            cases=await _case_definitions(db, agent_id=agent_id, current=current, body=body),
        )
    except ValueError as error:
        await db.rollback()
        raise HTTPException(status_code=422, detail={
            "code": "invalid_agent_eval_suite", "message": str(error),
        })
    response = await _suite_response(db, suite)
    record.resource_type, record.resource_id, record.http_status = "agent_eval_suite", suite.id, 201
    record.response_snapshot = response.model_dump(mode="json")
    await PlatformAuditWriter(db).append(
        category="agent", action="agent.eval_suite.created",
        actor_type="account", actor_id=current.account.id, actor_user_id=None,
        resource_type="agent_eval_suite", resource_id=suite.id,
        resource_version=suite.version, outcome="succeeded",
        summary=f"Created Agent eval suite {suite.name}",
        changes={"current_version_id": suite.current_version_id},
        dedupe_key=f"audit:create_agent_eval_suite:{record.id}",
    )
    await db.commit()
    return response


@router.post(
    "/{agent_id}/eval-suites/{suite_id}/versions",
    response_model=AgentEvalSuiteResponse,
)
async def create_suite_version(
    agent_id: str, suite_id: str, body: AgentEvalSuiteDefinition,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    suite = await _owned_suite(
        db, agent_id=agent_id, suite_id=suite_id, current=current, lock=True,
    )
    record, replay = await _idempotency(
        db, current=current, operation="version_agent_eval_suite", key=idempotency_key,
        request={"agent_id": agent_id, "suite_id": suite_id, **body.model_dump(mode="json")},
    )
    if replay and record.response_snapshot:
        return AgentEvalSuiteResponse.model_validate(record.response_snapshot)
    if body.expected_version is not None and body.expected_version != suite.version:
        raise HTTPException(status_code=409, detail="评测集已更新，请重新载入后编辑")
    try:
        version = await create_eval_suite_version(
            db, suite=suite, actor_account_id=current.account.id, name=body.name,
            description=body.description, gate_policy=body.gate_policy,
            cases=await _case_definitions(db, agent_id=agent_id, current=current, body=body, suite=suite),
        )
    except ValueError as error:
        await db.rollback()
        raise HTTPException(status_code=422, detail={
            "code": "invalid_agent_eval_suite", "message": str(error),
        })
    response = await _suite_response(db, suite)
    record.resource_type, record.resource_id, record.http_status = (
        "agent_eval_suite_version", version.id, 200
    )
    record.response_snapshot = response.model_dump(mode="json")
    await PlatformAuditWriter(db).append(
        category="agent", action="agent.eval_suite.versioned",
        actor_type="account", actor_id=current.account.id, actor_user_id=None,
        resource_type="agent_eval_suite", resource_id=suite.id,
        resource_version=suite.version, outcome="succeeded",
        summary=f"Published Agent eval suite version {version.version_no}",
        changes={"version_id": version.id, "content_hash": version.content_hash},
        dedupe_key=f"audit:version_agent_eval_suite:{record.id}",
    )
    await db.commit()
    return response


@router.post(
    "/{agent_id}/eval-suites/{suite_id}/state/{target}",
    response_model=AgentEvalSuiteResponse,
)
async def change_suite_state(
    agent_id: str, suite_id: str, target: str,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    if target not in {"activate", "deactivate"}:
        raise HTTPException(status_code=404, detail="Eval suite action not found")
    suite = await _owned_suite(
        db, agent_id=agent_id, suite_id=suite_id, current=current, lock=True,
    )
    desired = "active" if target == "activate" else "inactive"
    record, replay = await _idempotency(
        db, current=current, operation=f"{target}_agent_eval_suite",
        key=idempotency_key,
        request={"agent_id": agent_id, "suite_id": suite_id, "status": desired},
    )
    if replay and record.response_snapshot:
        return AgentEvalSuiteResponse.model_validate(record.response_snapshot)
    suite.status = desired
    suite.version += 1
    await db.flush()
    response = await _suite_response(db, suite)
    record.resource_type, record.resource_id, record.http_status = "agent_eval_suite", suite.id, 200
    record.response_snapshot = response.model_dump(mode="json")
    await PlatformAuditWriter(db).append(
        category="agent", action=f"agent.eval_suite.{target}d",
        actor_type="account", actor_id=current.account.id, actor_user_id=None,
        resource_type="agent_eval_suite", resource_id=suite.id,
        resource_version=suite.version, outcome="succeeded",
        summary=f"Set Agent eval suite {suite.name} to {desired}",
        changes={"status": desired},
        dedupe_key=f"audit:{target}_agent_eval_suite:{record.id}",
    )
    await db.commit()
    return response


@router.post(
    "/{agent_id}/eval-suites/{suite_id}/runs",
    response_model=AgentEvalRunResponse,
    status_code=202,
)
async def run_suite(
    agent_id: str, suite_id: str, body: AgentEvalRunCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    suite = await _owned_suite(
        db, agent_id=agent_id, suite_id=suite_id, current=current, lock=True,
    )
    record, replay = await _idempotency(
        db, current=current, operation="run_agent_eval_suite", key=idempotency_key,
        request={
            "agent_id": agent_id, "suite_id": suite_id,
            "expected_draft_revision": body.expected_draft_revision,
        },
    )
    if replay and record.resource_id:
        existing = await db.get(AgentEvalRun, record.resource_id)
        if existing:
            return await _eval_run_response(db, existing)
    try:
        eval_run = await start_eval_run(
            db, suite=suite, expected_draft_revision=body.expected_draft_revision,
            tenant_id=current.tenant_id, actor_account_id=current.account.id,
            actor_user_id=current.account.user_id,
            idempotency_record_id=record.id,
        )
    except AgentDraftConflict:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "draft_revision_conflict"})
    except AgentPublishValidationError as error:
        await db.rollback()
        raise HTTPException(status_code=422, detail={
            "code": "draft_not_evaluable", "issues": error.issues,
        })
    except (LookupError, ValueError, RuntimeError) as error:
        await db.rollback()
        raise HTTPException(status_code=409, detail={
            "code": "agent_eval_rejected", "message": str(error),
        })
    record.resource_type, record.resource_id, record.http_status = "agent_eval_run", eval_run.id, 202
    await PlatformAuditWriter(db).append(
        category="agent", action="agent.eval_run.started",
        actor_type="account", actor_id=current.account.id, actor_user_id=None,
        resource_type="agent_eval_run", resource_id=eval_run.id,
        outcome="succeeded", summary=f"Started {eval_run.case_count} Agent eval cases",
        changes={
            "suite_version_id": eval_run.suite_version_id,
            "candidate_content_hash": eval_run.candidate_content_hash,
        },
        dedupe_key=f"audit:run_agent_eval_suite:{record.id}",
    )
    await db.commit()
    return await _eval_run_response(db, eval_run)


@router.get("/{agent_id}/eval-runs", response_model=dict)
async def list_eval_runs(
    agent_id: str, limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    await _owned_agent(db, agent_id=agent_id, current=current)
    runs = list(await db.scalars(select(AgentEvalRun).where(
        AgentEvalRun.agent_id == agent_id,
    ).order_by(AgentEvalRun.created_at.desc(), AgentEvalRun.id.desc()).limit(min(max(limit, 1), 100))))
    return {"items": [
        (await _eval_run_response(db, item)).model_dump(mode="json") for item in runs
    ]}


@router.get("/{agent_id}/eval-runs/{eval_run_id}", response_model=AgentEvalRunResponse)
async def get_eval_run(
    agent_id: str, eval_run_id: str,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    await _owned_agent(db, agent_id=agent_id, current=current)
    eval_run = await db.get(AgentEvalRun, eval_run_id)
    if eval_run is None or eval_run.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Eval run not found")
    return await _eval_run_response(db, eval_run)
