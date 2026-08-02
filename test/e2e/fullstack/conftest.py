from __future__ import annotations

import io
import os
import time
import uuid
import zipfile

import httpx
import pytest


class PhaseOneAPI:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=30, trust_env=False)
        self.token = ""
        self.user_id = ""
        self.run_timeout = 180 if os.getenv("MYCLAW_LLM_MODE", "mock") == "real" else 75

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def request(self, method: str, path: str, *, idem: bool = False, **kwargs):
        headers = {**self.auth, **kwargs.pop("headers", {})}
        if idem:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        response = self.client.request(method, path, headers=headers, **kwargs)
        if response.is_error:
            raise AssertionError(f"{method} {path} -> {response.status_code}: {response.text}")
        return response

    def login(self):
        deadline = time.monotonic() + 30
        while True:
            response = self.client.post("/api/v1/auth/login", json={
                "email": "admin@test.myclaw.local", "password": "testpassword123",
            })
            if response.status_code == 200:
                break
            if time.monotonic() >= deadline or response.status_code < 500:
                response.raise_for_status()
            time.sleep(0.5)
        self.token = response.json()["access_token"]
        self.user_id = response.json()["user"]["user_id"]
        return response.json()["user"]

    def wait_run(self, run_id: str, *, timeout: float | None = None) -> dict:
        timeout = self.run_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.request("GET", f"/api/v1/runs/{run_id}").json()
            if last["lifecycle"] in {"completed", "cancelled"}:
                return last
            time.sleep(0.25)
        raise AssertionError(f"Run {run_id} did not settle: {last}")

    def wait_action(self, session_id: str, action_type: str, *, timeout: float | None = None) -> dict:
        timeout = self.run_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            items = self.request(
                "GET", f"/api/v1/sessions/{session_id}/required-actions",
                params={"status": "pending", "type": action_type},
            ).json()["items"]
            if items:
                return items[0]
            time.sleep(0.2)
        raise AssertionError(f"No pending {action_type} for Session {session_id}")

    def wait_resource(self, path: str, predicate, *, timeout: float | None = None) -> dict:
        timeout = self.run_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.request("GET", path).json()
            if predicate(last):
                return last
            time.sleep(0.2)
        raise AssertionError(f"Resource did not reach expected state: {last}")


def _skill_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: Acceptance Analysis\ndescription: Analyze acceptance evidence\n---\n\nUse explicit evidence.",
        )
        archive.writestr("references/checklist.md", "Verify durable runtime and immutable versions.")
    return buffer.getvalue()


@pytest.fixture(scope="session")
def phase_api():
    api = PhaseOneAPI(os.getenv("MYCLAW_FULLSTACK_API_URL", "http://localhost:8001"))
    user = api.login()
    assert set(user["roles"]) >= {"user", "admin"}
    yield api
    api.client.close()


