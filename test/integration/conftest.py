"""Shared fixtures for integration and e2e tests."""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Generator
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.fake_llm import DEFAULT_PLAN, FakeLLM


def _database_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "mysql+aiomysql://myclaw:mypassword@localhost:3307/myclaw_test",
    )


def _redis_available() -> bool:
    try:
        import redis

        url = os.environ.get("REDIS_URL", "redis://:redispassword@localhost:6380/0")
        r = redis.from_url(url)
        r.ping()
        return True
    except Exception:
        return False


def _mysql_available() -> bool:
    try:
        import re
        from urllib.parse import unquote

        import pymysql

        url = _database_url()
        match = re.match(
            r"mysql\+aiomysql://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)",
            url,
        )
        if not match:
            return False
        user, password, host, port, database = match.groups()
        conn = pymysql.connect(
            host=host,
            port=int(port),
            user=user,
            password=unquote(password),
            database=database,
            connect_timeout=3,
        )
        conn.ping()
        conn.close()
        return True
    except Exception:
        return False


requires_services = pytest.mark.skipif(
    not (_mysql_available() and _redis_available()),
    reason="MySQL (3307) and Redis (6380) required — run: docker compose -f docker-compose.test.yml up -d mysql-test redis-test",
)


@pytest.fixture(scope="session")
def fake_llm() -> FakeLLM:
    return FakeLLM(plan=DEFAULT_PLAN)


@pytest.fixture(scope="session", autouse=True)
def apply_test_env() -> Generator[None, None, None]:
    """Ensure test environment variables are set before app import."""
    os.environ.setdefault("DATABASE_URL", _database_url())
    os.environ.setdefault("REDIS_URL", "redis://:redispassword@localhost:6380/0")
    os.environ.setdefault("CELERY_BROKER_URL", "redis://:redispassword@localhost:6380/1")
    os.environ.setdefault("CELERY_RESULT_BACKEND", "redis://:redispassword@localhost:6380/2")
    os.environ.setdefault("SECRET_KEY", "test-secret-key-for-jwt-signing-32chars")
    os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-32-bytes-hex00")
    os.environ.setdefault("INIT_ADMIN_EMAIL", "admin@test.myclaw.local")
    os.environ.setdefault("INIT_ADMIN_PASSWORD", "testpassword123")
    os.environ.setdefault("DEBUG", "true")

    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def run_migrations(apply_test_env) -> Generator[None, None, None]:
    if not _mysql_available():
        yield
        return

    backend_dir = os.path.join(os.path.dirname(__file__), "..", "..", "backend")
    env = {**os.environ, "DATABASE_URL": _database_url()}
    subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=backend_dir,
        env=env,
        check=True,
        capture_output=True,
    )
    yield


