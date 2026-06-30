from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext


class FindAgentsTool(BaseTool):
    name = "find_agents"
    description = "在 Group 工作网中发现可协作的 Agent，按能力描述语义检索"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "能力描述，如 '获取用户画像' 或 '数据分析'"},
        },
        "required": ["query"],
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        from app.services.agent_service import AgentService
        agents = await AgentService.get_group_agents(db, exclude_id=context.agent_id)
        # Simple keyword match; replace with vector search in Phase 7
        query = args["query"].lower()
        matched = [
            a for a in agents
            if query in a["name"].lower() or query in a["description"].lower()
        ] or agents  # fallback: return all if no match
        return ToolResult(success=True, data=matched)
