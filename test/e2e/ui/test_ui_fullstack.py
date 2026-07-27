"""Playwright UI E2E tests against deployed frontend."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

FULLSTACK_API_URL = os.environ.get("MYCLAW_FULLSTACK_API_URL", "http://localhost:8001")
FULLSTACK_UI_URL = os.environ.get("MYCLAW_FULLSTACK_UI_URL", "http://localhost:8081")
MOCK_LLM_INTERNAL_URL = os.environ.get("MYCLAW_MOCK_LLM_INTERNAL_URL", "http://mock-llm:9090")
ADMIN_EMAIL = os.environ.get("INIT_ADMIN_EMAIL", "admin@test.myclaw.local")
ADMIN_PASSWORD = os.environ.get("INIT_ADMIN_PASSWORD", "testpassword123")

SEED_FILE = Path(__file__).parent / ".seed-data.json"


def _stack_reachable() -> bool:
    try:
        with httpx.Client(timeout=3.0) as c:
            return (
                c.get(f"{FULLSTACK_API_URL}/health").status_code == 200
                and c.get(FULLSTACK_UI_URL).status_code == 200
            )
    except Exception:
        return False


requires_fullstack = pytest.mark.skipif(
    not _stack_reachable(),
    reason="Full stack not reachable — run: ./scripts/run_fullstack.sh",
)

pytestmark = [pytest.mark.fullstack, requires_fullstack]


@pytest.fixture(scope="session", autouse=True)
def seed_data():
    """Create model provider + agent via API before UI tests."""
    with httpx.Client(base_url=FULLSTACK_API_URL, timeout=30.0) as c:
        login = c.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        login.raise_for_status()
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        provider = c.post("/api/v1/model-providers", headers=headers, json={
            "name": "ui-py-test-provider",
            "provider": "openai_compatible",
            "base_url": f"{MOCK_LLM_INTERNAL_URL}/v1",
            "api_key": "sk-ui-py",
        }).json()
        c.patch(f"/api/v1/model-providers/{provider['id']}/activate", headers=headers)

        agent = c.post("/api/v1/agents", headers=headers, json={
            "name": f"UI PyTest Agent {int(time.time())}",
            "description": "Agent for Playwright UI tests",
            "modes": ["chat", "plan"],
            "model_config_": {"provider_id": provider["id"], "model_id": "mock-model"},
            "system_prompt_blocks": {"blocks": [{"type": "text", "content": "You are helpful."}]},
        }).json()
        c.post(f"/api/v1/agents/{agent['id']}/activate", headers=headers)

        data = {"agentId": agent["id"], "agentName": agent["name"]}
        SEED_FILE.write_text(json.dumps(data))
        yield data
        SEED_FILE.unlink(missing_ok=True)


def login(page: Page):
    page.goto(f"{FULLSTACK_UI_URL}/login")
    page.get_by_placeholder("admin@myclaw.local").fill(ADMIN_EMAIL)
    page.locator("input[type='password']").fill(ADMIN_PASSWORD)
    page.get_by_role("button", name="登录").click()
    page.wait_for_url(re.compile(r"http://localhost:8081/(?!login)"), timeout=15_000)
    token = page.evaluate("() => localStorage.getItem('access_token')")
    assert token, "Login failed: no access_token in localStorage"


def goto_sidebar(page: Page, label: str):
    page.get_by_role("button", name=label, exact=True).click()


class TestLogin:
    def test_login_success(self, page: Page):
        login(page)
        expect(page.get_by_role("heading", name="Agents")).to_be_visible()
        expect(page.get_by_role("button", name="新建 Agent")).to_be_visible()

    def test_login_rejects_bad_credentials(self, page: Page):
        page.goto(f"{FULLSTACK_UI_URL}/login")
        page.get_by_placeholder("admin@myclaw.local").fill("wrong@test.myclaw.local")
        page.locator("input[type='password']").fill("wrong")
        page.get_by_role("button", name="登录").click()
        # Should remain on login page
        expect(page).to_have_url(re.compile(r".*/login"))


class TestNavigation:
    @pytest.mark.parametrize("label,heading", [
        ("Agents", "Agents"),
        ("Tasks", "Tasks"),
        ("用户管理", "用户管理"),
        ("Tool 管理", "Tool 管理"),
        ("Skill 管理", "Skill 管理"),
        ("MCP 管理", "MCP Server"),
        ("模型管理", "模型 Provider"),
    ])
    def test_sidebar_pages(self, page: Page, label: str, heading: str):
        login(page)
        goto_sidebar(page, label)
        expect(page.get_by_role("heading", name=heading)).to_be_visible()


class TestAgents:
    def test_open_agent_config(self, page: Page, seed_data):
        login(page)
        name = seed_data["agentName"]
        card = page.locator(".group").filter(has_text=name)
        expect(card).to_be_visible()
        card.get_by_title("配置").click()
        expect(page).to_have_url(re.compile(rf".*/agents/{seed_data['agentId']}/config"))

    def test_new_agent_page(self, page: Page):
        login(page)
        page.get_by_role("button", name="新建 Agent").click()
        expect(page).to_have_url(f"{FULLSTACK_UI_URL}/agents/new/config")


class TestAgentAssembly:
    def test_config_builtin_tool_then_chat_creates_scheduled_task(self, page: Page):
        login(page)

        with httpx.Client(base_url=FULLSTACK_API_URL, timeout=30.0) as c:
            login_resp = c.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
            token = login_resp.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            provider = c.post("/api/v1/model-providers", headers=headers, json={
                "name": f"ui-asm-provider-{int(time.time())}",
                "provider": "openai_compatible",
                "base_url": f"{MOCK_LLM_INTERNAL_URL}/v1",
                "api_key": "sk-ui-asm",
            }).json()
            c.patch(f"/api/v1/model-providers/{provider['id']}/activate", headers=headers)

            agent = c.post("/api/v1/agents", headers=headers, json={
                "name": f"UI Assembly Agent {int(time.time())}",
                "description": "Draft for UI assembly test",
                "modes": ["chat"],
                "model_config_": {"provider_id": provider["id"], "model_id": "mock-model"},
                "system_prompt_blocks": {"identity": "Study assistant"},
            }).json()
            agent_id = agent["id"]

        page.goto(f"{FULLSTACK_UI_URL}/agents/{agent_id}/config")
        expect(page.get_by_text("配置:")).to_be_visible(timeout=15_000)

        page.get_by_role("tab", name="工具与能力").click()
        tools_panel = page.locator(".ant-tabs-tabpane-active")
        tools_panel.locator(".ant-select-selector").first.click()
        page.locator(".ant-select-item-option-content").filter(
            has_text="create_scheduled_task"
        ).last.click(force=True)
        page.keyboard.press("Escape")

        page.get_by_role("button", name="保存").click()
        expect(page.get_by_text("保存成功")).to_be_visible(timeout=15_000)

        with httpx.Client(base_url=FULLSTACK_API_URL, timeout=30.0) as c:
            login_resp = c.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
            headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}
            c.post(f"/api/v1/agents/{agent_id}/activate", headers=headers).raise_for_status()

        page.reload()

        page.get_by_role("button", name="对话").click()
        expect(page).to_have_url(re.compile(rf"/agents/{agent_id}/chat"))

        textbox = page.get_by_placeholder(re.compile(r"输入"))
        textbox.fill("5分钟后提醒我复习BFS")
        page.get_by_role("button", name="发送").click()
        expect(page.get_by_text(re.compile(r"定时提醒|复习"))).to_be_visible(timeout=30_000)

        goto_sidebar(page, "Tasks")
        expect(page.get_by_text("复习提醒").first).to_be_visible(timeout=15_000)


class TestTasks:
    def test_task_board_renders(self, page: Page):
        login(page)
        goto_sidebar(page, "Tasks")
        expect(page.get_by_role("heading", name="Tasks")).to_be_visible()
        expect(page.get_by_text("异步任务看板")).to_be_visible()


class TestAdmin:
    def test_users_page(self, page: Page):
        login(page)
        goto_sidebar(page, "用户管理")
        expect(page.get_by_role("cell", name=ADMIN_EMAIL)).to_be_visible()

    def test_tools_page_loads_table(self, page: Page):
        login(page)
        goto_sidebar(page, "Tool 管理")
        page.wait_for_selector(".ant-table", timeout=15_000)

    def test_models_page(self, page: Page):
        login(page)
        goto_sidebar(page, "模型管理")
        expect(page.get_by_role("heading", name="模型 Provider")).to_be_visible()


def _open_chat_page(page: Page, agent_id: str, agent_name: str):
    page.goto(f"{FULLSTACK_UI_URL}/agents/{agent_id}/chat")
    expect(page).to_have_url(re.compile(rf"/agents/{agent_id}/chat"))
    textbox = page.get_by_placeholder(re.compile(r"输入"))
    expect(textbox).to_be_visible(timeout=30_000)
    return textbox


class TestSessionChat:
    def test_chat_reply(self, page: Page, seed_data):
        login(page)
        textbox = _open_chat_page(page, seed_data["agentId"], seed_data["agentName"])
        textbox.fill("Hello from UI test")
        page.get_by_role("button", name="发送").click()
        expect(page.get_by_text(re.compile(r"Mock assistant reply"))).to_be_visible(timeout=30_000)


class TestSessionPlan:
    def test_plan_generate_and_execute(self, page: Page, seed_data):
        login(page)
        textbox = _open_chat_page(page, seed_data["agentId"], seed_data["agentName"])

        textbox.fill("Analyze Q3 sales performance")
        page.get_by_role("button", name="生成计划").click()
        expect(page.get_by_text("执行计划：")).to_be_visible(timeout=30_000)
        expect(page.get_by_text("Collect sales data from Q3")).to_be_visible()

        page.get_by_role("button", name="确认执行").click()
        expect(page.get_by_text("计划已开始执行")).to_be_visible()
        expect(page.get_by_text("计划执行完成")).to_be_visible(timeout=120_000)


class TestTrace:
    def test_trace_page_after_chat(self, page: Page, seed_data):
        login(page)
        textbox = _open_chat_page(page, seed_data["agentId"], seed_data["agentName"])
        textbox.fill("Generate trace data")
        page.get_by_role("button", name="发送").click()
        expect(page.get_by_text(re.compile(r"Mock assistant reply"))).to_be_visible(timeout=30_000)

        page.get_by_role("button", name="查看 Trace").click()
        expect(page).to_have_url(re.compile(r".*/sessions/.+/trace"))
        expect(page.get_by_text("Trace 查看器")).to_be_visible(timeout=15_000)
