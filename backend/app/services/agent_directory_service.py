"""Governed local AgentCard discovery for Runtime and HTTP projections."""

from __future__ import annotations

from sqlalchemy import select

from app.models.agent_v1 import AgentBlueprint, AgentCard


def agent_card_view(card: AgentCard) -> dict:
    return {
        "agent_id": card.agent_id,
        "agent_version_id": card.agent_version_id,
        "card_content_hash": card.content_hash,
        "name": card.name,
        "description": card.description,
        "identity_kind": card.identity_kind,
        "domain_tags": list(card.domain_tags or []),
        "supported_outcomes": list(card.supported_outcomes or []),
        "accepted_request_summary": card.accepted_request_summary,
        "capabilities_summary": dict(card.capabilities_summary or {}),
        "collaboration_summary": dict(card.collaboration_summary or {}),
        "declaration": dict(card.declaration or {}),
        "attestation": dict(card.attestation or {}),
        "availability": card.availability,
        "group_enabled": card.group_enabled,
        "published_at": card.published_at.isoformat(),
    }


async def discover_agent_cards(
    db, *, account_id: str | None, actor_agent_id: str | None = None,
    query: str | None = None, domain_tags: list[str] | None = None,
    group_enabled: bool | None = None, limit: int = 20,
) -> list[AgentCard]:
    """Return current published cards visible to the original account.

    Discovery is intentionally not authorization. Invocation repeats all
    visibility, publication, mode, and collaboration checks before creating a
    target WorkItem.
    """
    limit = max(1, min(int(limit), 100))
    statement = (
        select(AgentCard)
        .join(AgentBlueprint, AgentBlueprint.id == AgentCard.agent_id)
        .where(
            AgentBlueprint.status == "published",
            AgentBlueprint.current_published_version_id == AgentCard.agent_version_id,
            AgentCard.availability == "available",
        )
        .order_by(AgentCard.name, AgentCard.agent_id)
        .limit(500)
    )
    if actor_agent_id:
        statement = statement.where(AgentCard.agent_id != actor_agent_id)
    if group_enabled is not None:
        statement = statement.where(AgentCard.group_enabled.is_(group_enabled))
    rows = list(await db.scalars(statement))
    agents = {} if not rows else {
        item.id: item for item in await db.scalars(select(AgentBlueprint).where(
            AgentBlueprint.id.in_([card.agent_id for card in rows])
        ))
    }
    visible = [
        card for card in rows
        if card.visibility == "enterprise"
        or (
            account_id is not None
            and agents.get(card.agent_id) is not None
            and agents[card.agent_id].owner_account_id == account_id
        )
    ]
    normalized_query = str(query or "").strip().casefold()
    requested_tags = {str(item).strip().casefold() for item in domain_tags or [] if str(item).strip()}
    filtered: list[AgentCard] = []
    for card in visible:
        tags = {str(item).casefold() for item in card.domain_tags or []}
        haystack = " ".join((
            card.name, card.description, card.accepted_request_summary,
            " ".join(str(item) for item in card.supported_outcomes or []),
            " ".join(tags),
        )).casefold()
        if normalized_query and normalized_query not in haystack:
            continue
        if requested_tags and not requested_tags.issubset(tags):
            continue
        filtered.append(card)
        if len(filtered) >= limit:
            break
    return filtered


async def resolve_callable_agent(
    db, *, account_id: str | None, actor_agent_id: str, target_agent_id: str,
) -> tuple[AgentBlueprint, AgentCard]:
    target = await db.get(AgentBlueprint, target_agent_id)
    if (
        target is None or target.id == actor_agent_id
        or target.status != "published" or not target.current_published_version_id
    ):
        raise LookupError("target Agent is unavailable")
    card = await db.scalar(select(AgentCard).where(
        AgentCard.agent_id == target.id,
        AgentCard.agent_version_id == target.current_published_version_id,
        AgentCard.availability == "available",
    ))
    if card is None:
        raise LookupError("target Agent has no current available AgentCard")
    if card.visibility != "enterprise" and (
        account_id is None or target.owner_account_id != account_id
    ):
        raise PermissionError("target Agent is outside the current discovery boundary")
    return target, card
