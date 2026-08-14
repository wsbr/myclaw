"""Real MySQL gate for local Knowledge indexing, isolation, and reconciliation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models.agent_v1 import AgentBlueprint
from app.models.common import uuid7
from app.models.foundation import PlatformAccount, UserIdentity
from app.models.knowledge_v1 import KnowledgeBase, KnowledgeBaseAgentAccess
from app.models.tenant_v1 import Tenant
from app.services.knowledge_service import (
    create_knowledge_base,
    ingest_source,
    reconcile_knowledge_storage,
    search_knowledge,
)


pytestmark = pytest.mark.integration


def _database_url() -> str:
    value = os.getenv("MYCLAW_V3_ACCEPTANCE_DATABASE_URL")
    if not value:
        pytest.skip(
            "MYCLAW_V3_ACCEPTANCE_DATABASE_URL selects the real migrated MySQL database",
        )
    return value


@pytest.mark.asyncio
async def test_mysql_knowledge_index_tenant_fk_and_missing_body_repair(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(settings, "knowledge_root", str(tmp_path / "knowledge"))
    engine = create_async_engine(_database_url(), isolation_level="READ COMMITTED")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid7(), uuid7()
    account_a, account_b = uuid7(), uuid7()
    agent_a, agent_b = uuid7(), uuid7()
    user_a, user_b = f"knowledge-{uuid7()}", f"knowledge-{uuid7()}"

    async with factory.begin() as db:
        db.add_all([
            Tenant(id=tenant_a, slug=f"ka-{tenant_a}", name="Knowledge A", status="active"),
            Tenant(id=tenant_b, slug=f"kb-{tenant_b}", name="Knowledge B", status="active"),
            UserIdentity(user_id=user_a, status="active"),
            UserIdentity(user_id=user_b, status="active"),
        ])
    async with factory.begin() as db:
        db.add_all([
            PlatformAccount(
                id=account_a, tenant_id=tenant_a, user_id=user_a,
                display_name="Knowledge A", status="active",
            ),
            PlatformAccount(
                id=account_b, tenant_id=tenant_b, user_id=user_b,
                display_name="Knowledge B", status="active",
            ),
        ])
    async with factory.begin() as db:
        db.add_all([
            AgentBlueprint(
                id=agent_a, tenant_id=tenant_a, owner_account_id=account_a,
                visibility="private", status="draft",
            ),
            AgentBlueprint(
                id=agent_b, tenant_id=tenant_b, owner_account_id=account_b,
                visibility="private", status="draft",
            ),
        ])
    async with factory.begin() as db:
        base = await create_knowledge_base(
            db, tenant_id=tenant_a, account_id=account_a,
            name=f"MySQL {uuid7()}", description="integration",
            agent_ids=[agent_a],
        )
        _, source, revision, replay = await ingest_source(
            db, knowledge_base=base, account_id=account_a,
            filename="mysql.md",
            data=b"# Durable evidence\n\nUse the exact fencing token.",
        )
        base_id, source_id = base.id, source.id
        storage_key = source.storage_key
        assert replay is False
        assert revision.revision_no == 1

    async with factory() as db:
        db.add(KnowledgeBaseAgentAccess(
            id=uuid7(), tenant_id=tenant_b, knowledge_base_id=base_id,
            agent_id=agent_b, granted_by_account_id=account_b,
        ))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()

    async with factory() as db:
        result = await search_knowledge(
            db, tenant_id=tenant_a, account_id=account_a,
            agent_id=agent_a, knowledge_base_id=base_id,
            query="fencing token", limit=5,
        )
        assert result["results"][0]["citation"]["source_id"] == source_id

    (Path(settings.knowledge_root) / storage_key).unlink()
    async with factory.begin() as db:
        base = await db.get(KnowledgeBase, base_id)
        report = await reconcile_knowledge_storage(
            db, knowledge_base=base, account_id=account_a, repair=True,
        )
        assert report["missing_sources"][0]["source_id"] == source_id
        assert report["index_revision"]["chunk_count"] == 0

    await engine.dispose()
