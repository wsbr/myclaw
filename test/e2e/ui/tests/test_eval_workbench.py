"""Browser contract tests. Run against a local Vite server with MYCLAW_EVAL_UI_URL.

Only HTTP fixtures are used: no provider requests or production writes.
"""
import json
import os
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(not os.getenv("MYCLAW_EVAL_UI_URL"), reason="Select a local frontend for evaluation UI tests")


@pytest.fixture
def eval_ui(page):
    user = {"id": "owner", "tenant_id": "tenant", "user_id": "user", "username": "Builder", "role": "builder", "roles": ["builder"], "status": "active", "version": 1}
    agent = {"id": "agent", "owner_account_id": "owner", "name": "搜索助手", "status": "published", "visibility": "private"}
    source = {"case_key": "run-source", "name": "搜索并保留来源", "input_text": "查找新闻并附上来源", "assertions": {"min_chars": 1}, "weight": "1", "tags": ["搜索"], "source_run_id": "source", "source_snapshot": {"response_text": "过去的回答", "tools": [{"key": "web_search", "status": "failed"}]}}
    suite = {"id": "suite", "agent_id": "agent", "name": "搜索回归", "status": "active", "version": 2, "current_version_id": "sv1", "current_version_no": 1, "description": "回归验证", "gate_policy": {"min_pass_rate": "1", "required_for_publish": False}, "cases": [{**source, "id": "case", "sort_order": 0}], "content_hash": "a" * 64}
    cases = [{"id": "cr", "eval_case_id": "case", "case_key": "run-source", "name": "搜索并保留来源", "session_id": "session", "run_id": "run", "status": "passed", "score": "1", "response_text": "含来源的完整回答", "input_text": "查找新闻并附上来源", "runtime_status": "completed", "assertion_results": [{"assertion": "require_successful_tools", "expected": "web_search", "passed": True, "actual": ["web_search"]}], "tags": ["搜索"]}]
    runs = [{"id": "baseline", "agent_id": "agent", "suite_version_id": "sv1", "suite_name": "搜索回归", "suite_version_no": 1, "candidate_agent_version_id": "av1", "candidate_content_hash": "a" * 64, "status": "passed", "case_count": 1, "passed_count": 1, "failed_count": 0, "error_count": 0, "score": "1", "case_runs": cases, "created_at": "2026-10-09T01:00:00Z"},
            {"id": "candidate", "agent_id": "agent", "suite_version_id": "sv1", "suite_name": "搜索回归", "suite_version_no": 1, "candidate_agent_version_id": "av2", "candidate_content_hash": "b" * 64, "status": "failed", "case_count": 1, "passed_count": 0, "failed_count": 1, "error_count": 0, "score": "0", "case_runs": cases, "created_at": "2026-10-09T02:00:00Z"}]
    writes = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route(request):
        path = request.request.url.split("/api/v1", 1)[1].split("?", 1)[0]
        method = request.request.method
        if method == "POST":
            writes.append((path, request.request.post_data_json))
            if path.endswith("/runs"):
                result = runs[0]
            else:
                body = request.request.post_data_json
                suite.update(name=body.get("name", suite["name"]), cases=[{**c, "id": f"case-{i}", "sort_order": i} for i, c in enumerate(body.get("cases", suite["cases"]))])
                result = suite
        elif path == "/auth/me": result = user
        elif path == "/agents": result = {"items": [agent], "next_cursor": None}
        elif path == "/agents/agent": result = {"agent": agent, "draft": {"draft_revision": 7}}
        elif path.endswith("/eval-suites"): result = {"items": [suite]}
        elif path.endswith("/eval-runs"): result = {"items": runs}
        elif path.endswith("/eval-sources"): result = {"items": [{"run_id": "source", "session_id": "session", "status": "failed", "input_preview": "查找新闻并附上来源", "created_at": "2026-10-09T00:00:00Z"}]}
        elif path.endswith("/eval-sources/source"): result = source
        elif path.endswith("/eval-comparison"): result = {"baseline_id": "baseline", "candidate_id": "candidate", "same_candidate": False, "score_delta": "-1", "regressed_count": 1, "improved_count": 0, "cases": [{"case_key": "case", "name": "搜索用例", "change": "regressed", "baseline_status": "passed", "candidate_status": "failed", "baseline_response": "过去回答", "candidate_response": "新回答", "candidate_assertions": []}], "tags": [{"tag": "搜索", "count": 1, "baseline_passed": 1, "candidate_passed": 0}]}
        else: result = {"items": [], "unread_count": 0}
        request.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    page.add_init_script("localStorage.setItem('access_token', 'test-token')")
    page.route("**/api/v1/**", route)
    page.goto(os.environ["MYCLAW_EVAL_UI_URL"] + "/evals?agent=agent")
    expect(page.get_by_role("heading", name="评测", exact=True)).to_be_visible()
    yield page, writes
    assert not errors


def test_import_edit_save_and_start_uses_fixed_draft(eval_ui):
    page, writes = eval_ui
    page.get_by_role("button", name="新建评测集", exact=True).click()
    dialog = page.get_by_role("dialog").filter(has=page.get_by_text("评测集名称", exact=True))
    dialog.get_by_label("评测集名称").fill("真实问题回归")
    dialog.get_by_role("button", name="从运行导入", exact=True).click()
    importer = page.get_by_role("dialog").filter(has=page.get_by_text("从真实运行导入用例", exact=True))
    importer.get_by_role("button", name=re.compile(r"预\s*览")).last.click()
    expect(importer.get_by_text("过去的回答", exact=True)).to_be_visible()
    importer.get_by_role("button", name="加入用例", exact=True).click()
    dialog.get_by_text("1. 搜索并保留来源", exact=True).click()
    required = dialog.get_by_label("必须执行成功的工具")
    required.fill("web_search")
    required.press("Enter")
    dialog.get_by_role("button", name="保存版本", exact=True).click()
    expect(dialog).not_to_be_visible()
    body = next(body for path, body in writes if path.endswith("/eval-suites"))
    assert body["cases"][0]["source_run_id"] == "source"
    assert body["cases"][0]["assertions"]["require_successful_tools"] == ["web_search"]
    assert body["gate_policy"]["required_for_publish"] is False
    assert "max_cost" not in body["cases"][0]["assertions"]
    page.get_by_role("button", name=re.compile(r"运\s*行")).click()
    expect(page.get_by_text("最近 50 次评测", exact=True)).to_be_visible()
    assert writes[-1][1] == {"expected_draft_revision": 7}


def test_edit_retains_source_and_compare_shows_regression(eval_ui):
    page, writes = eval_ui
    page.get_by_role("button", name=re.compile(r"编\s*辑")).click()
    page.get_by_role("button", name="保存版本", exact=True).click()
    expect(page.get_by_role("dialog")).not_to_be_visible()
    assert writes[0][1]["expected_version"] == 2
    assert writes[0][1]["cases"][0]["source_run_id"] == "source"
    page.get_by_role("tab", name="版本对比").click()
    page.get_by_role("combobox", name="基线运行").click()
    page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content').filter(has_text='aaaaaaaa').click()
    page.get_by_role("combobox", name="候选运行").click()
    page.get_by_role('combobox', name='候选运行').press('Enter')
    expect(page.get_by_text("退步用例", exact=True)).to_be_visible()
    expect(page.get_by_text("退步", exact=True)).to_be_visible()
    page.screenshot(path="/tmp/myclaw-eval-workbench.png", full_page=True)
