"""Evaluation evidence must remain scoped, immutable, and comparable."""
from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.agent_evals import router
from app.database import Base, get_db
from app.deps import CurrentAccount, get_current_builder
from app.models.agent_eval_v1 import AgentEvalCase, AgentEvalCaseRun, AgentEvalRun
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, UserIdentity, UserSession
from app.models.runtime_v1 import Message, Run, ToolInvocation, WorkItem
from app.services.agent_eval_gate_service import agent_eval_gate_issues
from app.services.agent_eval_service import (
    create_eval_suite, evaluate_response, normalize_assertions, normalize_cases,
    normalize_gate_policy, reconcile_one_eval_case,
)
from app.services.agent_eval_workbench import compare_eval_results

pytestmark = pytest.mark.unit


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def seed(db):
    user, account, agent, version, session, run, work = [uuid7() for _ in range(7)]
    owner = PlatformAccount(id=account, user_id=user, display_name="owner", status="active", auth_revision=1)
    db.add_all([
        UserIdentity(user_id=user, status="active"), owner,
        AgentBlueprint(id=agent, owner_account_id=account, visibility="private", status="published", next_version_no=2),
        AgentAssemblyVersion(id=version, agent_id=agent, version_no=1, publication_status="published", name="agent", created_by_account_id=account, content_hash="a" * 64),
        UserSession(id=session, user_id=user, account_id=account, entry_agent_id=agent, status="active", origin_type="web", next_event_sequence=1, permission_mode="direct", permission_revision=1, interaction_mode="chat", interaction_revision=1),
        Run(id=run, session_id=session, user_id=user, account_id=account, root_work_item_id=work, mode="chat", status="completed", permission_mode="direct", hard_budget_snapshot={}, completed_at=utcnow()),
        WorkItem(id=work, run_id=run, session_id=session, user_id=user, mode="chat", owner_agent_id=agent, agent_version_id=version, goal="Search and cite sources", status="succeeded"),
        Message(id=uuid7(), session_id=session, user_id=user, scope_type="chat", sender_type="agent", sender_agent_id=agent, recipient_type="session", role="assistant", content="Verified result", status="committed", content_format="markdown", run_id=run),
    ])
    await db.flush()
    return CurrentAccount(owner, frozenset({"builder"})), agent, run


def api(db, current):
    app = FastAPI()
    app.include_router(router)
    async def database():
        yield db
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_builder] = lambda: current
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def tool(db, run_id, key, status):
    run = await db.get(Run, run_id)
    db.add(ToolInvocation(
        id=uuid7(), runner_id=uuid7(), work_item_id=run.root_work_item_id,
        run_id=run_id, session_id=run.session_id, platform_tool_id=uuid7(),
        tool_version_id=uuid7(), tool_version_content_hash="b" * 64,
        tool_key=key, execution_mode="inline", status=status, arguments={},
        arguments_hash="b" * 64, business_idempotency_key=uuid7(), principal_hash="c" * 64,
    ))
    await db.flush()


@pytest.mark.asyncio
async def test_import_verifies_permissions_and_persists_provenance_in_new_versions(db):
    current, agent, run_id = await seed(db)
    await tool(db, run_id, "web_search", "failed")
    async with api(db, current) as client:
        source = (await client.get(f"/agents/{agent}/eval-sources/{run_id}")).json()
        assert source["input_text"] == "Search and cite sources"
        assert source["assertions"] == {"min_chars": 1}  # Bad observed behavior is not gold.
        assert source["source_snapshot"]["tools"] == [{"key": "web_search", "status": "failed"}]
        definition = {"name": "Search quality", "gate_policy": {"required_for_publish": False}, "cases": [{**source, "tags": ["search"], "assertions": {"require_successful_tools": ["web_search"]}}]}
        response = await client.post(f"/agents/{agent}/eval-suites", json=definition, headers={"Idempotency-Key": "create"})
        assert response.status_code == 201, response.text
        suite = response.json()
        assert suite["cases"][0]["source_snapshot"]["content_hash"] == "a" * 64
        assert suite["cases"][0]["tags"] == ["search"]
        assert not await agent_eval_gate_issues(db, agent_id=agent, candidate_content_hash="new")
        replay = await client.post(f"/agents/{agent}/eval-suites", json=definition, headers={"Idempotency-Key": "create"})
        assert replay.json()["id"] == suite["id"]

        definition["expected_version"] = suite["version"]
        definition["cases"][0]["input_text"] = "Self-contained revised question"
        revised = await client.post(f"/agents/{agent}/eval-suites/{suite['id']}/versions", json=definition, headers={"Idempotency-Key": "revise"})
        assert revised.status_code == 200, revised.text
        assert revised.json()["current_version_no"] == 2
        assert revised.json()["content_hash"] != suite["content_hash"]
        assert revised.json()["cases"][0]["source_snapshot"]["input_text"] == source["input_text"]
        stale = await client.post(f"/agents/{agent}/eval-suites/{suite['id']}/versions", json=definition, headers={"Idempotency-Key": "stale"})
        assert stale.status_code == 409
        old_case = await db.get(AgentEvalCase, suite["cases"][0]["id"])
        assert old_case.input_text == source["input_text"]

        runtime = await db.get(Run, run_id)
        for field, value in (("account_id", "another-account"), ("user_id", "another-user"), ("tenant_id", "another-tenant"), ("mode", "group")):
            old = getattr(runtime, field)
            setattr(runtime, field, value)
            await db.flush()
            assert (await client.get(f"/agents/{agent}/eval-sources/{run_id}")).status_code == 404
            assert (await client.get(f"/agents/{agent}/eval-sources")).json()["items"] == []
            setattr(runtime, field, old)
            await db.flush()


