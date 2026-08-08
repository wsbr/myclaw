from __future__ import annotations

import base64
import json
import re

from sqlalchemy import and_, exists, func, or_, select

from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.models.foundation import ExecutionEvent, ExecutionEventRecipient
from app.models.group_v1 import GroupMembership, GroupSession
from app.models.runtime_v1 import Message
from app.models.artifact_v1 import Artifact


def _encode_cursor(after: int, upper: int) -> str:
    raw = json.dumps({"after": after, "upper": upper}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[int, int]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        data = json.loads(raw)
        after, upper = int(data["after"]), int(data["upper"])
        if after < 0 or upper < after:
            raise ValueError
        return after, upper
    except Exception as error:
        raise ValueError("invalid search cursor") from error


class SearchGroupHistoryTool(BaseTool):
    name = "search_group_history"
    description = (
        "WHEN: retrieve prior shared or directly addressed evidence from the current governed Group. "
        "WHEN NOT: never use outside a Group or to access another member's private runtime data. "
        "RETURNS: bounded visible messages, Artifacts, events, sequence bounds, and a stable cursor. "
        "RECOVERY: narrow terms or continue with next_cursor when more evidence remains."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 1000},
            "agent_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
            "event_types": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
            "from_sequence": {"type": "integer", "minimum": 1},
            "to_sequence": {"type": "integer", "minimum": 1},
            "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        if db is None:
            return ToolResult(success=False, error="database connection is required")
        if not context.group_session_id or not context.group_membership_id:
            return ToolResult(success=False, error="search_group_history requires a current Group membership")
        membership = await db.scalar(select(GroupMembership).join(
            GroupSession, GroupSession.id == GroupMembership.group_session_id,
        ).where(
            GroupMembership.id == context.group_membership_id,
            GroupMembership.group_session_id == context.group_session_id,
            GroupMembership.agent_id == context.agent_id,
            GroupMembership.status == "joined",
            GroupSession.session_id == context.session_id,
            GroupSession.status.in_(("open", "closed")),
        ))
        if membership is None:
            return ToolResult(success=False, error="current Agent is not a member of this Group")
        query = args.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            return ToolResult(success=False, error="query must be 1-1000 characters")
        # Treat whitespace-separated words as AND terms. Requiring the entire
        # query to be one contiguous substring makes Chinese queries such as
        # "华东仓 取消率" miss "华东仓订单取消率" even though both terms exist.
        query_terms = tuple(dict.fromkeys(
            term.casefold() for term in re.split(r"\s+", query.strip()) if term
        ))
        limit = args.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            return ToolResult(success=False, error="limit must be between 1 and 100")
        current_max = int(await db.scalar(select(func.max(ExecutionEvent.sequence)).where(
            ExecutionEvent.session_id == context.session_id,
            ExecutionEvent.group_session_id == context.group_session_id,
        )) or 0)
        if args.get("cursor"):
            after, upper = _decode_cursor(args["cursor"])
            upper = min(upper, current_max)
        else:
            after, upper = 0, current_max
        from_sequence = args.get("from_sequence")
        to_sequence = args.get("to_sequence")
        if from_sequence is not None:
            if not isinstance(from_sequence, int) or isinstance(from_sequence, bool) or from_sequence < 1:
                return ToolResult(success=False, error="from_sequence must be a positive integer")
            after = max(after, from_sequence - 1)
        if to_sequence is not None:
            if not isinstance(to_sequence, int) or isinstance(to_sequence, bool) or to_sequence < 1:
                return ToolResult(success=False, error="to_sequence must be a positive integer")
            upper = min(upper, to_sequence)
        direct_to_principal = and_(
            ExecutionEvent.visibility == "direct",
            exists(select(ExecutionEventRecipient.event_id).where(
                ExecutionEventRecipient.event_id == ExecutionEvent.id,
                or_(
                    and_(
                        ExecutionEventRecipient.recipient_type == "agent",
                        ExecutionEventRecipient.recipient_id == context.agent_id,
                    ),
                    and_(
                        ExecutionEventRecipient.recipient_type == "group_membership",
                        ExecutionEventRecipient.recipient_id == context.group_membership_id,
                    ),
                ),
            )),
        )
        search_conditions = [or_(
            func.lower(ExecutionEvent.summary).contains(term),
            and_(Message.id.is_not(None), func.lower(Message.content).contains(term)),
            and_(Artifact.id.is_not(None), func.lower(Artifact.name).contains(term)),
        ) for term in query_terms]
        statement = select(ExecutionEvent, Message, Artifact).outerjoin(
            Message,
            and_(ExecutionEvent.entity_type == "message", Message.id == ExecutionEvent.entity_id),
        ).outerjoin(
            Artifact,
            and_(ExecutionEvent.entity_type == "artifact", Artifact.id == ExecutionEvent.entity_id),
        ).where(
            ExecutionEvent.session_id == context.session_id,
            ExecutionEvent.group_session_id == context.group_session_id,
            ExecutionEvent.sequence > after,
            ExecutionEvent.sequence <= upper,
            or_(ExecutionEvent.visibility == "group", direct_to_principal),
            *search_conditions,
        )
        agent_ids = args.get("agent_ids")
        if agent_ids is not None:
            if not isinstance(agent_ids, list) or any(not isinstance(item, str) for item in agent_ids):
                return ToolResult(success=False, error="agent_ids must be an array of strings")
            statement = statement.where(or_(
                ExecutionEvent.actor_id.in_(agent_ids), Message.sender_agent_id.in_(agent_ids),
            ))
        event_types = args.get("event_types")
        if event_types is not None:
            if not isinstance(event_types, list) or any(not isinstance(item, str) for item in event_types):
                return ToolResult(success=False, error="event_types must be an array of strings")
            statement = statement.where(ExecutionEvent.event_type.in_(event_types))
        rows = list((await db.execute(statement.order_by(ExecutionEvent.sequence).limit(limit + 1))).all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        hits = []
        for event, message, artifact in rows:
            hits.append({
                "kind": "message" if message is not None else "artifact" if artifact is not None else "event",
                "id": message.id if message is not None else artifact.id if artifact is not None else event.id,
                "event_type": event.event_type,
                "visibility": event.visibility,
                "sequence": event.sequence,
                "summary": (message.content[:4000] if message is not None else
                            f"{artifact.name} ({artifact.mime_type}, {artifact.size_bytes} bytes)" if artifact is not None
                            else event.summary[:2000]),
                "actor_id": event.actor_id,
                "caused_by_event_id": event.caused_by_event_id,
                "group_run_id": event.group_run_id,
            })
        last_sequence = rows[-1][0].sequence if rows else after
        data = {
            "hits": hits,
            "scanned_through_sequence": upper if not has_more else last_sequence,
            "max_sequence": upper,
            "next_cursor": _encode_cursor(last_sequence, upper) if has_more else None,
        }
        return ToolResult(
            status="succeeded", summary=f"Found {len(hits)} visible Group history items",
            data=data, content_blocks=[{"type": "json", "value": data}],
            evidence=[{
                "kind": "group_history", "group_session_id": context.group_session_id,
                "scanned_through_sequence": data["scanned_through_sequence"],
            }],
        )
