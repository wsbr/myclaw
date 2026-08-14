"""Explicit local KnowledgeBase retrieval with immutable citations."""

from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.services.knowledge_service import KnowledgeError, search_knowledge


class KnowledgeSearchTool(BaseTool):
    name = "knowledge_search"
    description = (
        "WHEN: retrieve evidence from local document knowledge explicitly granted to this Agent. "
        "WHEN NOT: do not use Knowledge as user Memory, do not assume missing evidence is false, "
        "and do not invent citations. RETURNS: bounded excerpts with exact source, page/section, "
        "checksum, chunk, and immutable index revision citations. RECOVERY: narrow or rephrase the "
        "query; ask the user to grant this Agent access when no KnowledgeBase is visible."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 1000},
            "knowledge_base_id": {
                "type": ["string", "null"], "maxLength": 36,
                "description": "Optional exact granted KnowledgeBase ID.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        if not context.account_id:
            raise KnowledgeError(
                "knowledge_account_authority_missing",
                "Knowledge retrieval requires an account authority",
                status_code=403,
            )
        data = await search_knowledge(
            db,
            tenant_id=context.tenant_id,
            account_id=context.account_id,
            agent_id=context.agent_id,
            runner_id=context.runner_id,
            query=args["query"],
            knowledge_base_id=args.get("knowledge_base_id"),
            limit=int(args.get("limit", 8)),
        )
        return ToolResult(
            status="succeeded",
            summary=f"Found {data['count']} local knowledge citations",
            data=data,
            content_blocks=[{"type": "json", "value": data}],
            evidence=[item["citation"] for item in data["results"]],
        )
