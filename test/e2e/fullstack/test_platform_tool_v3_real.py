"""Real-model, real-provider, real-MySQL/Redis/Workspace/Chromium acceptance."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import time

import pytest

from conftest import MyClawAPI


pytestmark = pytest.mark.fullstack
TERMINAL = {"completed", "cancelled", "failed"}


def _compose(*args: str) -> None:
    test_root = Path(__file__).resolve().parents[2]
    compose_file = Path(os.getenv("MYCLAW_COMPOSE_FILE", test_root / "docker-compose.test.yml"))
    aliases = {
        "runtime-worker-test": os.getenv("MYCLAW_RUNTIME_WORKER_SERVICE", "runtime-worker-test"),
        "sandbox-worker-test": os.getenv("MYCLAW_SANDBOX_WORKER_SERVICE", "sandbox-worker-test"),
        "browser-worker-test": os.getenv("MYCLAW_BROWSER_WORKER_SERVICE", "browser-worker-test"),
    }
    command = ["docker", "compose"]
    if os.getenv("MYCLAW_COMPOSE_ENV_FILE"):
        command.extend(["--env-file", os.environ["MYCLAW_COMPOSE_ENV_FILE"]])
    project = os.getenv("MYCLAW_COMPOSE_PROJECT") or os.getenv("COMPOSE_PROJECT_NAME")
    if project:
        command.extend(["-p", project])
    command.extend(["-f", str(compose_file), *(aliases.get(item, item) for item in args)])
    subprocess.run(command, cwd=test_root, check=True, timeout=180)


def _settle_with_tool_approvals(
    api: MyClawAPI, session_id: str, run_id: str, *, timeout: float = 600,
) -> tuple[dict, list[dict]]:
    deadline = time.monotonic() + timeout
    approved: list[dict] = []
    decided: set[str] = set()
    while time.monotonic() < deadline:
        run = api.request("GET", f"/api/v1/runs/{run_id}").json()
        if run["status"] in TERMINAL:
            return run, approved
        actions = api.request(
            "GET", f"/api/v1/sessions/{session_id}/required-actions",
            params={"status": "pending"},
        ).json()["items"]
        for action in actions:
            if action.get("run_id") != run_id or action["id"] in decided:
                continue
            if action["type"] in {"tool", "session_grant"}:
                api.approve(action)
            elif action["type"] == "tool_confirmation":
                api.request(
                    "POST", f"/api/v1/tool-confirmations/{action['id']}/approve",
                    idem=True, headers={"If-Match": f'W/"{action["version"]}"'},
                    json={
                        "arguments_hash": action["arguments_hash"],
                        "reason": "Real UAT exact-call approval",
                    },
                )
            else:
                continue
            decided.add(action["id"])
            approved.append(action)
        time.sleep(0.25)
    raise AssertionError(f"Run {run_id} did not settle after approvals")


def _run(
    api: MyClawAPI, agent_id: str, prompt: str, *, approve: bool = False,
) -> tuple[dict, dict, list[dict], list[dict]]:
    session = api.create_session(agent_id)
    command = api.chat(session["id"], prompt)
    if approve:
        run, approvals = _settle_with_tool_approvals(api, session["id"], command["run"]["id"])
    else:
        run, approvals = api.wait_run(command["run"]["id"]), []
    assert run["status"] == "completed", run
    return session, run, api.invocations(run["id"]), approvals


def _assert_expected_evidence(api: MyClawAPI, run: dict, invocations: list[dict], expected: str) -> None:
    matching = [item for item in invocations if item["tool_key"] == expected]
    assert matching, {"expected": expected, "observed": [item["tool_key"] for item in invocations]}
    assert all(item["status"] == "succeeded" for item in matching), matching
    assert all(item["tool_version_id"] and item["tool_version_content_hash"] for item in matching)
    assert any((item["result"] or {}).get("evidence") for item in matching), matching
    events = api.request("GET", f"/api/v1/runs/{run['id']}/events").json()["items"]
    assert not any(item["event_type"] == "tool_invocation.rejected" for item in events), events


@pytest.mark.timeout(1800)
def test_real_connections_search_fetch_mcp_browser_and_secret_boundaries(
    phase_api: MyClawAPI, assembled_world: dict, record_testsuite_property,
):
    api = phase_api
    assert assembled_world["conformance"]["status"] == "passed"
    assert assembled_world["bocha"]["test"]["provider"] == "bocha"
    assert assembled_world["brave"]["test"]["provider"] == "brave"
    assert assembled_world["mcp"]["test"]["details"]["tool_count"] >= 1
    assert assembled_world["browser"]["connection_type"] == "browser"

    cases = (
        (assembled_world["general"]["id"], "请搜索过去一周 OpenAI API 的公开中文资讯，返回真实链接和日期。", "bocha", "pw"),
        (assembled_world["general"]["id"], "Find public English sources from the past month about HTTP semantics and cite the URLs.", "bocha", "pm"),
        (assembled_world["brave_agent"]["id"], "请搜索过去一天关于 Chromium 的英文公开资料，并引用真实网址。", "brave", "pd"),
        (assembled_world["brave_agent"]["id"], "Search public English sources from the past year about Model Context Protocol and cite links.", "brave", "py"),
    )
    for agent_id, prompt, provider, freshness in cases:
        _, run, invocations, _ = _run(api, agent_id, prompt)
        _assert_expected_evidence(api, run, invocations, "web_search")
        searched = next(item for item in invocations if item["tool_key"] == "web_search")
        assert searched["provider"] == provider
        assert searched["arguments"].get("freshness") == freshness
        results = searched["result"]["data"]["results"]
        assert results and all(item["url"].startswith(("http://", "https://")) for item in results)

    _, html_run, html_calls, _ = _run(
        api, assembled_world["general"]["id"],
        "读取并摘要 https://www.rfc-editor.org/rfc/rfc9110.html 的公开正文，附可点击引用和校验依据。",
    )
    _assert_expected_evidence(api, html_run, html_calls, "web_fetch")
    _, pdf_run, pdf_calls, _ = _run(
        api, assembled_world["general"]["id"],
        "读取 https://www.rfc-editor.org/rfc/rfc9110.pdf 这份公开 PDF，摘要其主题并附引用。",
    )
    _assert_expected_evidence(api, pdf_run, pdf_calls, "web_fetch")
    assert next(item for item in pdf_calls if item["tool_key"] == "web_fetch")["result"]["data"]["mime_type"] == "application/pdf"

    connections = api.request("GET", "/api/v1/tool-connections").json()["items"]
    serialized = str(connections)
    assert os.environ["MYCLAW_BOCHA_API_KEY"] not in serialized
    assert os.environ["MYCLAW_BRAVE_API_KEY"] not in serialized
    record_testsuite_property("real_external_connections", "bocha,brave,web-fetch,mcp,chromium")


@pytest.mark.timeout(7200)
def test_thirty_natural_language_blind_capability_choices(
    phase_api: MyClawAPI, assembled_world: dict, record_testsuite_property,
):
    api = phase_api
    agent_id = assembled_world["general"]["id"]

    memory_session = api.create_session(agent_id)
    memory_command = api.chat(
        memory_session["id"],
        f"请长期记住：我的验收代号是 aurora-{assembled_world['suffix']}，这是我本人明确提供的事实。",
    )
    memory_run = api.wait_run(memory_command["run"]["id"])
    assert memory_run["status"] == "completed", memory_run
    assert any(
        item["tool_key"] == "memory_propose" and item["status"] == "succeeded"
        for item in api.invocations(memory_run["id"])
    )

    cases: list[tuple[str, str]] = []
    cases.extend((prompt, "list_files") for prompt in (
        "列出当前工作区根目录的条目和文件元数据，最多二十项。",
        "请盘点这个会话工作目录顶层有哪些文件或目录，并报告大小。",
        "查看工作空间的目录清单；我现在不需要读取文件正文。",
        "给我当前工作区的分页目录概览，先看第一页。",
        "Inspect the top-level workspace entries and return their metadata, without reading contents.",
    ))
    cases.extend((prompt, "search_files") for prompt in (
        "在当前工作区全文查找短语 quarterly-acceptance，并给出命中路径和行号。",
        "搜索工作目录中名称或正文含 recovery-boundary 的文件，限制十条结果。",
        "请用工作区内容检索寻找字符串 provenance-check，不要运行 shell。",
        "在所有 Markdown 文件里查找 audit evidence，返回行号与片段。",
        "Search workspace paths and file text for the regular expression release[-_]gate and show line matches.",
    ))
    cases.extend((prompt, "web_search") for prompt in (
        "搜索本周公开网络中关于 Python 3.12 的最新资料并列出真实网址。",
        "请在互联网上查找过去一个月的 Chromium 更新信息，附来源链接。",
        "Find current public sources about JSON Schema Draft 2020-12 and cite the result URLs.",
        "检索过去一年 Model Context Protocol 的公开资料，不要访问本地文件。",
        "Search the live web for recent HTTP security guidance and return dated links.",
    ))
    cases.extend((prompt, "web_fetch") for prompt in (
        "打开这个已知公开地址并提取正文：https://www.rfc-editor.org/rfc/rfc9110.html",
        "读取并概括 https://www.rfc-editor.org/rfc/rfc9110.pdf 的正文。",
        "Fetch the public text at https://www.rfc-editor.org/rfc/rfc8259.txt and cite it.",
        "提取 https://example.com/ 的网页标题和干净正文，并给出校验值。",
        "Retrieve the known public URL https://www.iana.org/domains/reserved and cite its final URL.",
    ))
    cases.extend((prompt, "browser_navigate") for prompt in (
        "请在隔离的交互式浏览器中打开 https://example.com/ 并报告页面标题。",
        "用独立浏览会话导航到 https://httpbin.org/forms/post，先不要提交表单。",
        "Open https://www.iana.org/domains/reserved in the isolated interactive browser and report the page.",
        "在隔离 Chromium 中访问 https://www.rfc-editor.org/，不要复用任何用户登录状态。",
        "Navigate an isolated browser to https://example.com/ and inspect the page origin.",
    ))
    cases.extend((prompt, "memory_search") for prompt in (
        "从我的长期记忆中查找验收代号，并说明事实来源。",
        "我之前明确告诉你的 aurora 验收代号是什么？请查询持久记忆证据。",
        "检索我的跨会话个人事实，找出验收代号及其来源消息。",
        "Search durable user memory for my acceptance codename and return provenance.",
        "请查询已生效记忆中与验收代号有关的事实，不要凭对话猜测。",
    ))
    assert len(cases) == 30

    passed = 0
    for prompt, expected in cases:
        _, run, invocations, _ = _run(api, agent_id, prompt)
        _assert_expected_evidence(api, run, invocations, expected)
        passed += 1
    assert passed == 30
    record_testsuite_property("blind_tool_selection", "30/30")


@pytest.mark.timeout(2400)
def test_real_multistep_chat_browser_memory_and_dynamic_mcp(
    phase_api: MyClawAPI, assembled_world: dict, record_testsuite_property,
):
    api = phase_api
    agent_id = assembled_world["general"]["id"]

    workspace_session = api.create_session(agent_id)
    command = api.chat(
        workspace_session["id"],
        "在工作区创建脚本 calc.py，使其打印 17 加 25 的结果；真实执行它，把输出写入 result.txt，"
        "最后将 result.txt 正式发布为可下载成果。只在每一步有真实结果后继续。",
    )
    run, approvals = _settle_with_tool_approvals(api, workspace_session["id"], command["run"]["id"])
    assert run["status"] == "completed", run
    calls = api.invocations(run["id"])
    assert {"write_file", "execute", "publish_artifact"} <= {item["tool_key"] for item in calls}
    assert approvals
    artifacts = api.request(
        "GET", f"/api/v1/sessions/{workspace_session['id']}/artifacts",
        params={"status": "published"},
    ).json()["items"]
    assert artifacts
    downloaded = api.request("GET", f"/api/v1/artifacts/{artifacts[-1]['id']}/download").content
    assert hashlib.sha256(downloaded).hexdigest() == artifacts[-1]["checksum"]

    _, evidence_run, evidence_calls, _ = _run(
        api, agent_id,
        "先在公开网络搜索 JSON Schema 2020-12 的权威资料，再读取最相关结果正文，最后用可点击引用作答。",
    )
    assert {"web_search", "web_fetch"} <= {item["tool_key"] for item in evidence_calls}
    _assert_expected_evidence(api, evidence_run, evidence_calls, "web_fetch")

    browser_session = api.create_session(agent_id)
    browser_command = api.chat(
        browser_session["id"],
        "在隔离浏览器打开 https://httpbin.org/forms/post，观察表单，在 custname 字段输入 Real UAT，"
        "提交前等待我的精确确认；确认后提交表单，再保存一张页面截图作为证据。",
    )
    browser_run, browser_approvals = _settle_with_tool_approvals(
        api, browser_session["id"], browser_command["run"]["id"],
    )
    assert browser_run["status"] == "completed", browser_run
    browser_calls = api.invocations(browser_run["id"])
    browser_keys = [item["tool_key"] for item in browser_calls]
    assert {"browser_navigate", "browser_observe", "browser_interact", "browser_screenshot"} <= set(browser_keys)
    assert any(action["type"] in {"tool", "tool_confirmation"} for action in browser_approvals)
    assert any(
        item["result"].get("artifact_ids")
        for item in browser_calls if item["tool_key"] == "browser_screenshot"
    )

    _, mcp_run, mcp_calls, mcp_approvals = _run(
        api, agent_id,
        "通过平台已授权的开发文档连接器查找 OpenAI Responses API 的工具调用说明，引用返回的官方文档地址。",
        approve=True,
    )
    remote = [item for item in mcp_calls if item["tool_key"].startswith("mcp__")]
    assert remote and all(item["status"] == "succeeded" for item in remote)
    assert mcp_approvals
    assert all(item["connection_revision_id"] for item in remote)
    record_testsuite_property("multistep_real_tools", "workspace,execute,artifact,search,fetch,browser,memory,mcp")


@pytest.mark.timeout(1800)
def test_chat_working_plan_finishes_after_report_artifact_is_published(
    phase_api: MyClawAPI, real_llm_world: dict, record_testsuite_property,
):
    """Regression for Session 019fe50d: a completed Run must not leave a 4/5 plan."""
    api = phase_api
    session = api.create_session(real_llm_world["general"]["id"])

    command = api.chat(
        session["id"],
        "请完整执行这个无需补充信息的多步骤任务：先写个随机数的脚本，然后跑一下，如果是单数抓取百度首页，"
        "如果是双数抓取新浪首页，最后把抓取到的内容写成报告并发布为可下载成果",
    )
    run, approvals = _settle_with_tool_approvals(
        api, session["id"], command["run"]["id"],
    )
    assert run["status"] == "completed", run

    invocations = api.invocations(run["id"])
    observed_tools = {
        item["tool_key"] for item in invocations if item["status"] == "succeeded"
    }
    assert {"write_file", "execute", "web_fetch", "publish_artifact"} <= observed_tools
    assert approvals

    artifacts = api.request(
        "GET", f"/api/v1/sessions/{session['id']}/artifacts",
        params={"status": "published"},
    ).json()["items"]
    assert artifacts

    plans = api.request(
        "GET", f"/api/v1/runs/{run['id']}/working-plans",
    ).json()["items"]
    assert len(plans) == 1, plans
    final_plan = plans[0]
    assert final_plan["status"] == "closed", final_plan
    assert final_plan["items"], final_plan
    assert all(
        item["status"] == "completed" for item in final_plan["items"]
    ), final_plan

    events = api.request("GET", f"/api/v1/runs/{run['id']}/events").json()["items"]
    artifact_sequence = max(
        item["sequence"] for item in events if item["event_type"] == "artifact.published"
    )
    completed_plan_updates = [
        item for item in events
        if item["event_type"] == "working_plan.updated"
        and item["sequence"] > artifact_sequence
        and item["payload"].get("items")
        and all(step["status"] == "completed" for step in item["payload"]["items"])
    ]
    assert completed_plan_updates, {
        "reason": "WorkingPlan was not reconciled after the report Artifact was published",
        "artifact_sequence": artifact_sequence,
        "final_plan": final_plan,
    }
    record_testsuite_property(
        "chat_working_plan_terminal_consistency",
        "artifact-published-before-final-plan-update,all-steps-completed",
    )


@pytest.mark.timeout(1800)
def test_plan_zero_side_effect_before_approval_restart_and_real_implementation(
    phase_api: MyClawAPI, assembled_world: dict, record_testsuite_property,
):
    api = phase_api
    session = api.create_session(assembled_world["general"]["id"])
    entered = api.request(
        "POST", f"/api/v1/sessions/{session['id']}/plan-mode", idem=True,
        headers={"If-Match": f'W/"{session["version"]}"'},
        json={
            "goal": "先调查并规划：批准后在工作区创建 plan-result.txt，正文为 approved-real-plan，"
                    "再把它发布成可下载成果。批准前不得产生任何写入或执行副作用。",
        },
    ).json()
    run_id = entered["run"]["id"]
    action = api.wait_action(session["id"], "plan_implementation")
    plans = api.request("GET", f"/api/v1/runs/{run_id}/working-plans").json()["items"]
    assert len(plans) == 1 and plans[0]["items"]
    before = api.invocations(run_id)
    forbidden = {"write_file", "apply_patch", "delete_path", "execute", "publish_artifact", "browser_interact"}
    assert not any(item["tool_key"] in forbidden and item["status"] == "succeeded" for item in before)
    assert api.request(
        "GET", f"/api/v1/sessions/{session['id']}/artifacts", params={"status": "published"},
    ).json()["items"] == []

    _compose("restart", "runtime-worker-test")
    plan = api.request("GET", f"/api/v1/working-plans/{plans[0]['id']}").json()
    api.request(
        "POST", f"/api/v1/working-plans/{plan['id']}/approve-implementation", idem=True,
        headers={"If-Match": f'"{plan["version"]}"'},
        json={"expected_revision": plan["revision"], "subject_hash": action["subject_hash"]},
    )
    run, approvals = _settle_with_tool_approvals(api, session["id"], run_id)
    assert run["status"] == "completed", run
    after = api.invocations(run_id)
    assert {"write_file", "publish_artifact"} <= {item["tool_key"] for item in after}
    artifacts = api.request(
        "GET", f"/api/v1/sessions/{session['id']}/artifacts", params={"status": "published"},
    ).json()["items"]
    assert artifacts
    content = api.request("GET", f"/api/v1/artifacts/{artifacts[-1]['id']}/download").content
    assert content == b"approved-real-plan"
    record_testsuite_property("plan_gate", "zero-side-effect-before-approval,real-after-approval,restart-recovery")


@pytest.mark.timeout(1800)
def test_group_real_model_collaboration_natural_stop_and_restart(
    phase_api: MyClawAPI, assembled_world: dict, record_testsuite_property,
):
    api = phase_api
    members = [item["id"] for item in assembled_world["group_agents"]]
    session = api.create_session(assembled_world["reporter"]["id"])
    group = api.request(
        "POST", f"/api/v1/sessions/{session['id']}/groups", idem=True,
        json={
            "member_agent_ids": members,
            "reporter_agent_id": assembled_world["reporter"]["id"],
            "goal": "共同评审一次平台工具发布：产品分析范围与用户价值，工程师评估实现和恢复，"
                    "验收负责人汇总安全、证据和发布结论。使用真实协作消息，自然完成后停止。",
            "shared_context_refs": [],
        },
    ).json()
    group_run = api.request("GET", f"/api/v1/groups/{group['id']}/runs").json()["items"][0]
    _compose("restart", "runtime-worker-test")
    run = api.wait_run(group_run["run_id"], timeout=900)
    assert run["status"] == "completed", run
    messages = api.request(
        "GET", f"/api/v1/group-runs/{group_run['id']}/messages", params={"limit": 100},
    ).json()["items"]
    speaking_agents = {item["sender_agent_id"] for item in messages if item["sender_agent_id"]}
    assert len(speaking_agents) >= 2, messages
    timeline = api.request("GET", f"/api/v1/groups/{group['id']}/timeline").json()["items"]
    assert any(item["event_type"] == "run.completed" for item in timeline)
    record_testsuite_property("group_gate", "real-model-collaboration,natural-stop,restart-recovery")
