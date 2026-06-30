import httpx
import json
from typing import Any


class McpClient:
    """MCP protocol client (HTTP/SSE transport)."""

    def __init__(self, endpoint: str, auth_type: str = "none", auth_config: dict | None = None):
        self.endpoint = endpoint.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if auth_type == "bearer" and auth_config:
            self.headers["Authorization"] = f"Bearer {auth_config.get('token', '')}"
        elif auth_type == "api_key" and auth_config:
            header_name = auth_config.get("header", "X-API-Key")
            self.headers[header_name] = auth_config.get("key", "")

    async def list_tools(self) -> list[dict]:
        async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as client:
            resp = await client.post(f"{self.endpoint}/tools/list", json={})
            resp.raise_for_status()
            return resp.json().get("tools", [])

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        async with httpx.AsyncClient(headers=self.headers, timeout=60.0) as client:
            resp = await client.post(f"{self.endpoint}/tools/call", json={
                "name": tool_name,
                "arguments": arguments,
            })
            resp.raise_for_status()
            data = resp.json()
            # MCP returns content array
            content = data.get("content", [])
            if content and content[0].get("type") == "text":
                try:
                    return json.loads(content[0]["text"])
                except Exception:
                    return content[0]["text"]
            return data

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.endpoint}/health")
                return resp.status_code < 400
        except Exception:
            return False
