"""Bound Agent edges and opt-in network edges share guarded delegation."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint, AgentCard, AgentVersionBinding
from app.models.common import utcnow
from app.models.foundation import UserSession
from app.models.runtime_v1 import AgentInvocation, DurableRunner, Run, WorkItem
from app.services.agent_invocation_service import AgentInvocationRejected, dispatch_agent_invocation
from app.workers.runtime_worker import RuntimeWorker

pytestmark = pytest.mark.unit


@pytest.fixture
async def collaboration_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        agents, versions, cards = {}, {}, {}
        for name in "abcd":
            agents[name] = AgentBlueprint(
                id=name, tenant_id="tenant", owner_account_id="account", visibility="enterprise",
                status="published", current_published_version_id=f"{name}-v1",
            )
            versions[name] = AgentAssemblyVersion(
                id=f"{name}-v1", agent_id=name, version_no=1, publication_status="published",
                name=name, group_enabled=False, immutable_at=utcnow(), created_by_account_id="account",
            )
            cards[name] = AgentCard(
                id=f"{name}-card", agent_id=name, agent_version_id=f"{name}-v1",
                name=name, visibility="enterprise", availability="available", group_enabled=False,
                collaboration_summary={"callable": True}, content_hash=name * 64, published_at=utcnow(),
            )
            db.add_all([agents[name], versions[name], cards[name]])
        session = UserSession(
            id="session", tenant_id="tenant", account_id="account", user_id="user", entry_agent_id="a",
        )
        run = Run(
            id="run", tenant_id="tenant", account_id="account", session_id="session", user_id="user",
            mode="chat", status="active", permission_mode="direct", root_work_item_id="root",
            hard_budget_snapshot={"max_agent_depth": 8},
        )
        work = WorkItem(
            id="root", tenant_id="tenant", run_id="run", session_id="session", user_id="user",
            mode="chat", owner_agent_id="a", agent_version_id="a-v1", goal="coordinate", status="running",
            active_runner_id="root-runner",
        )
        runner = DurableRunner(
            id="root-runner", tenant_id="tenant", work_item_id="root", run_id="run", session_id="session",
            agent_id="a", agent_version_id="a-v1", execution_epoch=1, status="running",
            principal_snapshot={"tenant_id": "tenant", "account_id": "account", "user_id": "user", "mode": "chat"},
        )
        db.add_all([session, run, work, runner])
        await db.flush()
        yield SimpleNamespace(db=db, agents=agents, versions=versions, cards=cards, session=session, run=run, work=work, runner=runner)
    await engine.dispose()


async def bind(fixture, source, target, *, version_id=None):
    version_id = version_id or fixture.versions[source].id
    order = int(await fixture.db.scalar(select(func.count()).select_from(AgentVersionBinding).where(
        AgentVersionBinding.agent_version_id == version_id,
    )) or 0)
    fixture.db.add(AgentVersionBinding(agent_version_id=version_id, bound_agent_id=target, sort_order=order))
    await fixture.db.flush()


async def invoke(fixture, target, *, caller=None, key=None):
    caller = caller or fixture.runner
    work = await fixture.db.get(WorkItem, caller.work_item_id)
    return await dispatch_agent_invocation(
        fixture.db, caller_runner=caller, caller_work=work, run=fixture.run,
        arguments={"agent_id": target, "task": f"Work for {target}"},
        operation_key=key or f"{caller.agent_id}-to-{target}", provider_call_id=key or f"{caller.agent_id}-to-{target}",
    )


async def target_runner(fixture, invocation):
    work = await fixture.db.get(WorkItem, invocation.target_work_item_id)
    return await fixture.db.get(DurableRunner, work.active_runner_id)


def mode(fixture, value):
    fixture.run.mode = value
    fixture.work.mode = value
    fixture.runner.principal_snapshot = {**fixture.runner.principal_snapshot, "mode": value}


@pytest.mark.parametrize("run_mode", ["chat", "plan"])
async def test_bound_edges_allow_multilevel_without_network_opt_in(collaboration_db, run_mode):
    f = collaboration_db
    mode(f, run_mode)
    await bind(f, "a", "b")
    await bind(f, "b", "c")
    first = await invoke(f, "b")
    b = await target_runner(f, first)
    second = await invoke(f, "c", caller=b)
    c = await target_runner(f, second)
    assert first.authorization_snapshot["collaboration_path"] == "bound_agent"
    assert second.authorization_snapshot["collaboration_path"] == "bound_agent"
    assert b.principal_snapshot["delegation_chain"] == ["a"]
    assert c.principal_snapshot["delegation_chain"] == ["a", "b"]
    assert c.principal_snapshot["account_id"] == f.run.account_id
    assert second.caller_work_item_id == b.work_item_id
    assert second.return_route == first.return_route == "caller"
    assert second.run_id == first.run_id == f.run.id


@pytest.mark.parametrize("run_mode", ["chat", "plan"])
@pytest.mark.parametrize("caller_network,target_network,expected", [
    (False, False, "agent_invocation_target_not_bound"),
    (False, True, "agent_invocation_target_not_bound"),
    (True, False, "agent_network_target_disabled"),
    (True, True, None),
])
async def test_unbound_edge_requires_both_network_opt_ins(collaboration_db, run_mode, caller_network, target_network, expected):
    f = collaboration_db
    mode(f, run_mode)
    f.versions["a"].group_enabled = caller_network
    f.cards["b"].group_enabled = target_network
    if expected:
        with pytest.raises(AgentInvocationRejected) as rejected:
            await invoke(f, "b")
        assert rejected.value.code == expected
    else:
        invocation = await invoke(f, "b")
        assert invocation.authorization_snapshot["collaboration_path"] == "agent_network"


@pytest.mark.parametrize("run_mode", ["chat", "plan"])
@pytest.mark.parametrize("bound", [True, False])
@pytest.mark.parametrize("denial", ["private", "callable", "nested_callable", "unavailable", "inactive", "version_unpublished", "missing_card"])
async def test_all_edges_recheck_original_user_acl_publication_and_governance(collaboration_db, run_mode, bound, denial):
    f = collaboration_db
    mode(f, run_mode)
    f.versions["a"].group_enabled = True
    f.cards["b"].group_enabled = True
    if bound:
        await bind(f, "a", "b")
    if denial == "private":
        f.agents["b"].visibility = f.cards["b"].visibility = "private"
        f.agents["b"].owner_account_id = "another-account"
        # A stale/forged child principal cannot replace the originating account.
        f.runner.principal_snapshot = {**f.runner.principal_snapshot, "account_id": "another-account"}
    elif denial == "callable":
        f.cards["b"].collaboration_summary = {"callable": False}
    elif denial == "nested_callable":
        f.cards["b"].collaboration_summary = {"governance": {"callable": False}}
    elif denial == "unavailable":
        f.cards["b"].availability = "unavailable"
    elif denial == "inactive":
        f.agents["b"].status = "inactive"
    elif denial == "version_unpublished":
        f.versions["b"].publication_status = "deprecated"
    else:
        await f.db.delete(f.cards["b"])
    with pytest.raises(AgentInvocationRejected):
        await invoke(f, "b")
    assert await f.db.scalar(select(func.count()).select_from(AgentInvocation)) == 0
    assert await f.db.scalar(select(func.count()).select_from(WorkItem)) == 1


@pytest.mark.parametrize("bound", [False, True])
async def test_group_allowed_policy_applies_only_to_network_edges(collaboration_db, bound):
    f = collaboration_db
    f.versions["a"].group_enabled = f.cards["b"].group_enabled = True
    f.cards["b"].collaboration_summary = {"governance": {"group_allowed": False}}
    if bound:
        await bind(f, "a", "b")
        assert (await invoke(f, "b")).authorization_snapshot["collaboration_path"] == "bound_agent"
    else:
        with pytest.raises(AgentInvocationRejected) as rejected:
            await invoke(f, "b")
        assert rejected.value.code == "agent_network_governance_denied"


@pytest.mark.parametrize("frozen_enabled,new_enabled", [(False, True), (True, False)])
async def test_network_caller_uses_frozen_version_not_latest_publication(collaboration_db, frozen_enabled, new_enabled):
    f = collaboration_db
    f.versions["a"].group_enabled = frozen_enabled
    f.cards["b"].group_enabled = True
    f.db.add(AgentAssemblyVersion(
        id="a-v2", agent_id="a", version_no=2, publication_status="published", name="a",
        group_enabled=new_enabled, immutable_at=utcnow(), created_by_account_id="account",
    ))
    f.agents["a"].current_published_version_id = "a-v2"
    if frozen_enabled:
        assert (await invoke(f, "b")).status == "queued"
    else:
        with pytest.raises(AgentInvocationRejected) as rejected:
            await invoke(f, "b")
        assert rejected.value.code == "agent_invocation_target_not_bound"


async def test_each_hop_uses_its_own_binding_set(collaboration_db):
    f = collaboration_db
    await bind(f, "a", "b")
    await bind(f, "a", "c")
    b = await target_runner(f, await invoke(f, "b"))
    with pytest.raises(AgentInvocationRejected) as rejected:
        await invoke(f, "c", caller=b)
    assert rejected.value.code == "agent_invocation_target_not_bound"


@pytest.mark.parametrize("run_mode", ["chat", "plan"])
async def test_cycles_and_frozen_depth_limit_apply_to_all_modes(collaboration_db, run_mode):
    f = collaboration_db
    mode(f, run_mode)
    f.run.hard_budget_snapshot = {"max_agent_depth": 2}
    await bind(f, "a", "b")
    await bind(f, "b", "a")
    await bind(f, "b", "c")
    await bind(f, "c", "d")
    b = await target_runner(f, await invoke(f, "b"))
    with pytest.raises(AgentInvocationRejected) as cycle:
        await invoke(f, "a", caller=b)
    assert cycle.value.code == "agent_invocation_cycle"
    c = await target_runner(f, await invoke(f, "c", caller=b))
    with pytest.raises(AgentInvocationRejected) as depth:
        await invoke(f, "d", caller=c)
    assert depth.value.code == "agent_invocation_max_depth"


async def test_old_run_depth_one_is_not_silently_relaxed(collaboration_db):
    f = collaboration_db
    f.run.hard_budget_snapshot = {"max_agent_depth": 1}
    await bind(f, "a", "b")
    await bind(f, "b", "c")
    b = await target_runner(f, await invoke(f, "b"))
    with pytest.raises(AgentInvocationRejected) as rejected:
        await invoke(f, "c", caller=b)
    assert rejected.value.code == "agent_invocation_max_depth"


async def test_run_wide_invocation_budget_is_shared_and_replay_is_not_recharged(collaboration_db):
    f = collaboration_db
    f.run.hard_budget_snapshot = {"max_agent_depth": 8, "max_agent_invocations": 1}
    await bind(f, "a", "b")
    await bind(f, "b", "c")
    first = await invoke(f, "b")
    assert (await invoke(f, "b")).id == first.id
    b = await target_runner(f, first)
    with pytest.raises(AgentInvocationRejected) as rejected:
        await invoke(f, "c", caller=b)
    assert rejected.value.code == "budget_exhausted"
    assert await f.db.scalar(select(func.count()).select_from(AgentInvocation)) == 1


async def test_fanout_is_per_work_but_depth_and_count_remain_run_wide(collaboration_db):
    f = collaboration_db
    f.run.hard_budget_snapshot = {"max_agent_depth": 8, "max_agent_fan_out_per_work_item": 1}
    for source, target in [("a", "b"), ("a", "d"), ("b", "c")]:
        await bind(f, source, target)
    b = await target_runner(f, await invoke(f, "b"))
    with pytest.raises(AgentInvocationRejected) as rejected:
        await invoke(f, "d")
    assert rejected.value.code == "agent_fan_out_exhausted"
    assert (await invoke(f, "c", caller=b)).status == "queued"


@pytest.mark.parametrize("run_mode", ["chat", "plan"])
@pytest.mark.parametrize("child_failed", [False, True])
async def test_nested_results_resume_direct_parent_then_ancestor(collaboration_db, run_mode, child_failed):
    f = collaboration_db
    mode(f, run_mode)
    await bind(f, "a", "b")
    await bind(f, "b", "c")
    ab = await invoke(f, "b")
    b = await target_runner(f, ab)
    ab.status = "running"
    bc = await invoke(f, "c", caller=b)
    c = await target_runner(f, bc)
    bc.status = "running"
    b_work = await f.db.get(WorkItem, b.work_item_id)
    c_work = await f.db.get(WorkItem, c.work_item_id)
    for work, runner in [(f.work, f.runner), (b_work, b)]:
        work.status = "waiting_agent"
        work.wait_reason = "waiting_agent"
        work.active_runner_id = None
        runner.status = "succeeded"
    f.run.status = "waiting_agent"
    f.run.wait_reason = "waiting_agent"
    worker = RuntimeWorker(worker_id="nested-test")
    await worker._settle_agent_target(
        f.db, runner=c, work=c_work, run=f.run, invocation=bc,
        content=None if child_failed else "C evidence",
        error=RuntimeError("C failed") if child_failed else None,
    )
    resumed_b = await f.db.get(DurableRunner, b_work.active_runner_id)
    assert resumed_b.id != b.id and resumed_b.agent_id == "b"
    assert resumed_b.principal_snapshot["delegation_chain"] == ["a"]
    assert resumed_b.principal_snapshot["account_id"] == "account"
    assert f.work.status == "waiting_agent" and f.work.active_runner_id is None
    assert ab.status == "running"
    b_result = json.loads(resumed_b.input_snapshot["pending_results"][-1]["content"])
    assert b_result["data"]["agent_invocation_id"] == bc.id
    assert b_result["status"] == ("failed" if child_failed else "succeeded")
    b_work.status = "running"
    await worker._settle_agent_target(
        f.db, runner=resumed_b, work=b_work, run=f.run, invocation=ab, content="B synthesis",
    )
    resumed_a = await f.db.get(DurableRunner, f.work.active_runner_id)
    assert resumed_a.id != f.runner.id and resumed_a.agent_id == "a"
    assert resumed_a.principal_snapshot.get("delegation_chain", []) == []
    a_result = json.loads(resumed_a.input_snapshot["pending_results"][-1]["content"])
    assert a_result["data"]["agent_invocation_id"] == ab.id
    assert a_result["data"]["content"] == "B synthesis"
    assert ab.status == "succeeded" and f.work.status == "queued"
