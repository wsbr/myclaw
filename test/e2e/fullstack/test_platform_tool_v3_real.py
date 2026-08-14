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


def _assert_expected_evidence(
    api: MyClawAPI, run: dict, invocations: list[dict], expected: str,
    *, require_evidence: bool = True,
) -> None:
    matching = [item for item in invocations if item["tool_key"] == expected]
    assert matching, {"expected": expected, "observed": [item["tool_key"] for item in invocations]}
    succeeded = [item for item in matching if item["status"] == "succeeded"]
    assert succeeded, matching
    assert all(item["tool_version_id"] and item["tool_version_content_hash"] for item in matching)
    if require_evidence:
        assert any((item["result"] or {}).get("evidence") for item in succeeded), matching
    events = api.request("GET", f"/api/v1/runs/{run['id']}/events").json()["items"]
    assert not any(item["event_type"] == "tool_invocation.rejected" for item in events), events


def _runner_harnesses(api: MyClawAPI, run_id: str) -> list[dict]:
    runners = api.request("GET", f"/api/v1/runs/{run_id}/runners").json()["items"]
    return [
        api.request("GET", f"/api/v1/runners/{runner['id']}/trace").json()["harness"]
        for runner in runners
    ]


def _wait_for_emergent_group(
    api: MyClawAPI, session_id: str, *, timeout: float = 180,
) -> dict:
    deadline = time.monotonic() + timeout
    observed: list[dict] = []
    while time.monotonic() < deadline:
        observed = api.request(
            "GET", f"/api/v1/sessions/{session_id}/groups",
        ).json()["items"]
        matches = [
            item for item in observed
            if item.get("formation_mode") == "emergent_model_selected"
        ]
        if matches:
            return matches[0]
        time.sleep(0.25)
    raise AssertionError(f"No emergent Group appeared: {observed}")


