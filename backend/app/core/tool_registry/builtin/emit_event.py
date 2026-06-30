from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext


class EmitEventTool(BaseTool):
    name = "emit_event"
    description = "向 Agent 工作网广播事件，订阅该事件类型的 Agent 会自动触发执行"
    input_schema = {
        "type": "object",
        "properties": {
            "event_type": {"type": "string", "description": "事件类型名称，如 '问诊完成'"},
            "payload": {"type": "object", "description": "事件携带的数据"},
        },
        "required": ["event_type", "payload"],
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        from app.core.events.bus import EventBus
        await EventBus.publish(
            event_type=args["event_type"],
            payload=args["payload"],
            source_agent_id=context.agent_id,
            source_session_id=context.session_id,
            db=db,
        )
        return ToolResult(success=True, data={"event_type": args["event_type"]})
