import httpx


async def send_telegram(bot_token: str, chat_id: str, content: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": content, "parse_mode": "Markdown"}
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.post(url, json=payload)
        return resp.status_code == 200
