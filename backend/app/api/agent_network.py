from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import SessionActor, get_session_actor
from app.services.agent_directory_service import (
    agent_card_view, discover_agent_cards, resolve_callable_agent,
)


router = APIRouter(prefix="/agent-network", tags=["agent-network"])


@router.get("/cards")
async def search_cards(
    query: str | None = None,
    domain_tag: list[str] = Query(default=[]),
    capability: list[str] = Query(default=[]),
    governance_label: list[str] = Query(default=[]),
    group_enabled: bool | None = True,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    if actor.account is None:
        raise HTTPException(status_code=403, detail={"code": "identity_mapping_required"})
    cards = await discover_agent_cards(
        db, tenant_id=actor.account.tenant_id, account_id=actor.account.id,
        query=query, domain_tags=domain_tag,
        group_enabled=group_enabled, capability_keys=capability,
        governance_labels=governance_label, limit=limit,
    )
    return {"items": [agent_card_view(card) for card in cards], "next_cursor": None}


@router.get("/agents/{agent_id}")
async def get_card(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    if actor.account is None:
        raise HTTPException(status_code=403, detail={"code": "identity_mapping_required"})
    try:
        _agent, card = await resolve_callable_agent(
            db, tenant_id=actor.account.tenant_id, account_id=actor.account.id,
            actor_agent_id="__http_directory__", target_agent_id=agent_id,
        )
        if not card.group_enabled:
            raise LookupError("Agent does not participate in the network")
    except (LookupError, PermissionError):
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent_card_view(card)
