"""Real browser acceptance for the administrator and Agent workspaces."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import time

import pytest
from playwright.sync_api import Page, expect

from conftest import MyClawAPI


pytestmark = pytest.mark.fullstack
TERMINAL = {"completed", "cancelled", "failed"}


def _screenshot(page: Page, root: Path, name: str) -> dict[str, str]:
    target = root / f"{name}.png"
    page.screenshot(path=target, full_page=True)
    return {
        "path": str(target),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }


def _run_for_message(api: MyClawAPI, session_id: str, content: str, timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = api.request("GET", f"/api/v1/sessions/{session_id}/messages").json()["items"]
        message = next(
            (
                item for item in reversed(items)
                if item["sender_type"] == "user" and item["content"] == content
            ),
            None,
        )
        if message and message.get("run_id"):
            return message["run_id"]
        time.sleep(0.25)
    raise AssertionError(f"UI message was not persisted: {content}")


def _settle_from_ui(
    page: Page,
    api: MyClawAPI,
    run_id: str,
    *,
    timeout: float,
    evidence_root: Path,
    screenshot_prefix: str,
) -> tuple[dict, int]:
    deadline = time.monotonic() + timeout
    approvals = 0
    captured = False
    while time.monotonic() < deadline:
        run = api.request("GET", f"/api/v1/runs/{run_id}").json()
        if run["status"] in TERMINAL:
            return run, approvals
        approve = page.get_by_role("button", name="批准", exact=True)
        if approve.count() and approve.first.is_visible():
            if not captured:
                _screenshot(page, evidence_root, f"{screenshot_prefix}-approval")
                captured = True
            approve.first.click()
            approvals += 1
            page.wait_for_timeout(750)
            continue
        page.wait_for_timeout(500)
    raise AssertionError(f"Run did not settle through browser approvals: {run_id}")


@pytest.mark.timeout(3000)
def test_real_browser_admin_chat_browser_plan_and_inspector(
    page: Page,
    assembled_world: dict,
    phase_api: MyClawAPI,
    record_testsuite_property,
):
    """Use a real Chromium UI session against the real model and every deployed worker."""
    api = phase_api
    ui_url = os.getenv("MYCLAW_FULLSTACK_UI_URL", "http://localhost:8081").rstrip("/")
    model_name = assembled_world["model_version"]["model_name"]
    evidence_root = Path(os.getenv(
        "MYCLAW_BROWSER_EVIDENCE_DIR",
        Path(__file__).resolve().parents[2] / "test-results" / "browser-acceptance"
        / assembled_world["suffix"],
    ))
    evidence_root.mkdir(parents=True, exist_ok=True)
    browser_errors: list[str] = []
    page.on("pageerror", lambda error: browser_errors.append(str(error)))
    page.set_viewport_size({"width": 1536, "height": 960})

    page.goto(ui_url, wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="MyClaw", exact=True)).to_be_visible()
    page.get_by_role("textbox", name="邮箱", exact=True).fill(
        os.getenv("MYCLAW_TEST_ADMIN_EMAIL", "admin@test.myclaw.local")
    )
    page.get_by_role("textbox", name="密码", exact=True).fill(
        os.getenv("MYCLAW_TEST_ADMIN_PASSWORD", "testpassword123")
    )
    page.get_by_role("button", name="登录", exact=True).click()
    expect(page.get_by_role("heading", name="Agents", exact=True)).to_be_visible(timeout=15_000)

    # The administrator sees the exact real model and its passed conformance result.
    page.goto(f"{ui_url}/models", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="模型注册表", exact=True)).to_be_visible()
    page.get_by_text(assembled_world["model"]["name"], exact=True).click()
    expect(page.get_by_text(model_name, exact=True)).to_be_visible(timeout=10_000)
    expect(page.get_by_text("passed", exact=True).first).to_be_visible()
    model_shot = _screenshot(page, evidence_root, "01-real-model")

    # Provider configuration, encrypted-credential state and a genuine request are visible in UI.
    page.goto(f"{ui_url}/tools", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Tool 管理", exact=True)).to_be_visible()
    for connection in (*assembled_world["search_connections"], assembled_world["mcp"]):
        expect(page.get_by_text(connection["connection_key"], exact=True)).to_be_visible()
    search_row = page.locator("tr").filter(
        has_text=assembled_world["search_connections"][0]["connection_key"]
    )
    search_row.get_by_role("button", name="真实测试", exact=True).click()
    expect(page.get_by_text(re.compile(r"真实连接成功 · \d+ms"))).to_be_visible(timeout=60_000)
    page.get_by_role("tab", name="Tool Catalog", exact=True).click()
    fetch_contract = page.get_by_role("button", name="查看 web_fetch 合同", exact=True)
    expect(fetch_contract).to_be_visible(timeout=30_000)
    fetch_contract.click()
    expect(page.get_by_text("Output Schema", exact=True)).to_be_visible()
    tool_shot = _screenshot(page, evidence_root, "02-tool-contract")
    page.get_by_role("button", name="Close", exact=True).click()

    # Chat selects web_fetch from natural language and exposes its immutable ledger and citation.
    page.goto(f"{ui_url}/agents", wait_until="domcontentloaded")
    agent_name = f"Evidence Assistant {assembled_world['suffix']}"
    card = page.locator("div.bg-card").filter(has_text=agent_name)
    expect(card).to_have_count(1)
    card.get_by_role("button", name="对话", exact=True).click()
    expect(page.get_by_text(agent_name, exact=True)).to_be_visible()
    chat_prompt = (
        "读取 https://www.rfc-editor.org/rfc/rfc9110.html 的公开正文，"
        "概括主题并附可点击来源；请依据真实工具结果回答。"
    )
    page.get_by_role("textbox", name="消息输入", exact=True).fill(chat_prompt)
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page).to_have_url(re.compile(r"/agents/[0-9a-f-]{36}/chat/[0-9a-f-]{36}$"), timeout=15_000)
    session_id = page.url.rstrip("/").split("/")[-1]
    chat_run_id = _run_for_message(api, session_id, chat_prompt)
    chat_run = api.wait_run(chat_run_id, timeout=600)
    assert chat_run["status"] == "completed", chat_run
    chat_calls = api.invocations(chat_run_id)
    assert any(item["tool_key"] == "web_fetch" and item["status"] == "succeeded" for item in chat_calls)
    expect(page.get_by_role("tab", name=re.compile(r"^Tool\s+\d+"))).to_be_visible(timeout=15_000)
    page.get_by_role("tab", name=re.compile(r"^Tool\s+\d+")).click()
    expect(page.get_by_text("web_fetch", exact=True)).to_be_visible(timeout=15_000)
    expect(page.get_by_text("ToolVersion", exact=False).first).to_be_visible()
    chat_shot = _screenshot(page, evidence_root, "03-chat-tool-ledger")

    # Sensitive form submission is approved from the product UI, not through the API test helper.
    page.get_by_role("tab", name="运行", exact=True).click()
    browser_prompt = (
        "在隔离浏览器打开 https://httpbin.org/forms/post，观察后在 custname 输入 Real Browser UAT；"
        "然后必须使用最新 browser_observe 中名为 Submit order 的 element_ref 发起 "
        "browser_interact click。不要用聊天文字询问确认；让平台为这个精确 click 显示批准操作，"
        "在我从页面批准后继续提交，并保存提交结果页面截图作为证据。"
    )
    page.get_by_role("textbox", name="消息输入", exact=True).fill(browser_prompt)
    page.get_by_role("button", name="发送", exact=True).click()
    browser_run_id = _run_for_message(api, session_id, browser_prompt)
    browser_run, browser_approvals = _settle_from_ui(
        page, api, browser_run_id, timeout=900, evidence_root=evidence_root,
        screenshot_prefix="04-browser",
    )
    assert browser_run["status"] == "completed", browser_run
    assert browser_approvals >= 1
    browser_calls = api.invocations(browser_run_id)
    browser_keys = {item["tool_key"] for item in browser_calls}
    assert {"browser_navigate", "browser_observe", "browser_interact", "browser_screenshot"} <= browser_keys
    submitted = [
        item for item in browser_calls
        if item["tool_key"] == "browser_interact"
        and item["status"] == "succeeded"
        and item["arguments"].get("action") == "click"
    ]
    assert submitted, browser_calls
    assert any(
        str((item["result"].get("data") or {}).get("url") or "").endswith("/post")
        for item in submitted
    ), submitted
    assert any(item["result"]["artifact_ids"] for item in browser_calls if item["tool_key"] == "browser_screenshot")
    page.get_by_role("tab", name=re.compile(r"^Tool\s+\d+")).click()
    expect(page.get_by_text("browser_screenshot", exact=True)).to_be_visible(timeout=15_000)
    browser_shot = _screenshot(page, evidence_root, "05-browser-result")

    # Plan remains read-only before the exact UI approval, survives approvals, then publishes a real artifact.
    page.get_by_role("button", name="Plan", exact=True).click()
    plan_prompt = (
        "先只读制定一个简洁实施计划：获批后创建 plan-ui-result.txt，正文精确为 approved-real-ui-plan，"
        "并将该文件发布为可下载 Artifact。批准前不得写入、执行或发布。"
    )
    page.get_by_role("textbox", name="消息输入", exact=True).fill(plan_prompt)
    page.get_by_role("button", name="进入只读 Plan", exact=True).click()
    expect(page.get_by_text("Plan 权限与执行", exact=True)).to_be_visible(timeout=15_000)
    expect(page.get_by_role("button", name="批准并在同一 Session 实施", exact=True)).to_be_visible(timeout=600_000)
    plan_runs = api.request(
        "GET", f"/api/v1/sessions/{session_id}/runs", params={"mode": "plan"},
    ).json()["items"]
    assert plan_runs
    plan_run_id = plan_runs[0]["id"]
    forbidden = {"write_file", "apply_patch", "delete_path", "execute", "publish_artifact", "browser_interact"}
    assert not any(
        item["status"] == "succeeded" and item["tool_key"] in forbidden
        for item in api.invocations(plan_run_id)
    )
    plan_approval_shot = _screenshot(page, evidence_root, "06-plan-readonly")
    page.get_by_role("button", name="批准并在同一 Session 实施", exact=True).click()
    plan_run, plan_approvals = _settle_from_ui(
        page, api, plan_run_id, timeout=900, evidence_root=evidence_root,
        screenshot_prefix="07-plan-tool",
    )
    assert plan_run["status"] == "completed", plan_run
    plan_calls = api.invocations(plan_run_id)
    assert {"write_file", "publish_artifact"} <= {item["tool_key"] for item in plan_calls}
    artifacts = api.request(
        "GET", f"/api/v1/sessions/{session_id}/artifacts", params={"status": "published"},
    ).json()["items"]
    plan_artifact = next(item for item in artifacts if item["name"] == "plan-ui-result.txt")
    downloaded = api.request("GET", f"/api/v1/artifacts/{plan_artifact['id']}/download").content
    assert downloaded == b"approved-real-ui-plan"
    expect(page.get_by_text("plan-ui-result.txt", exact=True).first).to_be_visible(timeout=15_000)
    expect(page.get_by_role("button", name="下载", exact=True).first).to_be_visible()
    plan_shot = _screenshot(page, evidence_root, "08-plan-artifact")

    assert not browser_errors
    record_testsuite_property("real_browser_uat", "admin,provider,chat,browser-approval,inspector,plan,artifact")
    record_testsuite_property("real_browser_model", model_name)
    record_testsuite_property("real_browser_screenshot_sha256", ",".join(
        item["sha256"] for item in (
            model_shot, tool_shot, chat_shot, browser_shot, plan_approval_shot, plan_shot,
        )
    ))
    record_testsuite_property("real_browser_tool_approvals", str(browser_approvals + plan_approvals))
