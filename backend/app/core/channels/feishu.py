import httpx
import json


async def send_feishu(webhook_url: str, content: str) -> bool:
    payload = {
        "msg_type": "text",
        "content": {"text": content},
    }
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.post(webhook_url, json=payload)
        return resp.status_code == 200
