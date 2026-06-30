from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext


class SendToChannelTool(BaseTool):
    name = "send_to_channel"
    description = "主动将内容推送到指定通知渠道（微信、飞书、Telegram 等）"
    input_schema = {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "description": "渠道名称: platform_ui | wechat | feishu | telegram | webhook"},
            "content": {"type": "string", "description": "要发送的内容"},
            "title": {"type": "string", "description": "消息标题（可选）"},
        },
        "required": ["channel", "content"],
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        from app.core.channels.dispatcher import ChannelDispatcher
        ok = await ChannelDispatcher.send(
            channel=args["channel"],
            content=args["content"],
            title=args.get("title"),
            context=context,
            db=db,
        )
        return ToolResult(success=ok, error=None if ok else "Channel send failed")
