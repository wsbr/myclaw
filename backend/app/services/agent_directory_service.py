"""Governed local AgentCard discovery for Runtime and HTTP projections."""

from __future__ import annotations

from sqlalchemy import select

from app.models.agent_v1 import (
    AgentAssemblyVersion, AgentBlueprint, AgentCard, AgentVersionBinding,
)
from app.models.foundation import PlatformAccountRole
from app.services.agent_access_service import agent_use_filter, has_agent_use_access


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
    db, *, tenant_id: str, account_id: str | None,
    actor_agent_id: str | None = None,
    actor_agent_version_id: str | None = None,
    query: str | None = None, domain_tags: list[str] | None = None,
    group_enabled: bool | None = None, capability_keys: list[str] | None = None,
    governance_labels: list[str] | None = None, limit: int = 20,
    exhaustive: bool = False,
) -> list[AgentCard]:
    """Return current published cards visible to the original account.

    Discovery is intentionally not authorization. Invocation repeats all
    visibility, publication, mode, and collaboration checks before creating a
    target WorkItem.
    """
    if actor_agent_id:
        actor = await db.scalar(select(AgentBlueprint).where(
            AgentBlueprint.id == actor_agent_id,
            AgentBlueprint.tenant_id == tenant_id,
        ))
        version_id = actor_agent_version_id or (
            actor.current_published_version_id if actor else None
        )
        actor_version = await db.get(AgentAssemblyVersion, version_id) if version_id else None
        if (
            actor is None or actor_version is None
            or actor_version.agent_id != actor_agent_id
            or actor_version.immutable_at is None
            or actor_version.publication_status not in {"published", "draft_test_snapshot"}
            or not actor_version.group_enabled
        ):
            return []
        # A model-supplied filter cannot opt out of the network boundary.
        if group_enabled is False:
            return []
        group_enabled = True
    limit = max(1, min(int(limit), 500))
    statement = (
        select(AgentCard)
        .join(AgentBlueprint, AgentBlueprint.id == AgentCard.agent_id)
        .join(AgentAssemblyVersion, AgentAssemblyVersion.id == AgentCard.agent_version_id)
        .where(
            AgentBlueprint.tenant_id == tenant_id,
            AgentBlueprint.status == "published",
            AgentBlueprint.current_published_version_id == AgentCard.agent_version_id,
            AgentAssemblyVersion.publication_status == "published",
            AgentAssemblyVersion.immutable_at.is_not(None),
            AgentCard.availability == "available",
        )
        .order_by(AgentCard.name, AgentCard.agent_id)
    )
    if account_id is None:
        statement = statement.where(AgentCard.visibility == "enterprise")
    else:
        is_admin = bool(await db.scalar(select(PlatformAccountRole.account_id).where(
            PlatformAccountRole.account_id == account_id,
            PlatformAccountRole.role_key == "admin",
        ).limit(1)))
        if not is_admin:
            statement = statement.where(agent_use_filter(account_id))
    if not exhaustive:
        statement = statement.limit(500)
    if actor_agent_id:
        statement = statement.where(AgentCard.agent_id != actor_agent_id)
    if group_enabled is not None:
        statement = statement.where(AgentCard.group_enabled.is_(group_enabled))
    rows = list(await db.scalars(statement))
    visible = rows
    normalized_query = str(query or "").strip().casefold()
    requested_tags = {str(item).strip().casefold() for item in domain_tags or [] if str(item).strip()}
    requested_capabilities = {
        str(item).strip().casefold() for item in capability_keys or [] if str(item).strip()
    }
    requested_governance = {
        str(item).strip().casefold() for item in governance_labels or [] if str(item).strip()
    }
    filtered: list[AgentCard] = []
    for card in visible:
        tags = {str(item).casefold() for item in card.domain_tags or []}
        collaboration = dict(card.collaboration_summary or {})
        governance = collaboration.get("governance") or {}
        if collaboration.get("discoverable") is False:
            continue
        if isinstance(governance, dict) and governance.get("discoverable") is False:
            continue
        if actor_agent_id and (
            collaboration.get("callable") is False
            or (isinstance(governance, dict) and (
                governance.get("callable") is False
                or governance.get("group_allowed") is False
            ))
        ):
            continue
        capabilities = {
            str(item).casefold()
            for item in (card.capabilities_summary or {}).get("tool_keys", [])
        }
        capabilities.update(
            str(item).casefold()
            for item in (card.capabilities_summary or {}).get("capabilities", [])
        )
        labels = set()
        if isinstance(governance, dict):
            labels = {str(item).casefold() for item in governance.get("labels", [])}
        haystack = " ".join((
            card.name, card.description, card.accepted_request_summary,
            " ".join(str(item) for item in card.supported_outcomes or []),
            " ".join(tags),
        )).casefold()
        if normalized_query and normalized_query not in haystack:
            continue
        if requested_tags and not requested_tags.issubset(tags):
            continue
        if requested_capabilities and not requested_capabilities.issubset(capabilities):
            continue
        if requested_governance and not requested_governance.issubset(labels):
            continue
        filtered.append(card)
        if len(filtered) >= limit:
            break
    return filtered


async def resolve_callable_agent(
    db, *, tenant_id: str, account_id: str | None,
    actor_agent_id: str, target_agent_id: str,
) -> tuple[AgentBlueprint, AgentCard]:
    target = await db.scalar(select(AgentBlueprint).where(
        AgentBlueprint.id == target_agent_id,
        AgentBlueprint.tenant_id == tenant_id,
    ))
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
    version = await db.get(AgentAssemblyVersion, card.agent_version_id)
    if (
        version is None or version.publication_status != "published"
        or version.immutable_at is None
    ):
        raise LookupError("target Agent version is not currently published")
    if account_id is None:
        visible = card.visibility == "enterprise"
    else:
        visible = await has_agent_use_access(
            db, agent=target, account_id=account_id,
        )
    if not visible:
        raise PermissionError("target Agent is outside the current discovery boundary")
    collaboration = dict(card.collaboration_summary or {})
    governance = collaboration.get("governance") or {}
    if collaboration.get("callable") is False or (
        isinstance(governance, dict) and governance.get("callable") is False
    ):
        raise PermissionError("target Agent is denied by collaboration governance")
    return target, card


async def resolve_bound_agent_cards(
    db, *, tenant_id: str, account_id: str | None,
    actor_agent_id: str, actor_agent_version_id: str,
) -> list[AgentCard]:
    """Project only this frozen caller's usable explicit targets, without network discovery."""
    version = await db.get(AgentAssemblyVersion, actor_agent_version_id)
    if version is None or version.agent_id != actor_agent_id:
        return []
    target_ids = list(await db.scalars(select(AgentVersionBinding.bound_agent_id).where(
        AgentVersionBinding.agent_version_id == actor_agent_version_id,
    ).order_by(AgentVersionBinding.sort_order)))
    cards: list[AgentCard] = []
    for target_id in target_ids:
        try:
            _agent, card = await resolve_callable_agent(
                db, tenant_id=tenant_id, account_id=account_id,
                actor_agent_id=actor_agent_id, target_agent_id=target_id,
            )
        except (LookupError, PermissionError):
            continue
        cards.append(card)
    return cards
