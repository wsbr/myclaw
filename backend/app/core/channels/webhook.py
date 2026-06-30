import httpx
import json


async def send_webhook(url: str, content: str, headers: dict | None = None, secret: str | None = None) -> bool:
    payload = {"content": content}
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    if secret:
        req_headers["X-Webhook-Secret"] = secret
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.post(url, json=payload, headers=req_headers)
        return resp.status_code < 300
