"""Governed Memory search and candidate proposal Tools."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import or_, select

from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.models.agent_v1 import AgentAssemblyVersion
from app.models.common import uuid7
from app.models.memory_v1 import (
    MemoryCandidateSourceMessage, MemoryHead, MemoryRecord, MemoryWriteCandidate,
)
from app.models.runtime_v1 import DurableRunner, Message
from app.services.memory_service import (
    MemoryConflict, canonical_fact_key, memory_view, normalized_content,
)


class MemorySearchTool(BaseTool):
    name = "memory_search"
    description = (
        "WHEN: retrieve currently effective user or user-agent facts with their message provenance. "
        "WHEN NOT: do not assume a missing fact is false and do not use this to modify Memory. "
        "RETURNS: active facts, scope, versions, confidence, and user-message sources. "
        "RECOVERY: broaden the query or use an empty query for a bounded recent-fact inventory before asking the user."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 0, "maxLength": 500},
            "scope": {"type": "string", "enum": ["all", "user", "user_agent"], "default": "all"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        statement = select(MemoryRecord).where(
            MemoryRecord.user_id == context.user_id,
            MemoryRecord.status == "active",
            or_(
                MemoryRecord.scope == "user",
                (MemoryRecord.scope == "user_agent") & (MemoryRecord.agent_id == context.agent_id),
            ),
        )
        scope = args.get("scope", "all")
        if scope == "user":
            statement = statement.where(MemoryRecord.scope == "user")
        elif scope == "user_agent":
            statement = statement.where(MemoryRecord.scope == "user_agent")
        query = args["query"].strip()
        if query:
            statement = statement.where(or_(
                MemoryRecord.fact_key.like(f"%{query}%"),
                MemoryRecord.content.like(f"%{query}%"),
            ))
        statement = statement.order_by(
            MemoryRecord.updated_at.desc(), MemoryRecord.id.desc(),
        ).limit(int(args.get("limit", 20)))
        records = list(await db.scalars(statement))
        items = [await memory_view(db, record) for record in records]
        data = {"items": items, "count": len(items), "query": query}
        return ToolResult(
            status="succeeded", summary=f"Found {len(items)} supported Memory facts",
            data=data, content_blocks=[{"type": "json", "value": data}],
            evidence=[
                {"kind": "memory", "memory_id": item["id"], "version": item["version"], "sources": item["sources"]}
                for item in items
            ],
        )


class MemoryProposeTool(BaseTool):
    name = "memory_propose"
    description = (
        "WHEN: propose a durable fact explicitly supported by committed user messages. "
        "WHEN NOT: never write Memory directly or cite assistant/web/tool text as the source. "
        "RETURNS: candidate ID plus the trusted arbitration disposition, effective Memory ID, and provenance. "
        "RECOVERY: ask the user to state the fact when no user-authored source message exists."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["user", "user_agent"]},
            "fact_key": {"type": "string", "minLength": 1, "maxLength": 255},
            "content": {"type": "string", "minLength": 1, "maxLength": 20000},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string", "maxLength": 2000},
        },
        "required": ["scope", "fact_key", "content"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        runner = await db.get(DurableRunner, context.runner_id)
        assembly = await db.get(AgentAssemblyVersion, runner.agent_version_id) if runner else None
        if assembly is None or not assembly.memory_write_policy_id:
            raise MemoryConflict("the Runner has no frozen Memory write policy")
        # Source identity is platform-owned. Internal Message IDs are not
        # exposed to the model and the model cannot nominate assistant, Tool,
        # web, or cross-Session content as Memory provenance.
        source_message_id = (runner.input_snapshot or {}).get("message_id")
        message_ids = [source_message_id] if source_message_id else []
        if not message_ids:
            raise MemoryConflict(
                "a committed user-authored source message is required"
            )
        messages = list(await db.scalars(select(Message).where(
            Message.id.in_(message_ids), Message.session_id == context.session_id,
            Message.user_id == context.user_id, Message.sender_type == "user",
            Message.status == "committed",
        )))
        if len(messages) != len(message_ids):
            raise MemoryConflict("every source must be a committed user-authored message in this Session")
        fact_key = canonical_fact_key(args["fact_key"])
        content, digest = normalized_content(args["content"])
        head = await db.scalar(select(MemoryHead).where(
            MemoryHead.user_id == context.user_id,
            MemoryHead.scope == args["scope"],
            MemoryHead.fact_key == fact_key,
            MemoryHead.agent_id.is_(None) if args["scope"] == "user" else MemoryHead.agent_id == context.agent_id,
        ))
        candidate = MemoryWriteCandidate(
            id=uuid7(), user_id=context.user_id,
            agent_id=context.agent_id if args["scope"] == "user_agent" else None,
            scope=args["scope"], fact_key=fact_key, content=content,
            normalized_hash=digest, source_session_id=context.session_id,
            runner_id=context.runner_id,
            policy_definition_id=assembly.memory_write_policy_id,
            observed_head_version=head.version if head else None,
            confidence=Decimal(str(args.get("confidence", 0.8))),
            reason=args.get("reason"), status="pending",
        )
        db.add(candidate)
        await db.flush()
        db.add_all([
            MemoryCandidateSourceMessage(
                candidate_id=candidate.id, message_id=message.id, source_role="user",
            )
            for message in messages
        ])
        data = {
            "candidate_id": candidate.id, "status": candidate.status,
            "fact_key": fact_key, "source_message_ids": message_ids,
            "observed_head_version": candidate.observed_head_version,
        }
        return ToolResult(
            status="succeeded", summary="Memory candidate created for governed arbitration",
            data=data, content_blocks=[{"type": "json", "value": data}],
            evidence=[{"kind": "user_messages", "message_ids": message_ids}],
        )
