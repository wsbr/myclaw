"""Phase-one Plan permission-mode vertical acceptance against deployed services."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import time
import uuid

import pymysql
import pytest

from conftest import PhaseOneAPI


pytestmark = pytest.mark.fullstack


def _session(api: PhaseOneAPI, agent_id: str) -> dict:
    return api.request(
        "POST", "/api/v1/sessions", idem=True,
        json={"entry_agent_id": agent_id},
    ).json()


def _enter(api: PhaseOneAPI, session: dict, goal: str) -> dict:
    return api.request(
        "POST", f"/api/v1/sessions/{session['id']}/plan-mode", idem=True,
        headers={"If-Match": f'"{session["version"]}"'}, json={"goal": goal},
    ).json()


def _compose(*args: str) -> None:
    test_root = Path(__file__).resolve().parents[2]
    project = os.getenv("MYCLAW_COMPOSE_PROJECT") or os.getenv("COMPOSE_PROJECT_NAME")
    command = ["docker", "compose"]
    if project:
        command.extend(["-p", project])
    command.extend(["-f", str(test_root / "docker-compose.test.yml"), *args])
    subprocess.run(command, cwd=test_root, check=True, timeout=120)


def _db_row(query: str, params: tuple = ()) -> tuple:
    connection = pymysql.connect(
        host=os.getenv("MYCLAW_GOAL1_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYCLAW_GOAL1_MYSQL_PORT", "3307")),
        user=os.getenv("MYCLAW_GOAL1_MYSQL_USER", "myclaw"),
        password=os.getenv("MYCLAW_GOAL1_MYSQL_PASSWORD", "mypassword"),
        database=os.getenv("MYCLAW_GOAL1_MYSQL_DATABASE", "myclaw_test"),
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchone()
    finally:
        connection.close()


def _wait_plan(api: PhaseOneAPI, run_id: str) -> dict:
    return api.wait_resource(
        f"/api/v1/runs/{run_id}",
        lambda item: item["status"] in {
            "waiting_approval", "waiting_user", "failed", "cancelled",
        },
    )


def _approve_tool_actions_until_terminal(
    api: PhaseOneAPI, session_id: str, run_id: str,
) -> dict:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        run = api.request("GET", f"/api/v1/runs/{run_id}").json()
        if run["status"] in {"completed", "cancelled", "failed"}:
            return run
        actions = api.request(
            "GET", f"/api/v1/sessions/{session_id}/required-actions",
            params={"status": "pending"},
        ).json()["items"]
        for action in actions:
            if action["type"] not in {"tool", "session_grant"}:
                continue
            api.request(
                "POST", f"/api/v1/approvals/{action['id']}/approve", idem=True,
                headers={"If-Match": f'"{action["version"]}"'},
                json={
                    "subject_hash": action["subject_hash"],
                    "reason": "Plan vertical acceptance Tool approval",
                },
            )
        time.sleep(0.2)
    raise AssertionError("Plan implementation did not reach a terminal Run")


@pytest.mark.timeout(300)
def test_plan_revision_cas_restart_approval_and_same_session_implementation(
    phase_api: PhaseOneAPI, assembled_world: dict, record_testsuite_property,
):
    api = phase_api
    session = _session(api, assembled_world["audit_lead"]["id"])
    entered = _enter(
        api, session,
        "复杂任务：先只读调查并分步骤规划，然后把结论写入可下载的《验收报告 2026.txt》，"
        "正文只写 artifact-e2e，完成文件创建和正式发布后再回复。",
    )
    run_id = entered["run"]["id"]
    root_work_item_id = entered["run"]["root_work_item_id"]
    assert entered["session"]["interaction_mode"] == "plan"
    assert entered["session"]["permission_mode"] == "plan_readonly"
    assert entered["session"]["permission_revision"] == session["permission_revision"] + 1

    assert _wait_plan(api, run_id)["status"] == "waiting_approval"
    first_action = api.wait_action(session["id"], "plan_implementation")
    plans = api.request("GET", f"/api/v1/runs/{run_id}/working-plans").json()["items"]
    first_plan = plans[0]
    assert first_plan["revision"] == 1 and len(first_plan["items"]) >= 3

    # Approval has not happened: no business-side-effect Tool can have
    # succeeded and no Artifact/extra plan-item WorkItem exists.
    assert _db_row(
        "SELECT COUNT(*) FROM tool_invocation WHERE run_id=%s AND "
        "tool_key IN ('write_file','execute','publish_artifact') AND status='succeeded'",
        (run_id,),
    )[0] == 0
    assert _db_row(
        "SELECT COUNT(*) FROM artifact WHERE work_item_id IN "
        "(SELECT id FROM work_item WHERE run_id=%s)", (run_id,),
    )[0] == 0
    assert _db_row("SELECT COUNT(*) FROM work_item WHERE run_id=%s", (run_id,))[0] == 1

    # Discussion changes direction in the same Run/WorkItem and produces a
    # new append-only revision. A stale revision/hash cannot be approved.
    def compete_for_revision(instruction: str):
        return api.client.post(
            f"/api/v1/working-plans/{first_plan['id']}/continue-planning",
            headers={
                **api.auth, "Idempotency-Key": str(uuid.uuid4()),
                "If-Match": f'"{first_plan["version"]}"',
            },
            json={
                "instruction": instruction,
                "expected_revision": first_plan["revision"],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        competing = list(pool.map(compete_for_revision, (
            "修改方向：先验证回滚边界，再交付文件。",
            "竞争方向：先扩展非功能检查，再交付文件。",
        )))
    assert sorted(response.status_code for response in competing) == [202, 409]
    continued = next(response for response in competing if response.status_code == 202).json()
    assert continued["run"]["id"] == run_id
    assert continued["run"]["root_work_item_id"] == root_work_item_id
    assert _wait_plan(api, run_id)["status"] == "waiting_approval"
    second_action = api.wait_action(session["id"], "plan_implementation")
    current_plan = api.request(
        "GET", f"/api/v1/working-plans/{first_plan['id']}",
    ).json()
    assert current_plan["revision"] == 2
    stale = api.client.post(
        f"/api/v1/working-plans/{first_plan['id']}/approve-implementation",
        headers={
            **api.auth, "Idempotency-Key": str(uuid.uuid4()),
            "If-Match": f'"{first_plan["version"]}"',
        },
        json={
            "expected_revision": first_plan["revision"],
            "subject_hash": first_action["subject_hash"],
        },
    )
    assert stale.status_code == 409

    # A restart while waiting does not lose the exact current approval.
    _compose("restart", "runtime-worker-test")
    approved = api.request(
        "POST", f"/api/v1/working-plans/{current_plan['id']}/approve-implementation",
        idem=True, headers={"If-Match": f'"{current_plan["version"]}"'},
        json={
            "expected_revision": current_plan["revision"],
            "subject_hash": second_action["subject_hash"],
        },
    ).json()
    assert approved["session"]["permission_mode"] == "direct"
    assert approved["run"]["id"] == run_id
    assert approved["run"]["root_work_item_id"] == root_work_item_id

    terminal = _approve_tool_actions_until_terminal(api, session["id"], run_id)
    assert terminal["status"] == "completed" and terminal["outcome"] == "succeeded"
    assert terminal["permission_mode"] == "direct"
    work_items = api.request("GET", f"/api/v1/runs/{run_id}/work-items").json()["items"]
    assert [item["id"] for item in work_items] == [root_work_item_id]
    assert work_items[0]["execution_epoch"] >= 4
    artifacts = api.request(
        "GET", f"/api/v1/sessions/{session['id']}/artifacts",
        params={"status": "published"},
    ).json()["items"]
    artifact = next(item for item in artifacts if item["name"] == "验收报告 2026.txt")
    assert api.request("GET", f"/api/v1/artifacts/{artifact['id']}/download").content == b"artifact-e2e"
    updates = api.request(
        "GET", f"/api/v1/working-plans/{current_plan['id']}/updates",
    ).json()["items"]
    assert [item["revision"] for item in updates] == [1, 2]
    record_testsuite_property("plan_approval_execution", "passed")


@pytest.mark.timeout(300)
def test_plan_dynamic_agent_invocation_rejection_and_cancel_paths(
    phase_api: PhaseOneAPI, assembled_world: dict,
):
    api = phase_api
    cards = api.request("GET", "/api/v1/agent-network/cards").json()["items"]
    assert len(cards) >= 3
    session = _session(api, assembled_world["audit_lead"]["id"])
    entered = _enter(
        api, session,
        "复杂任务 plan-agent-network ask clarification：先形成计划，澄清统计周期，"
        "并按需发现一个当前候选 Agent 做只读专业复核。",
    )
    run_id = entered["run"]["id"]
    root_work_item_id = entered["run"]["root_work_item_id"]
    assert _wait_plan(api, run_id)["status"] == "waiting_user"
    clarification = api.wait_action(session["id"], "clarification")
    answered = api.request(
        "POST", f"/api/v1/clarifications/{clarification['id']}/answer", idem=True,
        headers={"If-Match": f'"{clarification["version"]}"'},
        json={"answer": "采用 2026 年第二季度口径。"},
    ).json()
    assert answered["run_id"] == run_id and answered["work_item_id"] == root_work_item_id
    assert _wait_plan(api, run_id)["status"] == "waiting_approval"
    invocations = api.request(
        "GET", f"/api/v1/runs/{run_id}/agent-invocations",
    ).json()["items"]
    assert len(invocations) == 1 and invocations[0]["status"] == "succeeded"
    assert invocations[0]["target_agent_version_id"]
    assert len(invocations[0]["request_hash"]) == 64
    assert _db_row(
        "SELECT COUNT(*) FROM tool_invocation WHERE run_id=%s AND status='succeeded'",
        (run_id,),
    )[0] == 0
    # Exactly one root plus one real dynamic delegation. WorkingPlan items did
    # not turn into WorkItems.
    assert _db_row("SELECT COUNT(*) FROM work_item WHERE run_id=%s", (run_id,))[0] == 2

    action = api.wait_action(session["id"], "plan_implementation")
    rejected = api.request(
        "POST", f"/api/v1/approvals/{action['id']}/reject", idem=True,
        headers={"If-Match": f'"{action["version"]}"'},
        json={"subject_hash": action["subject_hash"], "reason": "需要更严格的测试方向"},
    ).json()
    assert rejected["status"] == "rejected"
    assert _wait_plan(api, run_id)["status"] == "waiting_approval"
    refreshed = api.request("GET", f"/api/v1/sessions/{session['id']}").json()
    assert refreshed["permission_mode"] == "plan_readonly"
    exited = api.request(
        "POST", f"/api/v1/sessions/{session['id']}/plan-mode/exit", idem=True,
        headers={"If-Match": f'"{refreshed["version"]}"'},
    ).json()
    assert exited["interaction_mode"] == "chat" and exited["permission_mode"] == "direct"
    assert api.request("GET", f"/api/v1/runs/{run_id}").json()["status"] == "cancelled"

    # Candidate choice stays model-driven: three separate PM goals cause three
    # directory queries and exact product/development/test delegations without
    # a platform-fixed participant set or order.
    for marker, role_name in (
        ("plan-agent-network-product", "产品经理"),
        ("plan-agent-network-development", "开发工程师"),
        ("plan-agent-network-test", "测试工程师"),
    ):
        role_session = _session(api, assembled_world["audit_lead"]["id"])
        role_run = _enter(
            api, role_session,
            f"复杂任务 {marker}：按需查询并调用对应专业候选进行只读复核。",
        )["run"]
        assert _wait_plan(api, role_run["id"])["status"] == "waiting_approval"
        role_invocations = api.request(
            "GET", f"/api/v1/runs/{role_run['id']}/agent-invocations",
        ).json()["items"]
        assert len(role_invocations) == 1
        selected_card = api.request(
            "GET", f"/api/v1/agent-network/agents/{role_invocations[0]['target_agent_id']}",
        ).json()
        assert selected_card["name"].startswith(role_name)
        role_current = api.request(
            "GET", f"/api/v1/sessions/{role_session['id']}",
        ).json()
        api.request(
            "POST", f"/api/v1/sessions/{role_session['id']}/plan-mode/exit",
            idem=True, headers={"If-Match": f'"{role_current["version"]}"'},
        )


@pytest.mark.timeout(600)
def test_plan_proposals_are_readable_30_of_30_and_adapt_5_of_5(
    phase_api: PhaseOneAPI, assembled_world: dict, record_testsuite_property,
):
    api = phase_api
    readable = 0
    adapted = 0
    for index in range(30):
        session = _session(api, assembled_world["audit_lead"]["id"])
        entered = _enter(
            api, session,
            f"复杂任务：第 {index + 1} 个 proposal 可读性样本，请分步骤规划退款审计交付。",
        )
        run_id = entered["run"]["id"]
        assert _wait_plan(api, run_id)["status"] == "waiting_approval"
        plan = api.request("GET", f"/api/v1/runs/{run_id}/working-plans").json()["items"][0]
        if plan["summary"].strip() and all(
            item["text"].strip() and item["status"] in {"pending", "in_progress", "completed"}
            for item in plan["items"]
        ):
            readable += 1
        if index < 5:
            api.request(
                "POST", f"/api/v1/working-plans/{plan['id']}/continue-planning",
                idem=True, headers={"If-Match": f'"{plan["version"]}"'},
                json={
                    "instruction": f"适应性样本 {index + 1}：增加失败回滚验证。",
                    "expected_revision": plan["revision"],
                },
            )
            assert _wait_plan(api, run_id)["status"] == "waiting_approval"
            revised = api.request("GET", f"/api/v1/working-plans/{plan['id']}").json()
            adapted += int(revised["revision"] == plan["revision"] + 1)
        current = api.request("GET", f"/api/v1/sessions/{session['id']}").json()
        api.request(
            "POST", f"/api/v1/sessions/{session['id']}/plan-mode/exit", idem=True,
            headers={"If-Match": f'"{current["version"]}"'},
        )
    assert readable == 30
    assert adapted == 5
    record_testsuite_property("proposal_readability", "30/30")
    record_testsuite_property("plan_adaptability", "5/5")
