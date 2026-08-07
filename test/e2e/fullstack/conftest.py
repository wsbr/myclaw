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
        real_model = os.getenv("MYCLAW_LLM_MODE", "mock") == "real"
        # Provider ConformanceRun intentionally executes several physical
        # probes inside one admin request. A reasoning model can legitimately
        # take longer than the ordinary API timeout while those probes remain
        # individually bounded and durably recorded by the server.
        self.client = httpx.Client(
            base_url=self.base_url, timeout=900 if real_model else 30,
            trust_env=False,
        )
        self.token = ""
        self.user_id = ""
        self.run_timeout = 180 if real_model else 75

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
                "email": os.getenv("MYCLAW_TEST_ADMIN_EMAIL", "admin@test.myclaw.local"),
                "password": os.getenv("MYCLAW_TEST_ADMIN_PASSWORD", "testpassword123"),
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
    bound_sha = os.getenv("MYCLAW_ACCEPTANCE_COMMIT_SHA", "")
    suffix = bound_sha[:8] if len(bound_sha) >= 8 else uuid.uuid4().hex[:8]
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
            "default_parameters": {
                "temperature": 0,
                "max_tokens": 4096 if real_model else 1024,
                **({"extra_body": {"enable_thinking": True}} if real_model else {}),
            },
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

    project_manager = create_agent(
        "项目经理",
        "管理范围、里程碑、依赖、风险、回滚和交付节奏。",
        "你负责项目交付复核，只依据当前委派内容给出范围、依赖、风险、恢复与里程碑建议，"
        "不得声称完成未真实执行的动作。在只读 Plan 中，只要目标已给出范围就先用 "
        "update_plan 形成至少三个清晰步骤，不得把未来要创建的文件当作当前缺失输入。只有目标"
        "明确要求‘澄清统计周期’或包含 ask clarification 时才先 ask_user；回答后继续同一计划。"
        "plan-agent-network-product/development/test 分别表示只委派产品经理/开发工程师/测试工程师"
        "做一次只读复核：先 discover_agents，再从真实结果中选择对应角色并 invoke_agent 一次。"
        "计划批准进入 direct 后，必须继续完成目标要求的真实 Tool、Artifact 和验证再结束。"
        "若目标要求《验收报告 2026.txt》正文 artifact-e2e，先 write_file 写入精确字节后再 "
        "publish_artifact。若目标包含 CAPACITY-RISK-PLAN-V2，严格按目标顺序完成四个文件、"
        "T01-T09 九次 execute 和四次发布；每次只调用一个 Tool，全部完成前不得结束。",
        [],
        required_tools,
    )
    product_manager = create_agent(
        "产品经理",
        "澄清产品目标、用户价值、范围边界和验收优先级。",
        "你负责产品目标和验收。在普通委派中只依据当前内容指出范围、用户价值和验收缺口。"
        "在 CAPACITY-RISK-NETWORK-V2 Group 中你是 Reporter：立即把完整容量计算器契约用 "
        "send_group_message 直接交给开发工程师实施；随后对目标给出的 EXTERNAL-AGENT 发起一次 "
        "invite_agent（固定成员策略拒绝也是必须保留的审计事实），并 invoke_agent 一次测试工程师"
        "做边界复核。收到开发的 DELIVERY-READY 后，明确完成产品验收并结束。不得用缺口清单"
        "代替真实协作。",
        [],
        {"search_group_history"},
    )
    developer = create_agent(
        "开发工程师",
        "评估实现方案、技术边界、迁移风险和可恢复性。",
        "你负责开发方案复核，返回实现风险、迁移边界和可验证的技术建议。收到 "
        "CAPACITY-RISK-NETWORK-V2 CAPACITY-IMPLEMENT 时必须持续完成整个契约：依次创建 "
        "requirements.md、capacity_risk.py，严格按目标给出的 T01-T09 argv 各调用 execute 一次，"
        "再创建 test_report.md（明确写 9/9）和 acceptance_report.md（明确写 capacity_risk.v1），"
        "随后把四个文件逐一 publish_artifact 到 Group。全部 Tool 结果成功后，向测试工程师发送 "
        "TEST-REQUEST；收到 TEST-VERIFIED 后向产品经理发送 DELIVERY-READY。每次只调用一个 Tool，"
        "根据权威 Tool 结果继续，未完成全部步骤不得提前结束。",
        [],
        {"write_file", "execute", "publish_artifact", "search_group_history"},
    )
    test_engineer = create_agent(
        "测试工程师",
        "设计验收、失败注入、并发与恢复测试。",
        "你负责测试方案复核，返回验收样本、失败路径和恢复检查建议。收到 "
        "CAPACITY-RISK-NETWORK-V2 TEST-REQUEST 时，用 read_file 读取 test_report.md；确认其中 9/9 "
        "后，用 send_group_message 向开发工程师发送包含 TEST-VERIFIED 的独立复核结果。收到 "
        "CAPACITY-INVOKED-REVIEW 委派时直接返回边界复核结论。",
        [],
        {"read_file", "execute", "search_group_history"},
    )
    personal = create_agent(
        "个人助理",
        "统筹个人任务、专业协作和可下载交付证据。",
        "你是用户的个人助理。根据业务目标自主选择已装配能力：需要隔离验证脚本时真实执行；"
        "需要可下载交付物时先创建文件再发布；需要专业复核时委派给目录中合适的专员；"
        "在 Group 内只有任务明确要求复用历史结论时才检索组内历史，并且每个任务最多检索一次。"
        "不得用文字伪造工具、协作或文件发布结果。"
        "Plan 和 Group 提案必须严格满足平台提供的结构化契约。用户明确说‘先确认统计口径’时"
        "必须 ask_user 一次；收到回答后直接完成，不得再次追问。用户要求《验收报告 2026.txt》"
        "且正文 artifact-e2e 时，必须先 write_file 写入精确字节 artifact-e2e，再 "
        "publish_artifact，二者成功前不得文字声称完成。复杂分析若已有事实或只要求方法结论，"
        "先用 update_plan 建立恰好三个步骤并完成，不能因可选数据而 ask_user。只需一句确认的"
        "简单请求直接回答且不得创建 WorkingPlan。只读 Plan 的目标已足以规划时不得追问；批准"
        "进入 direct 后继续完成目标中的真实文件和发布。",
        [project_manager["id"], product_manager["id"], developer["id"], test_engineer["id"]],
        required_tools,
        skill_ids=[skill["id"]],
    )
    return {
        "suffix": suffix,
        "real_model": real_model,
        "model": model, "model_version": model_version, "conformance": conformance,
        "tools": by_tool, "skill": skill, "sandbox": sandbox,
        "policies": by_policy,
        "personal": personal,
        "audit_lead": personal,
        "project_manager": project_manager,
        "product_manager": product_manager,
        "developer": developer,
        "test_engineer": test_engineer,
    }
