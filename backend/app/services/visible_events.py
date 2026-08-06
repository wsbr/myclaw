"""One authorization projection shared by REST, SSE, and Timeline."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.foundation import ExecutionEvent, ExecutionEventRecipient, UserSession
from app.models.group_v1 import GroupAgentNode, GroupMembership, GroupSession
from app.models.runtime_v1 import Message


@dataclass(frozen=True)
class EventPage:
    items: list[ExecutionEvent]
    scanned_through_sequence: int
    session_high_watermark: int
    has_more: bool


class VisibleEventProjection:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def scan_session_user(
        self,
        *,
        user_id: str,
        session_id: str,
        after_sequence: int,
        scan_limit: int = 500,
        limit: int = 100,
    ) -> EventPage:
        session = await self.db.get(UserSession, session_id)
        if session is None or session.user_id != user_id:
            raise LookupError("session not found")
        scan_limit = max(1, min(scan_limit, 1000))
        limit = max(1, min(limit, 100))
        scanned = list(await self.db.scalars(
            select(ExecutionEvent)
            .where(
                ExecutionEvent.session_id == session_id,
                ExecutionEvent.sequence > after_sequence,
            )
            .order_by(ExecutionEvent.sequence)
            .limit(scan_limit)
        ))
        ids = [event.id for event in scanned if event.visibility == "direct"]
        directly_visible: set[str] = set()
        if ids:
            directly_visible = set(await self.db.scalars(
                select(ExecutionEventRecipient.event_id).where(
                    ExecutionEventRecipient.event_id.in_(ids),
                    ExecutionEventRecipient.recipient_type == "user_identity",
                    ExecutionEventRecipient.recipient_id == user_id,
                )
            ))
        visible: list[ExecutionEvent] = []
        scanned_through = after_sequence
        for event in scanned:
            scanned_through = event.sequence
            is_visible = (
                event.visibility in ("user", "group")
                or (event.visibility == "direct" and event.id in directly_visible)
            )
            if is_visible:
                visible.append(event)
                if len(visible) >= limit:
                    break
        high = max(0, session.next_event_sequence - 1)
        return EventPage(
            items=visible,
            scanned_through_sequence=scanned_through,
            session_high_watermark=high,
            has_more=scanned_through < high,
        )

    async def list_direct_messages(
        self, *, user_id: str, session_id: str, mode: str,
        group_node_id: str | None = None, delegated: bool = False,
        hard_limit: int = 1000,
    ) -> list[Message]:
        """Return only the Message history directly visible to a Runner scope."""
        session = await self.db.get(UserSession, session_id)
        if session is None or session.user_id != user_id:
            raise LookupError("session not found")
        hard_limit = max(1, min(hard_limit, 2000))
        if mode == "group":
            if not group_node_id:
                raise LookupError("group node is required")
            node = await self.db.scalar(
                select(GroupAgentNode).join(
                    GroupSession, GroupSession.id == GroupAgentNode.group_session_id,
                ).where(
                    GroupAgentNode.id == group_node_id,
                    GroupSession.parent_session_id == session_id,
                    GroupSession.user_id == user_id,
                )
            )
            membership = None
            if node is None:
                membership = await self.db.scalar(select(GroupMembership).join(
                    GroupSession, GroupSession.id == GroupMembership.group_session_id,
                ).where(
                    GroupMembership.id == group_node_id,
                    GroupMembership.status == "joined",
                    GroupSession.parent_session_id == session_id,
                    GroupSession.user_id == user_id,
                ))
            if membership is None and node is None:
                raise LookupError("group node not found")
            group_session_id = (
                membership.group_session_id if membership is not None
                else node.group_session_id
            )
            agent_id = membership.agent_id if membership is not None else node.agent_id
            return list(await self.db.scalars(
                select(Message).where(
                    Message.session_id == session_id,
                    Message.scope_type == "group",
                    Message.group_session_id == group_session_id,
                    Message.status == "committed",
                    or_(
                        Message.visibility == "group",
                        Message.sender_agent_id == agent_id,
                        Message.recipient_agent_id == agent_id,
                        Message.recipient_group_node_id == group_node_id,
                    ),
                ).order_by(Message.created_at.desc(), Message.id.desc()).limit(hard_limit)
            ))[::-1]
        # A delegated Agent receives its explicit AgentInvocation request below;
        # ambient parent Chat/Plan history is not part of that principal's view.
        if delegated:
            return []
        return list(await self.db.scalars(
            select(Message).where(
                Message.session_id == session_id,
                Message.scope_type == "chat",
                Message.status == "committed",
            ).order_by(Message.created_at.desc(), Message.id.desc()).limit(hard_limit)
        ))[::-1]


def event_public_dict(event: ExecutionEvent) -> dict:
    return {
        "event_id": event.id,
        "session_id": event.session_id,
        "run_id": event.run_id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "entity_version": event.entity_version,
        "summary": event.summary,
        "payload": event.payload,
        "caused_by_event_id": event.caused_by_event_id,
        "correlation_id": event.correlation_id,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
