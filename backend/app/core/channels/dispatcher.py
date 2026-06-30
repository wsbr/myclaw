import json
from app.core.tool_registry.base import RuntimeContext


class ChannelDispatcher:

    @staticmethod
    async def send(
        channel: str,
        content: str,
        title: str | None = None,
        context: RuntimeContext | None = None,
        agent_id: str | None = None,
        db=None,
    ) -> bool:
        try:
            if channel == "platform_ui":
                return await ChannelDispatcher._send_platform_ui(content, context)

            cfg = {}
            if db and agent_id:
                cfg = await ChannelDispatcher._load_config(db, agent_id, channel)

            if channel == "feishu":
                from app.core.channels.feishu import send_feishu
                webhook_url = cfg.get("webhook_url")
                return await send_feishu(webhook_url, content) if webhook_url else False

            if channel == "wechat":
                import httpx
                webhook_url = cfg.get("webhook_url")
                if not webhook_url:
                    return False
                payload = {"msgtype": "text", "text": {"content": content}}
                async with httpx.AsyncClient(timeout=10) as http:
                    resp = await http.post(webhook_url, json=payload)
                    return resp.status_code == 200

            if channel == "telegram":
                from app.core.channels.telegram import send_telegram
                return await send_telegram(cfg.get("bot_token", ""), cfg.get("chat_id", ""), content)

            if channel == "email":
                from app.core.channels.email import send_email
                send_email(
                    smtp_host=cfg.get("smtp_host", ""),
                    smtp_port=int(cfg.get("smtp_port", 465)),
                    username=cfg.get("username", ""),
                    password=cfg.get("password", ""),
                    to_addr=cfg.get("to", ""),
                    subject=cfg.get("subject", title or "MyClaw 通知"),
                    content=content,
                    use_tls=cfg.get("use_tls", True),
                )
                return True

            if channel == "webhook":
                from app.core.channels.webhook import send_webhook
                return await send_webhook(
                    cfg.get("url", ""),
                    content,
                    headers=cfg.get("headers"),
                    secret=cfg.get("secret"),
                )

            return False
        except Exception:
            return False

    @staticmethod
    async def _send_platform_ui(content: str, context: RuntimeContext | None) -> bool:
        import redis.asyncio as aioredis
        from app.config import settings
        session_key = f"myclaw:session:{context.session_id}" if context else "myclaw:stream"
        async with aioredis.from_url(settings.redis_url, decode_responses=True) as r:
            await r.publish(session_key, json.dumps({"type": "task_output", "content": content}))
        return True

    @staticmethod
    async def _load_config(db, agent_id: str, channel: str) -> dict:
        from sqlalchemy import select
        from app.models.stats import ChannelConfig
        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel == channel,
                ChannelConfig.is_active == 1,
            )
        )
        row = result.scalar_one_or_none()
        return json.loads(row.config_json) if row else {}