@pytest_asyncio.fixture
async def db_engine(run_migrations):
    if not _mysql_available():
        pytest.skip("MySQL not available")
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def clean_db(db_engine) -> AsyncIterator[None]:
    """Truncate mutable tables between tests."""
    tables = [
        "spans", "runners", "execution_plans", "orchestration_graphs",
        "tasks", "sessions", "agent_versions", "agent_tools", "agent_skills",
        "agent_mcp_servers", "agent_builtin_tools", "agents",
        "available_models", "model_providers", "api_tokens", "users",
    ]
    async with db_engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in tables:
            await conn.execute(text(f"TRUNCATE TABLE `{table}`"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        from app.core.security import hash_password
        import uuid as _uuid

        admin_id = str(_uuid.uuid4())
        await conn.execute(
            text(
                "INSERT INTO users (id, email, username, password_hash, role, is_active) "
                "VALUES (:id, :email, :username, :password_hash, 'admin', 1)"
            ),
            {
                "id": admin_id,
                "email": os.environ["INIT_ADMIN_EMAIL"],
                "username": "testadmin",
                "password_hash": hash_password(os.environ["INIT_ADMIN_PASSWORD"]),
            },
        )
    yield


@pytest.fixture
def mock_model_hub(fake_llm: FakeLLM):
    with patch("app.core.model_hub.registry.ModelHub.get", return_value=fake_llm):
        with patch("app.runtime.plan_executor.ModelHub.get", return_value=fake_llm):
            with patch("app.runtime.agent_runtime.ModelHub.get", return_value=fake_llm):
                yield fake_llm


@pytest.fixture
def celery_eager():
    """Celery eager mode with per-thread DB engine to avoid event-loop conflicts."""
    import concurrent.futures

    import app.database as db_module
    from app.workers import task_worker
    from app.workers.celery_app import celery_app
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    prev = {
        "task_always_eager": celery_app.conf.task_always_eager,
        "task_eager_propagates": celery_app.conf.task_eager_propagates,
    }
    original_run_coro = task_worker._run_coro

    def patched_run_coro(factory):
        saved_engine = db_module.engine
        saved_session_local = db_module.AsyncSessionLocal

        def run_in_thread():
            async def _run():
                engine = create_async_engine(_database_url(), pool_pre_ping=True)
                db_module.engine = engine
                db_module.AsyncSessionLocal = async_sessionmaker(
                    engine, expire_on_commit=False, class_=AsyncSession
                )
                try:
                    return await factory()
                finally:
                    await engine.dispose()

            import asyncio
            return asyncio.run(_run())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(run_in_thread).result()

        db_module.engine = saved_engine
        db_module.AsyncSessionLocal = saved_session_local
        return result

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    task_worker._run_coro = patched_run_coro

    yield celery_app

    task_worker._run_coro = original_run_coro
    celery_app.conf.task_always_eager = prev["task_always_eager"]
    celery_app.conf.task_eager_propagates = prev["task_eager_propagates"]


@pytest_asyncio.fixture
async def app_client(clean_db, db_engine, mock_model_hub, celery_eager):
    import app.database as db_module
    from app.database import get_db
    from app.main import app

    old_engine = db_module.engine
    old_session_local = db_module.AsyncSessionLocal

    db_module.engine = db_engine
    db_module.AsyncSessionLocal = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with db_module.AsyncSessionLocal() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    db_module.engine = old_engine
    db_module.AsyncSessionLocal = old_session_local


@pytest_asyncio.fixture
async def auth_headers(app_client) -> dict[str, str]:
    resp = await app_client.post(
        "/api/v1/auth/login",
        json={
            "email": os.environ["INIT_ADMIN_EMAIL"],
            "password": os.environ["INIT_ADMIN_PASSWORD"],
        },
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def plan_agent(app_client, auth_headers) -> dict:
    """Create and activate a plan-mode agent with mock model provider."""
    provider_resp = await app_client.post(
        "/api/v1/model-providers",
        headers=auth_headers,
        json={
            "name": f"test-provider-{uuid.uuid4().hex[:8]}",
            "provider": "openai_compatible",
            "base_url": "http://mock-llm:9090/v1",
            "api_key": "sk-test",
        },
    )
    assert provider_resp.status_code == 201, provider_resp.text
    provider = provider_resp.json()

    await app_client.patch(
        f"/api/v1/model-providers/{provider['id']}/activate",
        headers=auth_headers,
    )

    agent_resp = await app_client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": f"plan-agent-{uuid.uuid4().hex[:8]}",
            "description": "Plan mode test agent",
            "modes": ["chat", "plan"],
            "model_config_": {"provider_id": provider["id"], "model_id": "mock-model"},
            "system_prompt_blocks": {"blocks": [{"type": "text", "content": "You are helpful."}]},
        },
    )
    assert agent_resp.status_code == 201, agent_resp.text
    agent = agent_resp.json()

    activate_resp = await app_client.post(
        f"/api/v1/agents/{agent['id']}/activate",
        headers=auth_headers,
    )
    assert activate_resp.status_code == 200, activate_resp.text
    return {**agent, "provider_id": provider["id"]}
