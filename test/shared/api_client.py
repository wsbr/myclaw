"""HTTP client wrapper for MyClaw API tests."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx


class MyClawClient:
    def __init__(self, base_url: str, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._client: httpx.AsyncClient | None = None

    @property
    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def __aenter__(self) -> "MyClawClient":
        self._client = httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=60.0)
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=60.0)
        return self._client

    async def login(self, email: str, password: str) -> dict:
        client = self._ensure_client()
        resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        client.headers["Authorization"] = f"Bearer {self._token}"
        return data

    async def create_model_provider(self, name: str, provider: str, base_url: str, api_key: str = "sk-test") -> dict:
        resp = await self._ensure_client().post(
            "/api/v1/model-providers",
            json={"name": name, "provider": provider, "base_url": base_url, "api_key": api_key},
        )
        resp.raise_for_status()
        return resp.json()

    async def activate_model_provider(self, provider_id: str) -> dict:
        resp = await self._ensure_client().patch(f"/api/v1/model-providers/{provider_id}/activate")
        resp.raise_for_status()
        return resp.json()

    async def create_agent(
        self,
        name: str,
        provider_id: str,
        modes: list[str] | None = None,
        model_id: str = "mock-model",
    ) -> dict:
        resp = await self._ensure_client().post(
            "/api/v1/agents",
            json={
                "name": name,
                "description": "Test agent for plan mode",
                "modes": modes or ["chat", "plan"],
                "model_config_": {"provider_id": provider_id, "model_id": model_id},
                "system_prompt_blocks": {"blocks": [{"type": "text", "content": "You are a helpful assistant."}]},
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def activate_agent(self, agent_id: str) -> dict:
        resp = await self._ensure_client().post(f"/api/v1/agents/{agent_id}/activate")
        resp.raise_for_status()
        return resp.json()

    async def update_agent(self, agent_id: str, payload: dict) -> dict:
        resp = await self._ensure_client().put(f"/api/v1/agents/{agent_id}", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def update_agent_tools(self, agent_id: str, tool_ids: list[str], builtin_tools: dict[str, bool]) -> dict:
        resp = await self._ensure_client().put(
            f"/api/v1/agents/{agent_id}/tools",
            json={"tool_ids": tool_ids, "builtin_tools": builtin_tools},
        )
        resp.raise_for_status()
        return resp.json()

    async def update_agent_skills(self, agent_id: str, skill_ids: list[str]) -> dict:
        resp = await self._ensure_client().put(
            f"/api/v1/agents/{agent_id}/skills",
            json={"skill_ids": skill_ids},
        )
        resp.raise_for_status()
        return resp.json()

    async def update_agent_mcp_servers(self, agent_id: str, mcp_server_ids: list[str]) -> dict:
        resp = await self._ensure_client().put(
            f"/api/v1/agents/{agent_id}/mcp-servers",
            json={"mcp_server_ids": mcp_server_ids},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_agent_versions(self, agent_id: str) -> list[dict]:
        resp = await self._ensure_client().get(f"/api/v1/agents/{agent_id}/versions")
        resp.raise_for_status()
        return resp.json()

    async def create_tool(self, name: str, display_name: str, description: str, input_schema: dict) -> dict:
        resp = await self._ensure_client().post(
            "/api/v1/tools",
            json={
                "name": name,
                "display_name": display_name,
                "description": description,
                "input_schema": input_schema,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def create_skill(self, name: str, display_name: str, description: str, content: str) -> dict:
        resp = await self._ensure_client().post(
            "/api/v1/skills",
            json={
                "name": name,
                "display_name": display_name,
                "description": description,
                "content": content,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def create_mcp_server(self, name: str, display_name: str, endpoint: str) -> dict:
        resp = await self._ensure_client().post(
            "/api/v1/mcp-servers",
            json={
                "name": name,
                "display_name": display_name,
                "description": "Test MCP server",
                "endpoint": endpoint,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def create_session(self, agent_id: str, mode: str = "plan") -> dict:
        resp = await self._ensure_client().post(
            "/api/v1/sessions",
            json={"agent_id": agent_id, "mode": mode},
        )
        resp.raise_for_status()
        return resp.json()

    async def generate_plan(self, session_id: str, goal: str) -> dict:
        resp = await self._ensure_client().post(
            f"/api/v1/sessions/{session_id}/plan/generate",
            json={"goal": goal},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_plan(self, session_id: str, plan_id: str) -> dict:
        resp = await self._ensure_client().get(f"/api/v1/sessions/{session_id}/plan/{plan_id}")
        resp.raise_for_status()
        return resp.json()

    async def execute_plan(self, session_id: str, plan_id: str) -> dict:
        resp = await self._ensure_client().post(
            f"/api/v1/sessions/{session_id}/plan/{plan_id}/execute",
        )
        resp.raise_for_status()
        return resp.json()

    async def get_task(self, task_id: str) -> dict:
        resp = await self._ensure_client().get(f"/api/v1/tasks/{task_id}")
        resp.raise_for_status()
        return resp.json()

    async def wait_for_task(
        self,
        task_id: str,
        *,
        timeout: float = 60.0,
        interval: float = 0.5,
    ) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = await self.get_task(task_id)
            if task.get("status") in ("completed", "failed", "cancelled"):
                return task
            await asyncio.sleep(interval)
        raise TimeoutError(f"Task {task_id} did not finish within {timeout}s")

    async def list_runners(self, session_id: str) -> list[dict]:
        resp = await self._ensure_client().get(f"/api/v1/sessions/{session_id}/runners")
        resp.raise_for_status()
        return resp.json()

    async def list_spans(self, session_id: str, runner_id: str) -> list[dict]:
        resp = await self._ensure_client().get(
            f"/api/v1/sessions/{session_id}/runners/{runner_id}/spans"
        )
        resp.raise_for_status()
        return resp.json()

    async def fetch_full_trace(self, session_id: str) -> dict[str, Any]:
        runners = await self.list_runners(session_id)
        spans_by_runner = {}
        for runner in runners:
            spans_by_runner[runner["id"]] = await self.list_spans(session_id, runner["id"])
        return {"runners": runners, "spans_by_runner": spans_by_runner}

    async def health(self) -> dict:
        resp = await self._ensure_client().get("/health")
        resp.raise_for_status()
        return resp.json()

    # ── Agents ──────────────────────────────────────────────────────────────

    async def list_agents(self) -> list[dict]:
        resp = await self._ensure_client().get("/api/v1/agents")
        resp.raise_for_status()
        return resp.json()

    async def get_agent(self, agent_id: str) -> dict:
        resp = await self._ensure_client().get(f"/api/v1/agents/{agent_id}")
        resp.raise_for_status()
        return resp.json()

    async def delete_agent(self, agent_id: str) -> None:
        resp = await self._ensure_client().delete(f"/api/v1/agents/{agent_id}")
        resp.raise_for_status()

    # ── Tasks ───────────────────────────────────────────────────────────────

    async def list_tasks(self, status: str | None = None) -> list[dict]:
        params = {"status": status} if status else {}
        resp = await self._ensure_client().get("/api/v1/tasks", params=params)
        resp.raise_for_status()
        return resp.json()

    async def create_task(self, agent_id: str, input_data: dict, title: str = "Test task") -> dict:
        resp = await self._ensure_client().post(
            "/api/v1/tasks",
            json={
                "agent_id": agent_id,
                "title": title,
                "trigger_type": "immediate",
                "input": input_data,
            },
        )
        resp.raise_for_status()
        return resp.json()

    # ── Chat (SSE) ──────────────────────────────────────────────────────────

    async def chat(self, session_id: str, message: str) -> str:
        """Send chat message and collect full streamed response."""
        resp = await self._ensure_client().post(
            f"/api/v1/sessions/{session_id}/chat",
            json={"message": message},
            timeout=60.0,
        )
        resp.raise_for_status()
        full_text = ""
        for line in resp.text.split("\n"):
            if line.startswith("data: ") and line != "data: [DONE]":
                import json
                try:
                    event = json.loads(line[6:])
                    if event.get("type") == "stream_chunk":
                        full_text += event.get("content", "")
                    elif event.get("type") == "done":
                        full_text = event.get("content", full_text)
                except json.JSONDecodeError:
                    pass
        return full_text

    # ── Admin resources ─────────────────────────────────────────────────────

    async def list_tools(self) -> list[dict]:
        resp = await self._ensure_client().get("/api/v1/tools")
        resp.raise_for_status()
        return resp.json()

    async def list_skills(self) -> list[dict]:
        resp = await self._ensure_client().get("/api/v1/skills")
        resp.raise_for_status()
        return resp.json()

    async def list_mcp_servers(self) -> list[dict]:
        resp = await self._ensure_client().get("/api/v1/mcp-servers")
        resp.raise_for_status()
        return resp.json()

    async def list_model_providers(self) -> list[dict]:
        resp = await self._ensure_client().get("/api/v1/model-providers")
        resp.raise_for_status()
        return resp.json()

    async def list_admin_users(self) -> list[dict]:
        resp = await self._ensure_client().get("/api/v1/admin/users")
        resp.raise_for_status()
        return resp.json()

    async def create_admin_user(self, email: str, username: str, password: str, role: str = "user") -> dict:
        resp = await self._ensure_client().post(
            "/api/v1/admin/users",
            json={"email": email, "username": username, "password": password, "role": role},
        )
        resp.raise_for_status()
        return resp.json()

    async def me(self) -> dict:
        resp = await self._ensure_client().get("/api/v1/auth/me")
        resp.raise_for_status()
        return resp.json()
