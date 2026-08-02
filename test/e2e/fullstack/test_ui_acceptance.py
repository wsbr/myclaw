from __future__ import annotations

import os
import re
import time

import pytest
from playwright.sync_api import Page, expect


pytestmark = pytest.mark.fullstack


@pytest.mark.timeout(900)
def test_browser_chat_plan_group_and_admin_surfaces(
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
    page.get_by_role("textbox", name="邮箱", exact=True).fill("admin@test.myclaw.local")
    page.get_by_role("textbox", name="密码", exact=True).fill("testpassword123")
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
    agent_name = f"Finance {assembled_world['suffix']}"
    agent_card = page.locator("div.bg-card").filter(has_text=agent_name)
    expect(agent_card).to_have_count(1)
    agent_card.get_by_role("button", name="对话", exact=True).click()
    expect(page.get_by_text(agent_name, exact=True)).to_be_visible()

    composer = page.get_by_placeholder("输入消息；Enter 发送，Shift+Enter 换行", exact=True)
    ui_message = "Reply briefly for UI browser acceptance; do not call any Tool."
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
    page.get_by_placeholder("描述需要规划的长任务目标", exact=True).fill(
        "UI Plan acceptance with direct text responses; do not call any Tool"
    )
    page.get_by_role("button", name=re.compile("生成 Plan$")).click()
    expect(page.get_by_text("awaiting_confirmation", exact=True)).to_be_visible(
        timeout=int(phase_api.run_timeout * 1000),
    )
    page.get_by_role("button", name="确认并冻结", exact=True).click()
    expect(page.get_by_role("row", name=re.compile("执行状态.*completed.*succeeded"))).to_be_visible(
        timeout=int(phase_api.run_timeout * 2 * 1000),
    )

    page.get_by_role("button", name="Group", exact=True).click()
    page.get_by_placeholder("输入 Group Formation 目标、后续 GroupRun 或定向补充", exact=True).fill(
        "UI Group acceptance with direct text responses; do not call any Tool"
    )
    page.get_by_role("button", name=re.compile("生成 Group Formation$")).click()
    expect(page.get_by_text("awaiting_confirmation", exact=True)).to_be_visible(
        timeout=int(phase_api.run_timeout * 1000),
    )
    page.get_by_role("button", name="确认并冻结", exact=True).click()
    page.get_by_role("tab", name="Timeline", exact=True).click()
    group_completed = page.get_by_text("GroupRun completed", exact=True)
    group_deadline = time.monotonic() + phase_api.run_timeout * 2
    confirmations_approved = 0
    while time.monotonic() < group_deadline:
        if group_completed.is_visible():
            break
        # Ant Design inserts visual spacing between two CJK characters, which is
        # also reflected in Chromium's accessible name ("批 准"). Match the
        # semantic button while allowing that presentation-only whitespace.
        approve = page.get_by_role("button", name=re.compile(r"批\s*准"))
        if approve.count() and approve.first.is_visible():
            with page.expect_response(
                lambda response: "/api/v1/tool-confirmations/" in response.url
                and response.request.method == "POST",
                timeout=15_000,
            ) as decision:
                approve.first.click()
            assert decision.value.ok, decision.value.text()
            confirmations_approved += 1
        page.wait_for_timeout(500)
    expect(group_completed).to_be_visible(timeout=1_000)
    assert confirmations_approved <= 4, "GroupRun exceeded the bounded Tool confirmation budget"

    assert not browser_errors
    record_property("chromium_ui_chat_plan_group_admin", "passed")
