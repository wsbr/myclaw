"""Chromium UAT for viewing, editing, previewing and versioning Skill Markdown."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import uuid

import pytest
from playwright.sync_api import Page, expect

from test_skill_runtime_real import skill_archive


pytestmark = pytest.mark.fullstack


def _shot(page: Page, root: Path, name: str) -> str:
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{name}.png"
    page.screenshot(path=target, full_page=True)
    return hashlib.sha256(target.read_bytes()).hexdigest()


@pytest.mark.timeout(600)
def test_skill_markdown_view_edit_preview_creates_immutable_version(
    page: Page, phase_api, record_testsuite_property,
):
    logical_name = f"ui-skill-{uuid.uuid4().hex[:10]}"
    created = phase_api.request(
        "POST", "/api/v1/skills", idem=True,
        data={"logical_name": logical_name},
        files={"package": ("ui-skill.zip", skill_archive("ui-v1"), "application/zip")},
    ).json()
    first = phase_api.request(
        "POST",
        f"/api/v1/skills/{created['id']}/versions/{created['uploaded_version']['id']}/publish",
        idem=True,
    ).json()
    phase_api.request("POST", f"/api/v1/skills/{created['id']}/sync", idem=True)

    ui_url = os.getenv("MYCLAW_FULLSTACK_UI_URL", "http://localhost:8081").rstrip("/")
    evidence_root = Path(os.getenv(
        "MYCLAW_BROWSER_EVIDENCE_DIR",
        Path(__file__).resolve().parents[2] / "test-results" / "minimal-browser",
    ))
    page.set_viewport_size({"width": 1536, "height": 960})
    page.goto(ui_url, wait_until="domcontentloaded")
    page.get_by_role("textbox", name="邮箱", exact=True).fill(
        os.getenv("MYCLAW_TEST_ADMIN_EMAIL", "admin@test.myclaw.local")
    )
    page.get_by_role("textbox", name="密码", exact=True).fill(
        os.getenv("MYCLAW_TEST_ADMIN_PASSWORD", "testpassword123")
    )
    page.get_by_role("button", name="登录", exact=True).click()
    expect(page.get_by_role("heading", name="Agents", exact=True)).to_be_visible(timeout=15_000)
    page.goto(f"{ui_url}/skills", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Skill 管理", exact=True)).to_be_visible()
    page.get_by_placeholder("搜索名称或描述").fill(logical_name)
    skill_button = page.locator("button").filter(has_text=logical_name).first
    expect(skill_button).to_be_visible(timeout=15_000)
    skill_button.click()
    expect(page.get_by_text("SKILL.md", exact=True).first).to_be_visible(timeout=15_000)
    expect(page.get_by_text("当前 v1", exact=True)).to_be_visible()

    content = (
        "---\nname: exact-evidence-writer\n"
        "description: UI immutable version acceptance\n"
        "allowed-tools:\n  - read_file\n---\n"
        "# UI Edited Marker\n\nSaved from the browser as a new immutable version."
    )
    expect(page.get_by_text("内容已与所选版本一致", exact=True)).to_be_visible(
        timeout=30_000,
    )
    editor = page.get_by_role("textbox", name="Markdown 编辑器", exact=True)
    expect(editor).to_be_visible(timeout=15_000)
    before_checksum = _shot(page, evidence_root, f"{logical_name}-before")
    editor.fill(content)
    expect(page.get_by_text("有尚未保存的修改", exact=True)).to_be_visible(timeout=15_000)
    page.get_by_text("预览", exact=True).click()
    expect(page.get_by_role("heading", name="UI Edited Marker", exact=True)).to_be_visible()
    preview_checksum = _shot(page, evidence_root, f"{logical_name}-preview")
    page.locator("button").filter(has_text="保存为新版本").click()
    expect(page.get_by_text("修改已保存为 v2，原版本保持不变", exact=True)).to_be_visible(
        timeout=30_000,
    )

    versions = phase_api.request(
        "GET", f"/api/v1/skills/{created['id']}/versions",
    ).json()["items"]
    by_version = {item["version_no"]: item for item in versions}
    assert by_version[1]["id"] == first["id"]
    assert by_version[1]["status"] == "published"
    assert by_version[2]["status"] == "uploaded"
    skill = next(
        item for item in phase_api.request("GET", "/api/v1/skills").json()["items"]
        if item["id"] == created["id"]
    )
    assert skill["current_published_version_id"] == first["id"]
    assert by_version[2]["checksum"] != first["checksum"]
    record_testsuite_property(
        "skill_ui_immutable_edit",
        "view,edit,preview,new-version,published-version-unchanged",
    )
    record_testsuite_property(
        "skill_ui_screenshot_sha256",
        f"{before_checksum},{preview_checksum}",
    )
