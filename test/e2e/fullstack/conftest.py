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
        for attempt in range(6):
            response = self.client.request(method, path, headers=headers, **kwargs)
            retryable_server_error = False
            if response.status_code >= 500:
                try:
                    retryable_server_error = bool(response.json()["error"]["retryable"])
                except (KeyError, TypeError, ValueError):
                    retryable_server_error = False
            if (
                response.status_code != 429
                and not retryable_server_error
            ) or attempt == 5:
                break
            retry_after = response.headers.get("Retry-After")
            if retry_after is None:
                try:
                    retry_after = response.json()["error"]["details"]["retry_after_seconds"]
                except (KeyError, TypeError, ValueError):
                    retry_after = 1
            time.sleep(max(0.25, min(float(retry_after), 5.0)))
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
            if last["status"] in {"completed", "cancelled", "failed"}:
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
            "---\nname: Refund Audit Evidence\ndescription: Build traceable ecommerce refund audit evidence\n---\n\n"
            "Use confirmed reporting periods, separate payment and order evidence, and disclose unresolved gaps.",
        )
        archive.writestr(
            "references/checklist.md",
            "Verify refund batch, payment status, duplicate-risk evidence, durable execution, and immutable versions.",
        )
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
    model = api.request("POST", "/api/v1/admin/models", idem=True, json={
        "key": model_key, "name": f"Acceptance {model_key}",
    }).json()
    model_version = api.request(
        "POST", f"/api/v1/admin/models/{model['id']}/versions", idem=True,
        headers={"If-Match": f'W/"{model["version"]}"'},
        json={
            "provider": "openai_compatible", "model_name": model_name,
            "endpoint": endpoint, "credential_ref": credential_ref,
            "default_parameters": {"temperature": 0, "max_tokens": 1024},
            "capabilities": {
                "streaming": True, "native_tool_calling": True,
                "parallel_tool_calling": False, "tool_result_continuation": True,
                "usage_in_stream": True, "stop_reason": True,
                "context_window": 16384, "structured_output": "none",
            },
        },
    ).json()
    conformance = api.request(
        "POST", f"/api/v1/admin/models/{model['id']}/versions/{model_version['id']}/conformance",
        idem=True,
    ).json()
    assert conformance["status"] == "passed", conformance
    model = api.request("GET", f"/api/v1/admin/models/{model['id']}").json()
    model = api.request(
        "POST", f"/api/v1/admin/models/{model['id']}/versions/{model_version['id']}/activate",
        idem=True, headers={"If-Match": f'W/"{model["version"]}"'},
    ).json()
    tools = api.request("GET", "/api/v1/platform-tools").json()["items"]
    by_tool = {item["tool_key"]: item for item in tools}
    required_tools = {
        "read_file", "write_file", "execute",
        "search_group_history", "publish_artifact",
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
        "model_id": model["id"], "sandbox_profile_id": sandbox["id"],
        "context_policy_id": by_policy["context.standard"],
        "history_policy_id": by_policy["history.summary_recent"],
        "memory_write_policy_id": by_policy["memory_write.user_agent_facts"],
        "memory_recall_policy_id": by_policy["memory_recall.user_combined"],
        "group_enabled": True,
    }

    def create_agent(
        name: str, description: str, instructions: str, bound: list[str], tool_keys: set[str],
        *, skill_ids: list[str] | None = None,
    ) -> dict:
        created = api.request("POST", "/api/v1/agents", json={
            "visibility": "enterprise",
            "draft": {
                **common,
                "name": f"{name} {suffix}",
                "description": description,
                "instructions": instructions,
                "bound_agent_ids": bound,
                "skill_ids": list(skill_ids or []),
                "tool_ids": [by_tool[key]["id"] for key in sorted(tool_keys)],
            },
        }).json()
        published = api.request(
            "POST", f"/api/v1/agents/{created['agent']['id']}/publish", idem=True,
            json={"expected_draft_revision": created["draft"]["draft_revision"]},
        ).json()
        return {"id": created["agent"]["id"], "published": published["published_version"]}

    refund_ops = create_agent(
        "退款运营专员",
        "核对退款批次、支付渠道状态、处理时效和业务口径。",
        "你负责电商退款运营核验。先确认统计周期和退款口径；缺少会改变结论的关键信息时向业务方提问。"
        "只依据已确认数据给出批次差异、时效风险和可追溯证据，不虚构查询或执行结果。",
        [],
        set(),
    )
    risk_control = create_agent(
        "订单风控复核员",
        "复核异常订单、重复退款、账号与设备关联风险。",
        "你负责退款相关订单风控复核。输出风险分层、异常原因和需要人工复核的证据，"
        "直接基于当前委派中已确认的信息工作；不得声称完成未真实执行的动作。",
        [],
        set(),
    )
    external_supplier = create_agent(
        "外部供应商对账员",
        "模拟未加入当前 Group 的外部协作对象，用于安全边界验收。",
        "你只处理供应商账单，不参与未明确加入的内部退款审计组。",
        [],
        set(),
    )
    product_manager = create_agent(
        "产品经理",
        "澄清产品目标、用户价值、范围边界和验收优先级。",
        "你负责产品需求复核，只依据当前委派内容指出范围、用户价值和验收标准缺口。",
        [],
        {"search_group_history"},
    )
    developer = create_agent(
        "开发工程师",
        "评估实现方案、技术边界、迁移风险和可恢复性。",
        "你负责开发方案复核，返回实现风险、迁移边界和可验证的技术建议。",
        [],
        {"write_file", "execute", "publish_artifact", "search_group_history"},
    )
    test_engineer = create_agent(
        "测试工程师",
        "设计验收、失败注入、并发与恢复测试。",
        "你负责测试方案复核，返回验收样本、失败路径和恢复检查建议。",
        [],
        {"read_file", "execute", "search_group_history"},
    )
    audit_lead = create_agent(
        "退款审计负责人",
        "统筹退款运营、订单风控与审计交付，生成可下载的审计证据。",
        "你是电商退款审计负责人。根据用户业务目标自主选择已装配能力：需要隔离验证脚本时真实执行；"
        "需要可下载交付物时先创建文件再发布；需要专业复核时委派给目录中合适的专员；"
        "在 Group 内只有任务明确要求复用历史结论时才检索组内历史，并且每个任务最多检索一次。"
        "不得用文字伪造工具、协作或文件发布结果。"
        "Plan 和 Group 提案必须严格满足平台提供的结构化契约。",
        [refund_ops["id"], risk_control["id"], external_supplier["id"]],
        required_tools,
        skill_ids=[skill["id"]],
    )
    return {
        "suffix": suffix,
        "real_model": real_model,
        "model": model, "tools": by_tool, "skill": skill, "sandbox": sandbox,
        "policies": by_policy,
        "audit_lead": audit_lead,
        "refund_ops": refund_ops,
        "risk_control": risk_control,
        "external_supplier": external_supplier,
        "product_manager": product_manager,
        "developer": developer,
        "test_engineer": test_engineer,
    }
