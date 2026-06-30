from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext


class GetAgentSchemaTool(BaseTool):
    name = "get_agent_schema"
    description = "获取指定 Agent 的详细能力清单（capability_manifest），用于图编排前的能力确认"
    input_schema = {
        "type": "object",
        "properties": {
            "agent_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要查询的 Agent ID 列表",
            },
        },
        "required": ["agent_ids"],
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        from app.models.agent import Agent
        from sqlalchemy import select

        result = await db.execute(select(Agent).where(Agent.id.in_(args["agent_ids"])))
        agents = result.scalars().all()
        schemas = []
        for a in agents:
            gc = a.group_config or {}
            schemas.append({
                "id": a.id,
                "name": a.name,
                "description": a.description,
                "capability_manifest": gc.get("capability_manifest", []),
                "event_manifest": gc.get("event_manifest", []),
            })
        return ToolResult(success=True, data=schemas)