@pytest.mark.timeout(1800)
def test_real_connections_search_fetch_mcp_browser_and_secret_boundaries(
    phase_api: MyClawAPI, assembled_world: dict, record_testsuite_property,
):
    api = phase_api
    assert assembled_world["conformance"]["status"] == "passed"
    assert assembled_world["model_version"]["capabilities"]["context_window"] == 1_000_000
    assert assembled_world["model_version"]["capabilities"]["max_input_tokens"] == 983_616
    assert assembled_world["model_version"]["capabilities"]["max_output_tokens"] == 65_536
    for connection in assembled_world["search_connections"]:
        assert connection["test"]["provider"] == connection["connection_type"]
        assert connection["test"]["details"]["result_count"] >= 1
        assert connection["test"]["details"]["request_id"]
        if connection["connection_type"] == "bailian":
            assert connection["test"]["details"]["model"] == "qwen-plus"
            assert connection["test"]["details"]["search_strategy"] == "turbo"
            assert connection["test"]["details"]["usage"].get("total_tokens", 0) > 0
    assert assembled_world["mcp"]["test"]["details"]["tool_count"] >= 1
    assert assembled_world["browser"]["connection_type"] == "browser"

    if assembled_world["search_provider"] == "bailian":
        bailian_agent = assembled_world["search_agents"]["bailian"]["id"]
        cases = (
            (bailian_agent, "请搜索过去一周 OpenAI API 的公开中文资讯，返回真实链接和日期。", "bailian", "week"),
            (bailian_agent, "Find public English sources from the past month about HTTP semantics and cite the URLs.", "bailian", "month"),
            (bailian_agent, "请搜索过去一天关于 Chromium 的英文公开资料，并引用真实网址。", "bailian", "day"),
            (bailian_agent, "Search public English sources from the past year about Model Context Protocol and cite links.", "bailian", "year"),
        )
    else:
        cases = (
            (assembled_world["search_agents"]["bocha"]["id"], "请搜索过去一周 OpenAI API 的公开中文资讯，返回真实链接和日期。", "bocha", "week"),
            (assembled_world["search_agents"]["bocha"]["id"], "Find public English sources from the past month about HTTP semantics and cite the URLs.", "bocha", "month"),
            (assembled_world["search_agents"]["brave"]["id"], "请搜索过去一天关于 Chromium 的英文公开资料，并引用真实网址。", "brave", "day"),
            (assembled_world["search_agents"]["brave"]["id"], "Search public English sources from the past year about Model Context Protocol and cite links.", "brave", "year"),
        )
    for agent_id, prompt, provider, freshness in cases:
        _, run, invocations, _ = _run(api, agent_id, prompt)
        _assert_expected_evidence(api, run, invocations, "web_search")
        assert len([
            item for item in invocations if item["tool_key"] == "web_search"
        ]) <= 2
        searched = next(
            item for item in invocations
            if item["tool_key"] == "web_search" and item["status"] == "succeeded"
        )
        assert searched["provider"] == provider
        assert searched["arguments"].get("freshness") == freshness
        results = searched["result"]["data"]["results"]
        assert results and all(item["url"].startswith(("http://", "https://")) for item in results)
        assert searched["result"]["data"]["original_query"]
        assert searched["result"]["data"]["effective_query"]
        harnesses = _runner_harnesses(api, run["id"])
        runtime_dates = {
            item["runtime_clock"].get("local_date")
            for item in harnesses if item["runtime_clock"].get("local_date")
        }
        assert runtime_dates
        assert any(
            local_date in searched["result"]["data"]["effective_query"]
            for local_date in runtime_dates
        )
        assert all(
            item["runtime_clock"].get("timezone") == "Asia/Shanghai"
            for item in harnesses if item["runtime_clock"]
        )
        projections = [
            projection for harness in harnesses
            for projection in harness["web_projection"]
        ]
        assert any(
            item["tool_name"] == "web_search" and item["omitted"]
            and item["projected_chars"] < item["full_chars"]
            for item in projections
        )
        assert all(
            item["hard_input_limit"] == 983_616
            for item in harnesses if item["hard_input_limit"] is not None
        )

    _, html_run, html_calls, _ = _run(
        api, assembled_world["general"]["id"],
        "读取并摘要 https://www.rfc-editor.org/rfc/rfc9110.html 的公开正文，附可点击引用和校验依据。",
    )
    _assert_expected_evidence(api, html_run, html_calls, "web_fetch")
    html_fetch = next(
        item for item in html_calls
        if item["tool_key"] == "web_fetch" and item["status"] == "succeeded"
    )
    assert html_fetch["result"]["data"]["text"]
    assert html_fetch["result"]["data"]["excerpts"]
    assert html_fetch["result"]["data"]["objective"]
    assert html_fetch["result"]["data"]["truncated"] is True
    assert len(html_fetch["result"]["data"]["text"]) <= 100_000
    html_projections = [
        projection for harness in _runner_harnesses(api, html_run["id"])
        for projection in harness["web_projection"]
    ]
    assert any(
        item["tool_name"] == "web_fetch" and item["omitted"]
        for item in html_projections
    )
    _, pdf_run, pdf_calls, _ = _run(
        api, assembled_world["general"]["id"],
        "读取 https://www.rfc-editor.org/rfc/rfc9110.pdf 这份公开 PDF，摘要其主题并附引用。",
    )
    _assert_expected_evidence(api, pdf_run, pdf_calls, "web_fetch")
    assert next(item for item in pdf_calls if item["tool_key"] == "web_fetch")["result"]["data"]["mime_type"] == "application/pdf"

    _, soft_404_run, soft_404_calls, _ = _run(
        api, assembled_world["general"]["id"],
        "只读取一次 https://httpbin.org/anything/404 并报告平台返回的类型化错误；不要改用其他网址。",
    )
    soft_404_fetch = next(
        item for item in soft_404_calls if item["tool_key"] == "web_fetch"
    )
    assert soft_404_fetch["status"] == "failed"
    assert soft_404_fetch["attempt_count"] == 1
    assert soft_404_fetch["result"]["error"]["code"] == "soft_404"

    connections = api.request("GET", "/api/v1/tool-connections").json()["items"]
    serialized = str(connections)
    secret_names = (
        ("MYCLAW_BAILIAN_API_KEY",)
        if assembled_world["search_provider"] == "bailian"
        else ("MYCLAW_BOCHA_API_KEY", "MYCLAW_BRAVE_API_KEY")
    )
    assert all(os.environ[name] not in serialized for name in secret_names)
    providers = ",".join(
        item["connection_type"] for item in assembled_world["search_connections"]
    )
    record_testsuite_property(
        "real_external_connections", f"{providers},web-fetch,mcp,chromium",
    )


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
        "搜索本周公开网络中关于 Python 3.12 的最新资料，返回一个真实网址；一次成功检索后立即作答，不要打开页面或重复检索。",
        "请在互联网上查找过去一个月的 Chromium 更新信息，附一个来源链接；一次成功检索后立即作答，不要打开页面或重复检索。",
        "Find one current public source about JSON Schema Draft 2020-12 and cite its URL; answer after one successful search without fetching or searching again.",
        "检索过去一年 Model Context Protocol 的一条公开资料，不要访问本地文件；一次成功检索后立即作答。",
        "Search the live web once for recent HTTP security guidance and return one dated link without fetching or repeating the search.",
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
        _assert_expected_evidence(
            api, run, invocations, expected,
            # This is a capability-choice matrix. Dedicated Memory UAT above
            # proves recall provenance; an empty semantic query is still a
            # valid, auditable memory_search choice here.
            require_evidence=expected != "memory_search",
        )
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
        "然后必须再次调用 browser_interact 点击提交按钮。不要用聊天文字询问确认；让平台为这个精确 click "
        "显示批准操作，在我批准后继续提交，最后调用 browser_screenshot 保存页面截图证据。不要在只输入字段后结束。",
    )
    browser_run, browser_approvals = _settle_with_tool_approvals(
        api, browser_session["id"], browser_command["run"]["id"],
    )
    assert browser_run["status"] == "completed", browser_run
    browser_calls = api.invocations(browser_run["id"])
    browser_keys = [item["tool_key"] for item in browser_calls]
    submitted = any(
        item["tool_key"] == "browser_interact"
        and item["status"] == "succeeded"
        and item["arguments"].get("action") == "click"
        for item in browser_calls
    )
    screenshot_recovery = False
    if not submitted or "browser_screenshot" not in browser_keys:
        recovery = api.chat(
            browser_session["id"],
            "你遗漏了明确要求的提交或截图。保持当前隔离浏览会话：先观察当前页面；如果表单尚未提交，"
            "必须调用 browser_interact 点击提交按钮，不要用聊天文字询问确认，让平台显示精确批准；"
            "批准并成功提交后调用 browser_screenshot 保存页面截图证据，完成两步后再结束。",
        )
        recovery_run, recovery_approvals = _settle_with_tool_approvals(
            api, browser_session["id"], recovery["run"]["id"],
        )
        assert recovery_run["status"] == "completed", recovery_run
        browser_calls.extend(api.invocations(recovery_run["id"]))
        browser_approvals.extend(recovery_approvals)
        browser_keys = [item["tool_key"] for item in browser_calls]
        submitted = any(
            item["tool_key"] == "browser_interact"
            and item["status"] == "succeeded"
            and item["arguments"].get("action") == "click"
            for item in browser_calls
        )
        screenshot_recovery = True
    assert {"browser_navigate", "browser_observe", "browser_interact", "browser_screenshot"} <= set(browser_keys)
    assert submitted
    assert any(action["type"] in {"tool", "tool_confirmation"} for action in browser_approvals)
    assert any(
        item["result"].get("artifact_ids")
        for item in browser_calls if item["tool_key"] == "browser_screenshot"
    )

    mcp_search_key = next(
        tool_key for tool_key in assembled_world["tools"]
        if tool_key.startswith("mcp__") and tool_key.endswith("__search_openai_docs")
    )
    _, mcp_run, mcp_calls, mcp_approvals = _run(
        api, agent_id,
        f"调用已授权的动态 MCP 工具 {mcp_search_key} 查找 OpenAI Responses API 的工具调用说明，"
        "并引用该工具返回的官方文档地址。不要改用 execute、web_search 或 web_fetch。",
        approve=True,
    )
    remote = [item for item in mcp_calls if item["tool_key"].startswith("mcp__")]
    assert remote and all(item["status"] == "succeeded" for item in remote)
    assert any(item["tool_key"] == mcp_search_key for item in remote)
    assert mcp_approvals
    assert all(item["connection_revision_id"] for item in remote)
    record_testsuite_property(
        "multistep_real_tools",
        "workspace,execute,artifact,search,fetch,browser,memory,mcp,"
        f"screenshot_recovery={str(screenshot_recovery).lower()}",
    )


