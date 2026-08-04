from __future__ import annotations

import subprocess
import threading
import time
import uuid
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from conftest import PhaseOneAPI


pytestmark = pytest.mark.fullstack


def _session(api: PhaseOneAPI, agent_id: str, title: str) -> dict:
    return api.request("POST", "/api/v1/sessions", idem=True, json={
        "entry_agent_id": agent_id, "origin_type": "web", "title": title,
    }).json()


def _chat(api: PhaseOneAPI, session_id: str, content: str) -> dict:
    return api.request(
        "POST", f"/api/v1/sessions/{session_id}/messages", idem=True,
        json={"content": content, "mode": "chat"},
    ).json()


def _events(api: PhaseOneAPI, run_id: str) -> list[dict]:
    return api.request("GET", f"/api/v1/runs/{run_id}/events").json()["items"]


def _approve(api: PhaseOneAPI, action: dict, reason: str) -> None:
    api.request(
        "POST", f"/api/v1/tool-confirmations/{action['id']}/approve", idem=True,
        headers={"If-Match": f'W/"{action["version"]}"'},
        json={"arguments_hash": action["arguments_hash"], "reason": reason},
    )


def _wait_run_with_user_actions(
    api: PhaseOneAPI,
    run_id: str,
    session_id: str,
    *,
    clarification_answer: str,
    max_actions: int = 8,
    timeout: float | None = None,
) -> tuple[dict, dict[str, int]]:
    """Drive bounded, real approval/clarification waits until a Run settles."""
    deadline = time.monotonic() + (timeout or api.run_timeout * 2)
    counts = {"tool_confirmation": 0, "clarification": 0}
    handled: set[str] = set()
    last = None
    while time.monotonic() < deadline:
        last = api.request("GET", f"/api/v1/runs/{run_id}").json()
        if last["lifecycle"] in {"completed", "cancelled"}:
            return last, counts
        for action_type in ("tool_confirmation", "clarification"):
            actions = api.request(
                "GET", f"/api/v1/sessions/{session_id}/required-actions",
                params={"status": "pending", "type": action_type},
            ).json()["items"]
            for action in actions:
                if action["id"] in handled:
                    continue
                handled.add(action["id"])
                counts[action_type] += 1
                if sum(counts.values()) > max_actions:
                    raise AssertionError(
                        f"Run {run_id} exceeded the bounded user-action budget: {counts}"
                    )
                if action_type == "tool_confirmation":
                    _approve(api, action, "Bounded vertical acceptance approval")
                else:
                    api.request(
                        "POST", f"/api/v1/clarifications/{action['id']}/answer", idem=True,
                        headers={"If-Match": f'W/"{action["version"]}"'},
                        json={"answer": clarification_answer},
                    )
        time.sleep(1.0)
    raise AssertionError(f"Run {run_id} did not settle with bounded user actions: {last}, {counts}")


def _capture_sse(api: PhaseOneAPI, session_id: str, after_sequence: int, ready: threading.Event) -> list[dict]:
    frames: list[dict] = []
    timeout = httpx.Timeout(connect=10, read=api.run_timeout, write=10, pool=10)
    with httpx.Client(base_url=api.base_url, headers=api.auth, timeout=timeout, trust_env=False) as client:
        with client.stream(
            "GET", f"/api/v1/sessions/{session_id}/events/stream",
            params={"after_sequence": after_sequence},
            headers={"Last-Event-ID": str(after_sequence)},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            ready.set()
            event_type = ""
            data = ""
            event_id = ""
            for line in response.iter_lines():
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data += line[6:]
                elif line.startswith("id: "):
                    event_id = line[4:]
                elif line == "" and event_type:
                    payload = json.loads(data) if data else {}
                    frames.append({"event": event_type, "id": event_id, "data": payload})
                    if event_type == "run.completed":
                        return frames
                    event_type = data = event_id = ""
    return frames


def _collector_logs() -> str:
    test_root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        ["docker", "compose", "-f", str(test_root / "docker-compose.test.yml"),
         "logs", "--no-color", "otel-collector-test"],
        cwd=test_root, check=True, timeout=30, capture_output=True, text=True,
    ).stdout


def _restart(*services: str):
    test_root = Path(__file__).resolve().parents[2]
    subprocess.run(
        ["docker", "compose", "-f", str(test_root / "docker-compose.test.yml"), "restart", *services],
        cwd=test_root, check=True, timeout=60,
    )