@pytest.mark.asyncio
async def test_tool_presence_success_and_forbidden_are_different_facts(db):
    _, _, run_id = await seed(db)
    await tool(db, run_id, "web_search", "failed")
    await tool(db, run_id, "write_file", "rejected")
    results = await evaluate_response(db, run_id=run_id, response="text", assertions={
        "require_tools": ["web_search"], "require_successful_tools": ["web_search"], "forbid_tools": ["write_file", "browser"],
    })
    assert [item["passed"] for item in results] == [True, False, False, True]
    await tool(db, run_id, "web_search", "succeeded")
    assert (await evaluate_response(db, run_id=run_id, response="", assertions={"require_successful_tools": ["web_search"]}))[0]["passed"]


@pytest.mark.asyncio
async def test_scoring_uses_root_user_delivery_not_latest_child_message(db):
    current, agent, run_id = await seed(db)
    suite, version = await create_eval_suite(db, agent_id=agent, actor_account_id=current.account.id, name="Gold", description="", gate_policy={}, cases=[{
        "case_key": "case", "name": "case", "input_text": "q", "assertions": {"contains_all": ["Verified"]},
    }])
    case = await db.scalar(select(AgentEvalCase).where(AgentEvalCase.suite_version_id == version.id))
    runtime = await db.get(Run, run_id)
    db.add(Message(id=uuid7(), session_id=runtime.session_id, user_id=runtime.user_id, scope_type="chat", sender_type="agent", sender_agent_id="child", recipient_type="session", role="assistant", content="Wrong child result", status="committed", content_format="markdown", run_id=run_id))
    evaluation = AgentEvalRun(id=uuid7(), agent_id=agent, suite_version_id=version.id, candidate_agent_version_id=uuid7(), candidate_content_hash="a" * 64, status="queued", case_count=1, gate_policy_snapshot={"min_pass_rate": "1"}, created_by_account_id=current.account.id)
    db.add(evaluation)
    db.add(AgentEvalCaseRun(id=uuid7(), eval_run_id=evaluation.id, eval_case_id=case.id, session_id=runtime.session_id, run_id=run_id, status="queued"))
    await db.flush()
    assert await reconcile_one_eval_case(db)
    assert evaluation.status == "passed"
    assert not await reconcile_one_eval_case(db)
    async with api(db, current) as client:
        result = await client.get(f"/agents/{agent}/eval-runs/{evaluation.id}")
        assert result.status_code == 200, result.text
        item = result.json()["case_runs"][0]
        assert item["response_text"] == "Verified result"
        assert item["runtime_status"] == "completed"


def test_comparison_pairs_exact_versions_and_reports_tag_regressions():
    baseline = {"id": "old", "agent_id": "agent", "suite_version_id": "v1", "status": "passed", "candidate_content_hash": "a", "score": "1", "case_runs": [
        {"case_key": "search", "name": "Search", "status": "passed", "tags": ["search"], "response_text": "old", "assertion_results": []},
    ]}
    candidate = deepcopy(baseline)
    candidate.update(id="new", candidate_content_hash="b", status="failed", score="0")
    candidate["case_runs"][0].update(status="failed", response_text="new")
    result = compare_eval_results(baseline, candidate)
    assert result["score_delta"] == "-1"
    assert result["regressed_count"] == 1
    assert result["tags"] == [{"tag": "search", "count": 1, "baseline_passed": 1, "candidate_passed": 0}]
    assert result["cases"][0]["baseline_response"] == "old"
    for field, value in (("suite_version_id", "v2"), ("agent_id", "another"), ("status", "running"), ("case_runs", [])):
        invalid = {**candidate, field: value}
        with pytest.raises(ValueError):
            compare_eval_results(baseline, invalid)


@pytest.mark.parametrize("assertions", [
    {"min_chars": 1.5}, {"min_chars": "NaN"},
    {"require_tools": "web_search"}, {"require_tools": [""]},
    {"require_tools": ["web_search"], "forbid_tools": ["web_search"]},
    {"max_cost": {"currency": "USD", "amount": "Infinity"}},
])
def test_invalid_contracts_rejected(assertions):
    with pytest.raises(ValueError):
        normalize_assertions(assertions)


def test_nonfinite_weights_thresholds_and_invalid_gate_types_rejected():
    for value in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            normalize_gate_policy({"min_pass_rate": value})
        with pytest.raises(ValueError):
            normalize_cases([{"case_key": "x", "name": "x", "input_text": "q", "assertions": {"min_chars": 1}, "weight": value}])
    with pytest.raises(ValueError):
        normalize_gate_policy({"required_for_publish": "false"})
    assert normalize_gate_policy({}) == {"min_pass_rate": "1"}