@pytest.mark.timeout(1800)
def test_chat_working_plan_finishes_after_report_artifact_is_published(
    phase_api: MyClawAPI, real_llm_world: dict, record_testsuite_property,
):
    """Regression for Session 019fe50d: a completed Run must not leave a 4/5 plan."""
    api = phase_api
    session = api.create_session(real_llm_world["general"]["id"])

    command = api.chat(
        session["id"],
        "请完整执行这个无需补充信息的多步骤任务：先写个随机数的脚本，然后跑一下，如果是单数抓取 "
        "https://example.com，如果是双数抓取 https://example.org，最后把抓取到的内容写成报告并发布为可下载成果",
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
def test_group_real_model_emergence_async_handoff_and_restart(
    phase_api: MyClawAPI, emergent_group_world: dict, record_testsuite_property,
):
    api = phase_api
    recovery_id = emergent_group_world["recovery"]["id"]
    evidence_id = emergent_group_world["evidence"]["id"]
    security_id = emergent_group_world["security"]["id"]
    session = api.create_session(recovery_id)
    command = api.chat(
        session["id"],
        (
            "以下是本次审查的全部现有材料，请基于这些材料先完成审查，不要搜索工作区或记忆，"
            "也不要等待我补充信息；缺失内容直接列为未解决风险。事故事实：02:00 发现应用凭证"
            "泄露，02:10 禁用了旧凭证；工单能证明这两个时间点，但缺少 01:30—02:10 的访问"
            "日志。现有恢复方案：直接从 S-17 快照恢复数据库，运维仅口头确认快照干净，没有"
            "校验和或恶意变更排查记录；新凭证暂存在共享环境文件，恢复期间服务账号继续保留"
            "管理员权限；冒烟测试通过后立即恢复 100% 流量，没有灰度、回滚门槛或独立审批。"
            "请以关键事实能否被证据支持为主线审查该恢复方案，把安全风险作为需要专业复核的"
            "子问题，最后给我一份整合后的可执行结论，明确未解决风险与分歧。"
        ),
    )
    chat_run = api.wait_run(command["run"]["id"], timeout=600)
    assert chat_run["status"] == "completed", chat_run
    chat_events = api.request(
        "GET", f"/api/v1/runs/{chat_run['id']}/events",
    ).json()["items"]
    assert any(
        item["event_type"] == "group.emergence_selected"
        for item in chat_events
    ), chat_events

    group = _wait_for_emergent_group(api, session["id"])
    group_run = api.request("GET", f"/api/v1/groups/{group['id']}/runs").json()["items"][0]
    assert group_run["run"]["mode"] == "group", group_run
    assert group_run["shared_context"]["source_chat_run_id"] == chat_run["id"]
    assert {
        item["agent_id"] for item in group_run["members"]
    } == {recovery_id, evidence_id, security_id}, group_run
    _compose("restart", "runtime-worker-test")
    run, approvals = _settle_with_tool_approvals(
        api, session["id"], group_run["run"]["id"], timeout=900,
    )
    assert run["status"] == "completed", run
    invocations = api.request(
        "GET", f"/api/v1/runs/{run['id']}/agent-invocations",
    ).json()["items"]
    successful = [item for item in invocations if item["status"] == "succeeded"]
    invocation_edges = {
        (item["caller_agent_id"], item["target_agent_id"])
        for item in successful
    }
    assert invocation_edges == {
        (recovery_id, evidence_id),
        (evidence_id, security_id),
    }, invocations
    assert len(successful) == 2, invocations
    assert all(item["return_route"] == "group" for item in successful)
    messages = api.request(
        "GET", f"/api/v1/group-runs/{group_run['id']}/messages", params={"limit": 100},
    ).json()["items"]
    directed_agent_edges = [
        (item["sender_agent_id"], item["recipient_agent_id"])
        for item in messages
        if item["sender_agent_id"] and item["recipient_agent_id"]
        and item["visibility"] == "direct"
    ]
    for edge in (
        (recovery_id, evidence_id),
        (evidence_id, security_id),
        (security_id, evidence_id),
        (evidence_id, recovery_id),
    ):
        assert directed_agent_edges.count(edge) == 1, messages
    assert (recovery_id, security_id) not in directed_agent_edges, messages
    graph = api.request(
        "GET", f"/api/v1/group-runs/{group_run['id']}/interaction-graph",
    ).json()
    graph_invocations = {
        (item["source_agent_id"], item["target_agent_id"])
        for item in graph["edges"] if item["cause_type"] == "agent_invocation"
    }
    assert graph_invocations == invocation_edges, graph
    timeline = []
    after_sequence = 0
    while True:
        page = api.request(
            "GET", f"/api/v1/groups/{group['id']}/timeline",
            params={"after_sequence": after_sequence, "limit": 100},
        ).json()["items"]
        timeline.extend(page)
        if len(page) < 100:
            break
        after_sequence = page[-1]["sequence"]
    assert any(item["event_type"] == "run.completed" for item in timeline), timeline
    record_testsuite_property(
        "group_gate",
        (
            "post-chat-emergence,semantic-owner-selection,async-directed-handoffs,"
            f"no-duplicate-delivery,restart-recovery,tool-approvals={len(approvals)}"
        ),
    )
