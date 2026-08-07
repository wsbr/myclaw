from __future__ import annotations

import os
import re
import time

import pytest
from playwright.sync_api import Page, expect


pytestmark = pytest.mark.fullstack


@pytest.mark.timeout(900)
def test_browser_chat_plan_and_admin_surfaces(
    page: Page, assembled_world: dict, phase_api, record_testsuite_property,
):
    """Exercise the current UI against the real API, workers and database."""
    record_property = record_testsuite_property
    record_property("commit_sha", os.getenv("MYCLAW_ACCEPTANCE_COMMIT_SHA", "uncommitted"))
    record_property("model", os.getenv("MYCLAW_REAL_LLM_MODEL", "mock"))
    ui_url = os.getenv("MYCLAW_FULLSTACK_UI_URL", "http://localhost:8081").rstrip("/")
    browser_errors: list[str] = []
    page.on("pageerror", lambda error: browser_errors.append(str(error)))

    page.goto(ui_url, wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="MyClaw", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="企业身份登录", exact=True)).to_be_visible()
    page.get_by_role("textbox", name="邮箱", exact=True).fill(
        os.getenv("MYCLAW_TEST_ADMIN_EMAIL", "admin@test.myclaw.local")
    )
    page.get_by_role("textbox", name="密码", exact=True).fill(
        os.getenv("MYCLAW_TEST_ADMIN_PASSWORD", "testpassword123")
    )
    page.get_by_role("button", name="登录", exact=True).click()
    expect(page.get_by_role("heading", name="Agents", exact=True)).to_be_visible(timeout=10_000)
    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_role("button", name="Platform Audit", exact=True)).to_be_visible(timeout=10_000)

    admin_surfaces = (
        ("我的账号", "我的账号"),
        ("用户管理", "用户管理"),
        ("Tool 管理", "Tool 管理"),
        ("Skill 管理", "Skill 管理"),
        ("模型管理", "模型注册表"),
        ("Policy / Sandbox", "Policy 与 Sandbox"),
        ("Platform Audit", "Platform Audit"),
    )
    for nav_name, heading in admin_surfaces:
        page.get_by_role("button", name=nav_name, exact=True).click()
        expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible(timeout=10_000)

    page.get_by_role("button", name="Agents", exact=True).click()
    agent_name = f"个人助理 {assembled_world['suffix']}"
    agent_card = page.locator("div.bg-card").filter(has_text=agent_name)
    expect(agent_card).to_have_count(1)
    agent_card.get_by_role("button", name="对话", exact=True).click()
    expect(page.get_by_text(agent_name, exact=True)).to_be_visible()

    composer = page.get_by_placeholder("输入消息；Enter 发送，Shift+Enter 换行", exact=True)
    ui_message = "请用一句话确认已收到第二季度退款异常复盘任务，本次只需回复确认。"
    composer.fill(ui_message)
    page.get_by_role("button", name=re.compile("发送$")).click()
    expect(page).to_have_url(re.compile(r"/agents/[0-9a-f-]{36}/chat/[0-9a-f-]{36}$"), timeout=15_000)
    session_id = page.url.rstrip("/").split("/")[-1]
    deadline = time.monotonic() + phase_api.run_timeout
    assistant_message = None
    while time.monotonic() < deadline:
        messages = phase_api.request("GET", f"/api/v1/sessions/{session_id}/messages").json()["items"]
        user_message = next(
            (item for item in reversed(messages) if item["sender_type"] == "user"
             and item["content"] == ui_message),
            None,
        )
        if user_message:
            assistant_message = next(
                (item for item in messages if item["sender_type"] == "agent"
                 and item["run_id"] == user_message["run_id"] and item["content"].strip()),
                None,
            )
        if assistant_message:
            break
        time.sleep(0.25)
    assert assistant_message is not None
    visible_excerpt = assistant_message["content"].strip().replace("`", "")[:40]
    expect(page.get_by_text(re.compile(re.escape(visible_excerpt)))).to_be_visible(timeout=15_000)

    page.get_by_role("button", name="Plan", exact=True).click()
    page.get_by_placeholder("描述需要只读调查与规划的长任务目标", exact=True).fill(
        "复杂任务 plan-agent-network：已确认第二季度支付侧退款 100 笔、订单侧 97 笔，"
        "只读规划核对 3 笔差异，现有输入足够；按需调用专业候选复核，"
        "再把结论写入可下载的《验收报告 2026.txt》，正文只写 artifact-e2e 并正式发布。"
    )
    page.get_by_role("button", name=re.compile("进入只读 Plan$")).click()
    expect(page.get_by_text("Plan 权限与执行", exact=True)).to_be_visible(timeout=10_000)
    expect(page.get_by_text(re.compile(r"^plan_readonly · r\d+$")).first).to_be_visible()
    expect(page.get_by_text("revision 1", exact=True)).to_be_visible(
        timeout=int(phase_api.run_timeout * 1000),
    )
    for section in ("WorkingPlan", "Required Action", "协作者", "Artifacts"):
        expect(page.get_by_text(section, exact=True)).to_be_visible()
    plan_runs = phase_api.request(
        "GET", f"/api/v1/sessions/{session_id}/runs", params={"mode": "plan"},
    ).json()["items"]
    assert plan_runs
    plan = phase_api.request(
        "GET", f"/api/v1/runs/{plan_runs[0]['id']}/working-plans",
    ).json()["items"][0]
    assert plan["summary"].strip() and len(plan["items"]) >= 2
    expect(page.get_by_text(re.compile(re.escape(plan["items"][0]["text"])))).to_be_visible(
        timeout=int(phase_api.run_timeout * 1000),
    )
    direction = page.get_by_placeholder(
        "继续讨论或修改方向，例如：先验证迁移回滚，再实现 UI", exact=True,
    )
    direction.fill("修改方向：增加失败回滚验证，再实施交付。")
    page.get_by_role("button", name="修改方向", exact=True).click()
    expect(page.get_by_text("revision 2", exact=True)).to_be_visible(
        timeout=int(phase_api.run_timeout * 1000),
    )
    page.get_by_role(
        "button", name="批准并在同一 Session 实施", exact=True,
    ).click()
    expect(page.get_by_text(re.compile(r"^direct · r\d+$")).first).to_be_visible(
        timeout=15_000,
    )
    plan_deadline = time.monotonic() + phase_api.run_timeout * 2
    plan_approvals = 0
    artifact_name = page.get_by_text("验收报告 2026.txt", exact=True)
    download_artifact = page.get_by_role("button", name=re.compile(r"^下\s*载$"))
    while time.monotonic() < plan_deadline:
        if download_artifact.count() and download_artifact.first.is_visible():
            break
        approve_tool = page.get_by_role("button", name=re.compile(r"^批\s*准$"))
        if approve_tool.count() and approve_tool.first.is_visible():
            approve_tool.first.click()
            plan_approvals += 1
        page.wait_for_timeout(500)
    expect(artifact_name.first).to_be_visible(timeout=1_000)
    expect(download_artifact.first).to_be_visible(timeout=1_000)
    assert plan_approvals <= 4, "Plan implementation exceeded the bounded Tool approval budget"

    assert not browser_errors
    record_property("chromium_ui_chat_plan_admin", "passed")
