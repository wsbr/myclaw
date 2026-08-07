"""Browser acceptance for the Phase-one Chat-only surface."""

from __future__ import annotations

import os
import re

import pytest
from playwright.sync_api import Page, expect


pytestmark = pytest.mark.fullstack


@pytest.mark.timeout(300)
def test_browser_chat_reconnect_clarification_approval_artifact_and_working_plan(
    page: Page, phase_api, assembled_world: dict,
):
    api = phase_api
    write_file = next(
        item for item in api.request("GET", "/api/v1/platform-tools").json()["items"]
        if item["tool_key"] == "write_file"
    )
    if write_file["confirmation_policy"] != "session_grant":
        api.request(
            "PATCH", f"/api/v1/platform-tools/{write_file['id']}", idem=True,
            headers={"If-Match": f'W/"{write_file["config_version"]}"'},
            json={"enabled": True, "confirmation_policy": "session_grant"},
        )

    browser_errors: list[str] = []
    page.on("pageerror", lambda error: browser_errors.append(str(error)))
    page.goto(
        os.getenv("MYCLAW_FULLSTACK_UI_URL", "http://localhost:8081").rstrip("/"),
        wait_until="domcontentloaded",
    )
    page.get_by_role("textbox", name="邮箱", exact=True).fill(
        os.getenv("MYCLAW_TEST_ADMIN_EMAIL", "admin@test.myclaw.local")
    )
    page.get_by_role("textbox", name="密码", exact=True).fill(
        os.getenv("MYCLAW_TEST_ADMIN_PASSWORD", "testpassword123")
    )
    page.get_by_role("button", name="登录", exact=True).click()
    expect(page.get_by_role("heading", name="Agents", exact=True)).to_be_visible(timeout=15_000)

    agent_name = f"个人助理 {assembled_world['suffix']}"
    card = page.locator("div.bg-card").filter(has_text=agent_name)
    expect(card).to_have_count(1)
    card.get_by_role("button", name="对话", exact=True).click()
    composer = page.get_by_placeholder("输入消息；Enter 发送，Shift+Enter 换行", exact=True)
    expect(composer).to_be_visible(timeout=15_000)

    composer.fill("请先确认统计口径：本轮应该采用哪个周期，然后再给出结论。")
    page.get_by_role("button", name=re.compile("发送$")).click()
    expect(page.get_by_text("需要补充信息", exact=True)).to_be_visible(timeout=15_000)
    session_id = page.url.rstrip("/").split("/")[-1]

    # A browser disconnect/reload reconstructs the pending action from durable state.
    page.reload(wait_until="domcontentloaded")
    answer = page.get_by_placeholder("输入回答；只会恢复提出该问题的 Work Item", exact=True)
    expect(answer).to_be_visible(timeout=15_000)
    clarification_action = api.wait_action(session_id, "clarification")
    answer.fill("采用 2026 年第二季度口径。")
    page.get_by_role("button", name="提交回答", exact=True).click()
    assert api.wait_run(clarification_action["run_id"])["status"] == "completed"
    messages = api.request(
        "GET", f"/api/v1/sessions/{session_id}/messages",
    ).json()["items"]
    clarification_message = next((
        item for item in reversed(messages)
        if item["sender_type"] == "agent"
        and item["run_id"] == clarification_action["run_id"]
        and item["content"].strip()
    ), None)
    assert clarification_message is not None
    excerpt = clarification_message["content"].strip().replace("`", "")[:40]
    expect(page.get_by_text(re.compile(re.escape(excerpt))).last).to_be_visible(
        timeout=int(api.run_timeout * 1000),
    )
    cursor = page.evaluate(
        "sessionId => localStorage.getItem(`myclaw:session-event-cursor:${sessionId}`)",
        session_id,
    )
    assert cursor and int(cursor) > 0

    composer.fill(
        "请把结论整理成可下载的《验收报告 2026.txt》，正文只写 artifact-e2e，"
        "完成文件创建和正式发布后再回复。"
    )
    page.get_by_role("button", name=re.compile("发送$")).click()
    expect(page.get_by_text("需要确认", exact=True)).to_be_visible(timeout=15_000)
    expect(page.get_by_text("write_file", exact=True)).to_be_visible()
    page.get_by_role("button", name=re.compile(r"批\s*准")).click()
    expect(page.get_by_text("验收报告 2026.txt", exact=True)).to_be_visible(timeout=25_000)
    expect(page.get_by_text("published", exact=True)).to_be_visible()

    complex_goal = (
        "这是复杂任务。已确认第二季度支付侧退款 100 笔、订单侧 97 笔；"
        "请基于现有事实分步骤完成退款分析并交付结论，不需要额外数据。"
    )
    composer.fill(complex_goal)
    page.get_by_role("button", name=re.compile("发送$")).click()
    expect(page.get_by_text("WorkingPlan", exact=True)).to_be_visible(timeout=15_000)
    expect(page.get_by_text("revision 1", exact=True)).to_be_visible()
    messages = api.request("GET", f"/api/v1/sessions/{session_id}/messages").json()["items"]
    complex_message = next(
        item for item in reversed(messages)
        if item["sender_type"] == "user" and item["content"] == complex_goal
    )
    plans = api.request(
        "GET", f"/api/v1/runs/{complex_message['run_id']}/working-plans",
    ).json()["items"]
    assert len(plans) == 1 and len(plans[0]["items"]) >= 2
    expect(page.get_by_text(plans[0]["items"][0]["text"], exact=True)).to_be_visible()
    assert not browser_errors