@pytest.fixture(scope="session")
def assembled_world(phase_api: PhaseOneAPI):
    api = phase_api
    suffix = uuid.uuid4().hex[:8]
    real_model = os.getenv("MYCLAW_LLM_MODE", "mock") == "real"
    if real_model:
        required = {
            "MYCLAW_REAL_LLM_API_KEY": os.getenv("MYCLAW_REAL_LLM_API_KEY"),
            "MYCLAW_REAL_LLM_ENDPOINT": os.getenv("MYCLAW_REAL_LLM_ENDPOINT"),
            "MYCLAW_REAL_LLM_MODEL": os.getenv("MYCLAW_REAL_LLM_MODEL"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Real-model acceptance requires: {', '.join(missing)}")
        model_key = f"real-{suffix}"
        model_name = required["MYCLAW_REAL_LLM_MODEL"]
        endpoint = required["MYCLAW_REAL_LLM_ENDPOINT"]
        credential_ref = "env://MYCLAW_REAL_LLM_API_KEY"
    else:
        model_key = f"mock-{suffix}"
        model_name = "mock-model"
        endpoint = "http://mock-llm:9090/v1"
        credential_ref = None
    model = api.request("POST", "/api/v1/models", idem=True, json={
        "key": model_key, "provider": "openai_compatible", "model_name": model_name,
        "endpoint": endpoint, "credential_ref": credential_ref,
        "default_parameters": {"temperature": 0},
        "capabilities": {"streaming": True, "tool_calling": True}, "enabled": True,
    }).json()
    tools = api.request("GET", "/api/v1/platform-tools").json()["items"]
    by_tool = {item["tool_key"]: item for item in tools}
    required_tools = {
        "read_file", "write_file", "execute", "invoke_agent",
        "request_clarification", "search_group_history", "publish_artifact",
    }
    assert required_tools <= by_tool.keys()
    execute = by_tool["execute"]
    if not execute["enabled"] or execute["confirmation_policy"] != "always":
        execute = api.request(
            "PATCH", f"/api/v1/platform-tools/{execute['id']}", idem=True,
            headers={"If-Match": f'W/"{execute["config_version"]}"'},
            json={"enabled": True, "confirmation_policy": "always"},
        ).json()
        by_tool["execute"] = execute
    policies = api.request("GET", "/api/v1/policy-definitions").json()["items"]
    by_policy = {item["policy_key"]: item["id"] for item in policies}
    sandbox = next(
        item for item in api.request("GET", "/api/v1/sandbox-profiles").json()["items"]
        if item["profile_key"] == "local"
    )
    skill = api.request(
        "POST", "/api/v1/skills", idem=True,
        data={"logical_name": f"acceptance.analysis.{suffix}"},
        files={"package": ("acceptance-skill.zip", _skill_zip(), "application/zip")},
    ).json()
    skill_version = skill["uploaded_version"]
    api.request(
        "POST", f"/api/v1/skills/{skill['id']}/versions/{skill_version['id']}/publish", idem=True,
    )
    api.request("POST", f"/api/v1/skills/{skill['id']}/sync", idem=True)

    common = {
        "description": "Phase-one acceptance Agent",
        "instructions": (
            "Use a durable Runtime Tool only when the current user or delegated task explicitly "
            "names that exact Tool. A Tool catalog, Skill, previous message, or general request to "
            "analyze or verify is never permission to call a Tool. Plan and Group definition or "
            "node tasks must return text directly unless the current task says 'You must call' "
            "followed by the exact Tool name. Never claim a Tool action without calling that Tool. "
            "Follow structured JSON response schemas exactly for Plan and Group generation. "
            "Return concise, evidence-based results and do not reveal credentials."
        ),
        "model_id": model["id"], "sandbox_profile_id": sandbox["id"],
        "context_policy_id": by_policy["context.standard"],
        "history_policy_id": by_policy["history.summary_recent"],
        "memory_write_policy_id": by_policy["memory_write.user_agent_facts"],
        "memory_recall_policy_id": by_policy["memory_recall.user_combined"],
        "group_enabled": True, "skill_ids": [skill["id"]],
        "tool_ids": [by_tool[key]["id"] for key in sorted(required_tools)],
    }

    def create_agent(name: str, bound: list[str]) -> dict:
        created = api.request("POST", "/api/v1/agents", json={
            "visibility": "enterprise",
            "draft": {**common, "name": f"{name} {suffix}", "bound_agent_ids": bound},
        }).json()
        published = api.request(
            "POST", f"/api/v1/agents/{created['agent']['id']}/publish", idem=True,
            json={"expected_draft_revision": created["draft"]["draft_revision"]},
        ).json()
        return {"id": created["agent"]["id"], "published": published["published_version"]}

    operations = create_agent("Operations", [])
    orders = create_agent("Orders", [])
    outsider = create_agent("Outside", [])
    finance = create_agent("Finance", [operations["id"], orders["id"], outsider["id"]])
    return {
        "suffix": suffix,
        "real_model": real_model,
        "model": model, "tools": by_tool, "skill": skill, "sandbox": sandbox,
        "policies": by_policy, "finance": finance, "operations": operations,
        "orders": orders, "outsider": outsider,
    }
