from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult


class RequestClarificationTool(BaseTool):
    """Runtime control Tool used to suspend a Work Item for a user answer."""

    name = "request_clarification"
    description = "向当前执行的原始用户提出一个必须回答的问题，并持久化等待其回复"
    input_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "需要用户回答的单个、明确问题",
                "minLength": 1,
                "maxLength": 8000,
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        return ToolResult(
            success=False,
            error="request_clarification must be dispatched by the durable runtime control gateway",
        )