def _publish_next(api: PhaseOneAPI, agent_id: str, label: str) -> str:
    draft = api.request("GET", f"/api/v1/agents/{agent_id}/draft").json()
    updated = api.request(
        "PATCH", f"/api/v1/agents/{agent_id}/draft",
        headers={"If-Match": f'W/"{draft["draft_revision"]}"'},
        json={"instructions": f"{draft['instructions']}\nPublished acceptance revision: {label}."},
    ).json()
    return api.request(
        "POST", f"/api/v1/agents/{agent_id}/publish", idem=True,
        json={"expected_draft_revision": updated["draft_revision"]},
    ).json()["published_version"]["id"]


@pytest.mark.timeout(1200)
def test_phase_one_acceptance(
    phase_api: PhaseOneAPI, assembled_world: dict, record_testsuite_property,
):
    api, world = phase_api, assembled_world
    record_property = record_testsuite_property
    record_property("commit_sha", os.getenv("MYCLAW_ACCEPTANCE_COMMIT_SHA", "uncommitted"))
    record_property("model", os.getenv("MYCLAW_REAL_LLM_MODEL", "mock"))
    audit_lead = world["audit_lead"]["id"]
    refund_ops = world["refund_ops"]["id"]
    risk_control = world["risk_control"]["id"]
    external_supplier = world["external_supplier"]["id"]

    # 1. Builder assembly is fully published and inspectable as an immutable version.
    detail = api.request("GET", f"/api/v1/agents/{audit_lead}").json()
    assert detail["agent"]["status"] == "published"
    assert detail["draft"]["model_id"] == world["model"]["id"]
    assert detail["draft"]["sandbox_profile_id"] == world["sandbox"]["id"]
    assert detail["draft"]["skill_ids"] == [world["skill"]["id"]]
    assert set(detail["draft"]["bound_agent_ids"]) == {
        refund_ops, risk_control, external_supplier,
    }
    version = api.request(
        "GET", f"/api/v1/agents/{audit_lead}/versions/{world['audit_lead']['published']['id']}",
    ).json()
    assert version["publication_status"] == "published" and version["content_hash"]

    # A draft with the published Skill selection can be copied and sealed for
    # an isolated draft-test Run without violating immutable relation guards.
    draft_test = api.request(
        "POST", f"/api/v1/agents/{audit_lead}/draft/test-runs", idem=True,
        json={
            "content": "请用一句话确认已进入退款审计上下文；本次不需要执行任何外部操作。",
            "expected_draft_revision": detail["draft"]["draft_revision"],
        },
    ).json()
    assert draft_test["run"]["execution_kind"] == "draft_test"
    assert api.wait_run(draft_test["run"]["id"])["outcome"] == "succeeded"

    # The published entry Agent is callable through its scoped Agent API Key.
    key_record = api.request(
        "POST", f"/api/v1/agents/{audit_lead}/api-keys", idem=True,
        json={"name": "phase-one-acceptance", "caller_label": "docker-e2e"},
    ).json()
    assert key_record["secret"] and key_record["prefix"]
    api_key_secret = key_record["secret"]
    try:
        with httpx.Client(
            base_url=api.base_url, timeout=30, trust_env=False,
            headers={"Authorization": f"Bearer {api_key_secret}"},
        ) as agent_client:
            api_session_response = agent_client.post(
                "/api/v1/sessions", headers={"Idempotency-Key": str(uuid.uuid4())},
                json={"entry_agent_id": audit_lead, "origin_type": "api", "user_id": api.user_id,
                      "title": "退款审计 API 接入验收"},
            )
            assert api_session_response.status_code == 201, api_session_response.text
            api_session = api_session_response.json()
            assert api_session["origin_type"] == "api" and api_session["entry_agent_id"] == audit_lead
            api_chat_response = agent_client.post(
                f"/api/v1/sessions/{api_session['id']}/messages",
                headers={"Idempotency-Key": str(uuid.uuid4())},
                json={"content": "请简短确认已收到 2026 年第二季度退款审计任务，本次只需回复确认。",
                      "mode": "chat"},
            )
            assert api_chat_response.status_code == 202, api_chat_response.text
            assert api.wait_run(api_chat_response.json()["run"]["id"])["outcome"] == "succeeded"
    finally:
        api.request("DELETE", f"/api/v1/agents/{audit_lead}/api-keys/{key_record['id']}", idem=True)
    record_property("scene_1_builder_assembly_publish", "passed")

    # 2. Chat survives worker/Web disconnects, waits for confirmation, and executes a durable file Tool.
    chat_session = _session(api, audit_lead, "退款批次审计")
    chat = _chat(
        api, chat_session["id"],
        "退款批次汇总显示申请 4087 笔、有效退款 4086 笔。请在隔离分析环境运行 Python，"
        "断言两者差额为 1，然后打印 sandbox-e2e 并返回真实标准输出；不要创建或发布文件。"
        "平台会在实际执行前自动向我展示审批卡，请直接提交待审批的执行请求，无需另行提问；"
        "不要用文字假装已经运行。",
    )
    action = api.wait_action(chat_session["id"], "tool_confirmation")
    _restart("runtime-worker-test", "sandbox-worker-test")
    assert action["tool_key"] == "execute"
    _approve(api, action, "Acceptance approval")
    # A fresh HTTP client simulates reconnect after the initiating connection is gone.
    with httpx.Client(base_url=api.base_url, timeout=30, headers=api.auth, trust_env=False) as reconnected:
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            settled = reconnected.get(f"/api/v1/runs/{chat['run']['id']}").json()
            if settled["lifecycle"] == "completed":
                break
            time.sleep(0.25)
        else:
            raise AssertionError(f"Reconnect polling did not settle Run: {settled}")
    assert settled["outcome"] == "succeeded"
    timeline = _events(api, chat["run"]["id"])
    assert {event["event_type"] for event in timeline} >= {
        "tool.confirmation_required", "tool_confirmation.approved", "tool.succeeded", "run.completed",
    }
    assert any("sandbox-e2e" in (item.get("content") or "") for item in api.request(
        "GET", f"/api/v1/sessions/{chat_session['id']}/messages",
    ).json()["items"])
    chat_runner = api.request(
        "GET", f"/api/v1/runs/{chat['run']['id']}/runners",
    ).json()["items"][-1]
    chat_trace = api.request("GET", f"/api/v1/runners/{chat_runner['id']}/trace").json()
    assert chat_trace["harness"]["response_contract"] == "tool_envelope.conversation_completion.v1"
    assert chat_trace["harness"]["harness_tool_keys"] == ["finalize_response"]
    assert {"execute", "write_file", "publish_artifact"} <= set(
        chat_trace["harness"]["assembled_tool_keys"]
    )
    # execute belongs to the frozen assembly and was actually satisfied, but is
    # deliberately absent from the final model turn after its one-shot effect.
    assert "execute" not in chat_trace["harness"]["effective_tool_keys"]
    assert "execute" in chat_trace["harness"]["completion_contract"]["required_tool_keys"]
    assert "execute" in chat_trace["harness"]["completion_contract"]["satisfied_tool_keys"]
    assert "execute" not in chat_trace["harness"]["completion_contract"]["pending_tool_keys"]

    # Streaming model deltas and reliable SSE events share one reconnectable endpoint.
    high_watermark = api.request("GET", f"/api/v1/sessions/{chat_session['id']}/events").json()[
        "session_high_watermark"
    ]
    ready = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        stream_future = executor.submit(_capture_sse, api, chat_session["id"], high_watermark, ready)
        assert ready.wait(10), "SSE connection did not become ready"
        time.sleep(0.5)
        streamed_chat = _chat(
            api, chat_session["id"],
            "请用一句话确认刚才的批次校验已经进入审计时间线，本次无需再执行外部操作。",
        )
        assert api.wait_run(streamed_chat["run"]["id"])["outcome"] == "succeeded"
        frames = stream_future.result(timeout=api.run_timeout + 10)
    assert any(
        frame["event"] == "model.delta"
        and frame["data"].get("run_id") == streamed_chat["run"]["id"]
        and frame["data"].get("delta")
        for frame in frames
    )
    assert any(
        frame["event"] == "run.completed"
        and frame["data"].get("run_id") == streamed_chat["run"]["id"]
        for frame in frames
    )

    # A file becomes downloadable only after the model explicitly writes and publishes it.
    artifact_chat = _chat(
        api, chat_session["id"],
        "请把本轮退款审计结论整理成可下载的《验收报告 2026.txt》。文件正文只写 artifact-e2e，"
        "格式为纯文本；请完成文件创建和正式发布后再回复我，不要只在消息里贴出内容。",
    )
    write_confirmation = api.wait_action(chat_session["id"], "tool_confirmation")
    assert write_confirmation["tool_key"] == "write_file"
    _approve(api, write_confirmation, "Allow deterministic acceptance file")
    publish_confirmation = api.wait_action(chat_session["id"], "tool_confirmation", timeout=api.run_timeout)
    assert publish_confirmation["tool_key"] == "publish_artifact"
    _approve(api, publish_confirmation, "Publish deterministic acceptance Artifact")
    assert api.wait_run(artifact_chat["run"]["id"])["outcome"] == "succeeded"
    artifacts = api.request(
        "GET", f"/api/v1/sessions/{chat_session['id']}/artifacts", params={"status": "published"},
    ).json()["items"]
    artifact = next(item for item in artifacts if item["name"] == "验收报告 2026.txt")
    download = api.request("GET", f"/api/v1/artifacts/{artifact['id']}/download")
    assert download.content == b"artifact-e2e"
    disposition = download.headers["content-disposition"]
    assert 'filename="2026.txt"' in disposition
    assert "filename*=UTF-8''%E9%AA%8C%E6%94%B6%E6%8A%A5%E5%91%8A%202026.txt" in disposition
    record_property("scene_2_chat_sse_tools_recovery_artifact", "passed")

    # 6. Tool governance applies safely to queued invocations and to new calls immediately.
    governance_session = _session(api, audit_lead, "退款校验执行治理")
    governed = _chat(
        api, governance_session["id"],
        "退款对账规则需要验证计算环境。请在隔离分析环境运行 Python 代码 "
        "assert 4087 - 4086 == 1; print('sandbox-e2e')，返回真实标准输出；"
        "不要创建或发布文件，也不要用文字模拟运行结果。",
    )
    pending = api.wait_action(governance_session["id"], "tool_confirmation")
    assert pending["tool_key"] == "execute"
    execute_tool = api.request("GET", f"/api/v1/platform-tools/{world['tools']['execute']['id']}").json()
    disabled = api.request(
        "PATCH", f"/api/v1/platform-tools/{execute_tool['id']}", idem=True,
        headers={"If-Match": f'W/"{execute_tool["config_version"]}"'}, json={"enabled": False},
    ).json()
    try:
        _approve(api, pending, "Verify disabled preflight")
        governed_result = api.wait_run(governed["run"]["id"])
        governed_events = {event["event_type"] for event in _events(api, governed["run"]["id"])}
        assert governed_result["outcome"] == "failed"
        assert not {"tool.dispatched", "tool.succeeded"}.intersection(governed_events)
        api.request(
            "PATCH", f"/api/v1/platform-tools/{execute_tool['id']}", idem=True,
            headers={"If-Match": f'W/"{disabled["config_version"]}"'},
            json={"enabled": True, "confirmation_policy": "never"},
        )
        immediate = _chat(
            api, governance_session["id"],
            "治理策略更新后，请再次在隔离分析环境运行 Python 代码 "
            "assert 4087 - 4086 == 1; print('sandbox-e2e') 并返回真实标准输出，"
            "不要创建或发布文件。",
        )
        assert api.wait_run(immediate["run"]["id"])["outcome"] == "succeeded"
        assert not api.request(
            "GET", f"/api/v1/sessions/{governance_session['id']}/required-actions",
            params={"status": "pending", "type": "tool_confirmation"},
        ).json()["items"]
    finally:
        current_execute = api.request("GET", f"/api/v1/platform-tools/{execute_tool['id']}").json()
        if not current_execute["enabled"] or current_execute["confirmation_policy"] != "always":
            api.request(
                "PATCH", f"/api/v1/platform-tools/{execute_tool['id']}", idem=True,
                headers={"If-Match": f'W/"{current_execute["config_version"]}"'},
                json={"enabled": True, "confirmation_policy": "always"},
            )
    record_property("scene_6_tool_governance", "passed")

    # 3. A real refund-audit Plan is edited, frozen, and runs independent domain nodes in parallel.
    plan_session = _session(api, audit_lead, "季度退款审计计划")
    created_plan = api.request(
        "POST", f"/api/v1/sessions/{plan_session['id']}/plans", idem=True,
        json={
            "goal": "复盘 2026 年第二季度退款异常：分别核对退款运营口径、订单风险和审计结论，"
                    "最后形成一份标明证据缺口的管理层摘要"
        },
    ).json()["plan"]
    proposal = api.wait_resource(
        f"/api/v1/plans/{created_plan['id']}",
        lambda value: value["definition_status"] == "awaiting_confirmation",
    )
    proposal_runners = api.request(
        "GET", f"/api/v1/runs/{created_plan['run_id']}/runners",
    ).json()["items"]
    proposal_harness = [
        api.request("GET", f"/api/v1/runners/{item['id']}/trace").json()["harness"]
        for item in proposal_runners
    ]
    assert all(item["response_contract"] == "structured.plan_proposal.v1" for item in proposal_harness)
    assert all(item["structured_output"] in {"json_object", "json_schema"} for item in proposal_harness)
    nodes = [
        {
            "node_key": "audit_summary", "title": "审计口径与交付框架",
            "goal": "基于已确认的 2026 年第二季度口径，定义退款审计范围、关键指标和管理层摘要结构；"
                    "本节点只需在消息中返回摘要，不生成文件。",
            "acceptance_criteria": "消息内明确统计周期、审计口径、证据缺口和交付结构。",
            "assigned_agent_id": audit_lead,
        },
        {
            "node_key": "refund_reconcile", "title": "退款运营核对",
            "goal": "基于题面已确认信息核对退款批次、渠道状态与处理时效，在消息中列出差异及业务解释。",
            "acceptance_criteria": "消息内覆盖批次、渠道、时效三个维度，并标明证据缺口。",
            "assigned_agent_id": refund_ops,
        },
        {
            "node_key": "risk_review", "title": "异常订单风控复核",
            "goal": "基于题面已确认信息复核重复退款、账号关联和设备风险，在消息中给出风险分层。",
            "acceptance_criteria": "消息内输出高、中、低风险判断及每类证据依据。",
            "assigned_agent_id": risk_control,
        },
    ]
    edited_plan = api.request(
        "PATCH", f"/api/v1/plans/{proposal['id']}", idem=True,
        headers={"If-Match": f'W/"{proposal["version"]}"'},
        json={"goal": proposal["goal"], "deliverable": "季度退款异常复盘与管理层审计摘要", "nodes": nodes,
              "dependencies": [], "expected_graph_revision": proposal["graph_revision"]},
    ).json()
    frozen_plan = api.request(
        "POST", f"/api/v1/plans/{proposal['id']}/confirm", idem=True,
        headers={"If-Match": f'W/"{edited_plan["version"]}"'},
        json={"expected_graph_revision": edited_plan["graph_revision"]},
    ).json()
    assert frozen_plan["definition_status"] == "frozen"
    plan_result, plan_actions = _wait_run_with_user_actions(
        api, frozen_plan["run_id"], plan_session["id"],
        clarification_answer=(
            "采用 2026 年第二季度口径；本轮只需基于题面生成消息内摘要，未知数据列为证据缺口。"
        ),
        max_actions=6,
    )
    assert plan_result["outcome"] == "succeeded"
    assert sum(plan_actions.values()) <= 6
    plan_versions = {item["agent_id"]: item["agent_version_id"] for item in frozen_plan["declared_agents"]}
    plan_work = api.request("GET", f"/api/v1/plans/{proposal['id']}/work-items").json()["items"]
    assert len(plan_work) == 3 and {item["status"] for item in plan_work} == {"succeeded"}
    record_property("scene_3_plan_freeze_parallel_settlement", "passed")

    # 4. Three-Agent refund Group: clarification, peer review, and confirmed-history reuse.
    group_session = _session(api, audit_lead, "退款异常联合复核组")
    formation = api.request(
        "POST", f"/api/v1/sessions/{group_session['id']}/group-formations", idem=True,
        json={
            "goal": "由退款审计负责人、退款运营专员和订单风控复核员联合复核第二季度异常退款，"
                    "先确认业务口径，再交叉验证风险结论"
        },
    ).json()
    formation = api.wait_resource(
        f"/api/v1/group-formations/{formation['id']}",
        lambda value: value["status"] == "awaiting_confirmation",
    )
    formation_runners = api.request(
        "GET", f"/api/v1/runs/{formation['generation_run']['id']}/runners",
    ).json()["items"]
    formation_harness = [
        api.request("GET", f"/api/v1/runners/{item['id']}/trace").json()["harness"]
        for item in formation_runners
    ]
    assert all(
        item["response_contract"] == "structured.group_formation.v1"
        for item in formation_harness
    )
    participants = [
        {"participant_key": "audit", "agent_id": audit_lead, "is_entry": True,
         "initial_responsibility": "统筹审计范围、交叉复核与最终交付"},
        {"participant_key": "refund", "agent_id": refund_ops, "is_entry": False,
         "initial_responsibility": "核对退款批次、渠道状态与处理时效"},
        {"participant_key": "risk", "agent_id": risk_control, "is_entry": False,
         "initial_responsibility": "复核异常订单和重复退款风险"},
    ]
    work_items = [
        {"work_item_key": "audit_scope", "participant_key": "audit", "title": "冻结审计口径",
         "goal": "整理已确认范围和待补证据。", "acceptance_criteria": "给出口径、范围和缺口清单。"},
        {"work_item_key": "refund_batch", "participant_key": "refund", "title": "退款批次核验",
         "goal": "核对退款批次和渠道处理状态。", "acceptance_criteria": "列出差异、时效和未确认项。"},
        {"work_item_key": "risk_cases", "participant_key": "risk", "title": "异常订单复核",
         "goal": "复核重复退款和账号关联风险。", "acceptance_criteria": "给出风险分层和证据依据。"},
    ]
    edited_formation = api.request(
        "PATCH", f"/api/v1/group-formations/{formation['id']}", idem=True,
        headers={"If-Match": f'W/"{formation["version"]}"'},
        json={"goal": formation["current_revision"]["goal"],
              "bootstrap_context": "业务暂定审计周期为 2026 年第二季度；所有结论必须区分已确认事实与待补证据。",
              "participants": participants, "initial_work_items": work_items,
              "suggested_edges": [
                  {"source_participant_key": "audit", "target_participant_key": "refund",
                   "label": "退款口径核对", "rationale": "由运营提供批次和渠道证据"},
                  {"source_participant_key": "audit", "target_participant_key": "risk",
                   "label": "风险交叉复核", "rationale": "由风控复核异常订单结论"},
              ], "expected_revision_no": formation["current_revision_no"]},
    ).json()
    confirmed = api.request(
        "POST", f"/api/v1/group-formations/{formation['id']}/confirm", idem=True,
        headers={"If-Match": f'W/"{edited_formation["version"]}"'},
        json={"expected_revision_no": edited_formation["current_revision_no"]},
    ).json()
    group_id = confirmed["group_session"]["id"]
    initial_group_run = confirmed["group_run"]["id"]
    initial_group_result, initial_group_actions = _wait_run_with_user_actions(
        api, confirmed["run"]["id"], group_session["id"],
        clarification_answer=(
            "采用 2026 年第二季度口径；未知业务数据作为证据缺口，不生成额外文件。"
        ),
        max_actions=6,
    )
    assert initial_group_result["outcome"] in {"succeeded", "partially_succeeded"}
    assert sum(initial_group_actions.values()) <= 6
    frozen_group_versions = {
        item["group_node_id"]: item["agent_version_id"]
        for item in api.request("GET", f"/api/v1/group-runs/{initial_group_run}").json()["agent_versions"]
    }
    group_nodes = api.request("GET", f"/api/v1/groups/{group_id}/nodes").json()["items"]
    by_agent_node = {item["agent_id"]: item["id"] for item in group_nodes}

    clarification_run = api.request(
        "POST", f"/api/v1/groups/{group_id}/runs", idem=True,
        json={"content": "目前业务材料同时出现‘第二季度’和‘六月单月’两个统计口径。"
                         "在开始退款差异核对前，请先向我确认本轮应采用哪个周期。",
              "recipient_node_id": by_agent_node[refund_ops], "reference_run_ids": []},
    ).json()
    clarification = api.wait_action(group_session["id"], "clarification")
    assert clarification["group_node_id"] == by_agent_node[refund_ops]
    api.request(
        "POST", f"/api/v1/clarifications/{clarification['id']}/answer", idem=True,
        headers={"If-Match": f'W/"{clarification["version"]}"'},
        json={"answer": "采用 2026 年第二季度，六月数据作为月度切片单列。"},
    )
    clarification_result, _ = _wait_run_with_user_actions(
        api, clarification_run["run"]["id"], group_session["id"],
        clarification_answer="采用 2026 年第二季度，六月数据作为月度切片单列。",
        max_actions=4,
    )
    assert clarification_result["outcome"] in {"succeeded", "partially_succeeded"}
    clarification_traces = [
        api.request("GET", f"/api/v1/runners/{runner['id']}/trace").json()["harness"]
        for runner in api.request(
            "GET", f"/api/v1/runs/{clarification_run['run']['id']}/runners",
        ).json()["items"]
    ]
    assert any(
        "request_clarification" in trace["completion_contract"]["required_tool_keys"]
        and "request_clarification" in trace["completion_contract"]["satisfied_tool_keys"]
        and "request_clarification" not in trace["completion_contract"]["pending_tool_keys"]
        for trace in clarification_traces
    )

    peer_run = api.request(
        "POST", f"/api/v1/groups/{group_id}/runs", idem=True,
        json={"content": "请让订单风控复核员对‘同一账号短期内多笔退款’做一次独立复核，"
                         "并把风险分层依据合并到你的审计结论中。只在本组消息内返回结论，"
                         "不要创建或发布文件。",
              "recipient_node_id": by_agent_node[audit_lead], "reference_run_ids": []},
    ).json()
    peer_result, _ = _wait_run_with_user_actions(
        api, peer_run["run"]["id"], group_session["id"],
        clarification_answer="基于本组已确认口径继续，并明确证据缺口。",
        max_actions=4,
    )
    assert peer_result["outcome"] in {"succeeded", "partially_succeeded"}
    assert "agent_invocation.succeeded" in {event["event_type"] for event in _events(api, peer_run["run"]["id"])}
    peer_runners = api.request("GET", f"/api/v1/runs/{peer_run['run']['id']}/runners").json()["items"]
    peer_traces = [api.request("GET", f"/api/v1/runners/{runner['id']}/trace").json() for runner in peer_runners]
    assert any(
        link["link_type"] == "agent_invocation" and link["linked_span_id"]
        for trace in peer_traces for link in trace["links"]
    )
    assert any(
        "invoke_agent" in trace["harness"]["completion_contract"]["required_tool_keys"]
        and "invoke_agent" in trace["harness"]["completion_contract"]["satisfied_tool_keys"]
        and "invoke_agent" not in trace["harness"]["completion_contract"]["pending_tool_keys"]
        for trace in peer_traces
    )

    search_run = api.request(
        "POST", f"/api/v1/groups/{group_id}/runs", idem=True,
        json={"content": "请从本组已经确认的讨论记录中找出关于统计周期和重复退款口径的结论，"
                         "据此给出一段可追溯的复核摘要。只使用本组讨论记录并在消息内返回，"
                         "不要读取、创建或发布文件。",
              "recipient_node_id": by_agent_node[audit_lead], "reference_run_ids": []},
    ).json()
    search_result, _ = _wait_run_with_user_actions(
        api, search_run["run"]["id"], group_session["id"],
        clarification_answer="检索本组已确认的第二季度口径和重复退款结论。",
        max_actions=4,
    )
    assert search_result["outcome"] in {"succeeded", "partially_succeeded"}
    search_events = _events(api, search_run["run"]["id"])
    assert len([
        event for event in search_events
        if event["event_type"] == "tool.succeeded"
        and event.get("payload", {}).get("tool_key") == "search_group_history"
    ]) == 1
    search_traces = [
        api.request("GET", f"/api/v1/runners/{runner['id']}/trace").json()["harness"]
        for runner in api.request(
            "GET", f"/api/v1/runs/{search_run['run']['id']}/runners",
        ).json()["items"]
    ]
    assert any(
        "search_group_history" in trace["completion_contract"]["required_tool_keys"]
        and "search_group_history" in trace["completion_contract"]["satisfied_tool_keys"]
        and "search_group_history" not in trace["completion_contract"]["pending_tool_keys"]
        for trace in search_traces
    )
    if os.getenv("MYCLAW_LLM_MODE", "mock") == "mock":
        assert any(
            trace["completion_contract"]["repair_no"] == 1
            and trace["completion_contract"]["settlement_mode"]
            == "provider_plain_text_after_satisfied_effects"
            and "search_group_history"
            in trace["completion_contract"]["satisfied_tool_keys"]
            for trace in search_traces
        )

    record_property("scene_4_group_collaboration", "passed")

    # 5. Publishing new versions changes new Chat only; frozen Plan and Group mappings remain unchanged.
    new_audit_version = _publish_next(api, audit_lead, "v2")
    new_refund_version = _publish_next(api, refund_ops, "v2")
    assert {
        item["agent_id"]: item["agent_version_id"]
        for item in api.request("GET", f"/api/v1/plans/{proposal['id']}").json()["declared_agents"]
    } == plan_versions
    assert {
        item["group_node_id"]: item["agent_version_id"]
        for item in api.request("GET", f"/api/v1/group-runs/{initial_group_run}").json()["agent_versions"]
    } == frozen_group_versions
    fresh_chat = _chat(
        api, chat_session["id"],
        "请按新发布的审计规则记录：本轮退款复盘采用 2026 年第二季度口径，六月作为月度切片；"
        "用一句话确认即可。",
    )
    fresh_result, _ = _wait_run_with_user_actions(
        api, fresh_chat["run"]["id"], chat_session["id"],
        clarification_answer="采用 2026 年第二季度口径，六月作为月度切片。",
        max_actions=2,
    )
    assert fresh_result["outcome"] == "succeeded"
    work = api.request("GET", f"/api/v1/runs/{fresh_chat['run']['id']}/work-items").json()["items"]
    assert work[0]["agent_version_id"] == new_audit_version
    assert new_refund_version != plan_versions[refund_ops]
    record_property("scene_5_version_freezing", "passed")

    # 7. Restart while a Runner is active: recover the same WorkItem and keep a reliable terminal event.
    restart_chat = _chat(
        api, chat_session["id"],
        "退款账龄分布已经确认：0—7 天占 83%，8—30 天占 14%，超过 30 天占 3%。"
        "请将这些数据压缩成一句管理层摘要；这是一次约需数秒的摘要计算，无需补充信息。",
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        runners = api.request("GET", f"/api/v1/runs/{restart_chat['run']['id']}/runners").json()["items"]
        if runners and runners[0]["status"] == "running":
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"Runner never entered running state before restart: {runners}")
    _restart("runtime-worker-test")
    recovered = api.wait_run(restart_chat["run"]["id"], timeout=90)
    assert recovered["outcome"] == "succeeded"
    work_items_after = api.request("GET", f"/api/v1/runs/{restart_chat['run']['id']}/work-items").json()["items"]
    assert len(work_items_after) == 1
    runners_after = api.request("GET", f"/api/v1/runs/{restart_chat['run']['id']}/runners").json()["items"]
    assert len(runners_after) == 2
    assert {runner["status"] for runner in runners_after} == {"lost", "succeeded"}
    assert {runner["execution_epoch"] for runner in runners_after} == {1, 2}
    terminal_events = [event for event in _events(api, restart_chat["run"]["id"]) if event["event_type"] == "run.completed"]
    assert len(terminal_events) == 1

    # The actual OTLP exporter delivered both Runtime spans and an OTel Link to
    # the Collector; the database trace above remains the user-safe projection.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        otel_logs = _collector_logs()
        if "model_generation" in otel_logs and "invoke_agent.link" in otel_logs:
            break
        time.sleep(1)
    else:
        raise AssertionError("OTel Collector did not receive Runtime generation and AgentInvocation link spans")
    record_property("scene_7_worker_recovery_and_otel", "passed")


