"""Builder options expose only usable current published policy versions."""

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.agent_options import router
from app.database import Base, get_db
from app.deps import get_current_builder
from app.models.common import uuid7
from app.models.registry import PolicyDefinition, PolicyVersion


pytestmark = pytest.mark.unit


def policy_rows(key, *, policy_type="context", enabled=True, status="active",
                version_status="published", has_current=True):
    definition = PolicyDefinition(
        id=uuid7(), policy_key=key, policy_type=policy_type, name=key,
        description=f"Description for {key}", implementation_ref=f"test:{key}",
        implementation_version="1", enabled=enabled, status=status,
    )
    version = PolicyVersion(
        id=uuid7(), policy_definition_id=definition.id, version_no=1,
        implementation_ref=f"test:{key}:v1", parameters={},
        content_hash="a" * 64, status=version_status,
    )
    definition.current_version_id = version.id if has_current else None
    return definition, version


async def test_options_require_active_enabled_and_own_published_current_version():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    concise = policy_rows("context.concise")
    evidence = policy_rows("context.evidence")
    history = policy_rows("history.recent", policy_type="history")
    disabled = policy_rows("context.disabled", enabled=False)
    inactive = policy_rows("context.inactive", status="inactive")
    deprecated = policy_rows("context.deprecated", version_status="deprecated")
    no_head = policy_rows("context.no-head", has_current=False)
    dangling_head = policy_rows("context.missing-version")
    dangling_head[0].current_version_id = uuid7()
    foreign_head = policy_rows("context.foreign-version")
    foreign_head[0].current_version_id = evidence[1].id
    # An older published revision is insufficient when the selected current
    # version is deprecated; the endpoint must not silently fall back.
    old_published = PolicyVersion(
        id=uuid7(), policy_definition_id=deprecated[0].id, version_no=2,
        implementation_ref="test:other-published", parameters={},
        content_hash="b" * 64, status="published",
    )
    async with factory.begin() as db:
        for pair in (concise, evidence, history, disabled, inactive, deprecated,
                     no_head, dangling_head, foreign_head):
            db.add_all(pair)
        db.add(old_published)

    app = FastAPI()
    app.include_router(router)

    async def database():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_builder] = lambda: object()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://policy-options.test",
        ) as client:
            response = await client.get("/agent-options/policy-definitions", params={"policy_type": "context"})
            assert response.status_code == 200
            assert response.json() == [{
                "id": definition.id, "key": definition.policy_key,
                "name": definition.name, "description": definition.description,
            } for definition in (concise[0], evidence[0])]
            history_response = await client.get(
                "/agent-options/policy-definitions", params={"policy_type": "history"},
            )
            assert history_response.status_code == 200
            assert [item["id"] for item in history_response.json()] == [history[0].id]
    finally:
        await engine.dispose()