def test_required_contract_surface(phase_api: PhaseOneAPI):
    schema = phase_api.client.get("/openapi.json").json()["paths"]
    required = {
        "/api/v1/accounts/me", "/api/v1/accounts/me/change-user-id",
        "/api/v1/admin/audit-events", "/api/v1/accounts/me/audit-events",
        "/api/v1/builder/audit-events", "/api/v1/runs/{run_id}/events",
        "/api/v1/auth/enterprise/start", "/api/v1/auth/enterprise/callback",
        "/api/v1/agents/{agent_id}/versions/{version_id}",
    }
    assert required <= schema.keys()


def test_signed_cursor_and_error_envelope(phase_api: PhaseOneAPI, assembled_world):
    first = phase_api.request("GET", "/api/v1/agents", params={"limit": 1}).json()
    assert len(first["items"]) == 1 and first["next_cursor"]
    second = phase_api.request(
        "GET", "/api/v1/agents", params={"limit": 1, "cursor": first["next_cursor"]},
    ).json()
    assert second["items"] and second["items"][0]["id"] != first["items"][0]["id"]

    tampered = first["next_cursor"][:-1] + ("A" if first["next_cursor"][-1] != "A" else "B")
    response = phase_api.client.get(
        "/api/v1/agents", params={"limit": 1, "cursor": tampered}, headers=phase_api.auth,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]

    rebound = phase_api.client.get(
        "/api/v1/agents",
        params={"limit": 1, "cursor": first["next_cursor"], "status": "published"},
        headers=phase_api.auth,
    )
    assert rebound.status_code == 422
    assert rebound.json()["error"]["code"] == "invalid_cursor"

    missing = phase_api.client.get(
        "/api/v1/runs/00000000-0000-0000-0000-000000000000", headers=phase_api.auth,
    )
    assert missing.status_code == 404
    assert set(missing.json()["error"]) == {
        "code", "message", "request_id", "retryable", "details",
    }
