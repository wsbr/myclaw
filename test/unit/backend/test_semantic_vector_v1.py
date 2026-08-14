"""Semantic Embedding and Qdrant adapter contract tests."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.platform_secrets import secret_reference, store_secret
from app.database import Base
from app.models.common import utcnow
from app.models.foundation import OutboxCommand, PlatformAccount, UserIdentity
from app.models.knowledge_v1 import KnowledgeBase, KnowledgeChunk, KnowledgeIndexRevision
from app.models.registry import ConformanceRun, RegisteredModel
from app.models.registry import ModelVersion
from app.models.tenant_v1 import Tenant
from app.schemas.registry_v1 import ModelVersionCreate
from app.services.embedding_service import (
    EmbeddingBatch,
    EmbeddingProviderError,
    OpenAICompatibleEmbeddingClient,
    embedding_spec,
)
from app.services.knowledge_service import create_knowledge_base, ingest_source
from app.services.vector_store_service import (
    QdrantVectorStore,
    VectorPoint,
    collection_name,
)
from app.workers.knowledge_index_worker import KnowledgeIndexWorker
import app.workers.knowledge_index_worker as knowledge_worker_module


pytestmark = pytest.mark.unit


def embedding_version(*, dimensions: int = 8) -> ModelVersion:
    return ModelVersion(
        id="embedding-version", model_id="embedding-model", version_no=1,
        provider="openai_compatible", model_name="text-embedding-v4",
        endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
        credential_ref="secret://embedding", default_parameters={"dimensions": dimensions},
        capabilities={
            "model_kind": "embedding", "embedding_dimensions": dimensions,
            "embedding_max_batch_size": 10,
        },
        adapter_protocol="embedding.openai_compatible.v1",
        content_hash="a" * 64,
    )


def test_embedding_model_schema_requires_frozen_dimensions():
    accepted = ModelVersionCreate.model_validate({
        "provider": "openai_compatible",
        "model_name": "text-embedding-v4",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "unit-secret",
        "default_parameters": {"dimensions": 1024, "encoding_format": "float"},
        "capabilities": {
            "model_kind": "embedding",
            "embedding_dimensions": 1024,
            "embedding_max_batch_size": 10,
        },
    })
    assert accepted.capabilities["embedding_dimensions"] == 1024
    with pytest.raises(ValidationError, match="dimensions"):
        ModelVersionCreate.model_validate({
            **accepted.model_dump(),
            "api_key": "unit-secret",
            "default_parameters": {"dimensions": 768},
        })


@pytest.mark.asyncio
async def test_embedding_client_uses_openai_wire_and_validates_shape():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "data": [
                {"index": 1, "embedding": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
                {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
            ],
            "usage": {"prompt_tokens": 7},
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleEmbeddingClient(timeout_seconds=1, client=http)
    result = await client.embed(
        spec=embedding_spec(embedding_version()), api_key="same-bailian-key",
        texts=["恢复检查点", "故障后继续执行"],
    )
    assert result.vectors == (
        (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    )
    assert result.input_tokens == 7
    assert requests[0].url.path.endswith("/compatible-mode/v1/embeddings")
    assert requests[0].headers["authorization"] == "Bearer same-bailian-key"
    assert requests[0].read().decode().count('"dimensions":8') == 1
    await http.aclose()


@pytest.mark.asyncio
async def test_embedding_client_rejects_wrong_dimensions_without_leaking_body():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [{"index": 0, "embedding": [1.0, 2.0]}],
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleEmbeddingClient(timeout_seconds=1, client=http)
    with pytest.raises(EmbeddingProviderError) as caught:
        await client.embed(
            spec=embedding_spec(embedding_version()), api_key="never-print-me",
            texts=["probe"],
        )
    assert caught.value.code == "embedding_vector_invalid"
    assert "never-print-me" not in str(caught.value)
    await http.aclose()


@pytest.mark.asyncio
async def test_qdrant_adapter_creates_filtered_collection_upserts_and_queries(monkeypatch):
    monkeypatch.setattr(
        "app.services.vector_store_service.settings.knowledge_qdrant_collection_prefix",
        "myclaw_test",
    )
    observed: list[tuple[str, str, dict | None]] = []
    collection_exists = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal collection_exists
        body = None
        if request.content:
            body = __import__("json").loads(request.content)
        observed.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path.startswith("/collections/"):
            if not collection_exists:
                return httpx.Response(404, json={"status": "not found"})
            return httpx.Response(200, json={
                "result": {"config": {"params": {"vectors": {"size": 3}}}},
            })
        if request.method == "PUT" and request.url.path.count("/") == 2:
            collection_exists = True
            return httpx.Response(200, json={"result": True})
        if request.method == "POST" and request.url.path.endswith("/points/query"):
            return httpx.Response(200, json={
                "result": {"points": [{"id": "chunk-1", "score": 0.91}]},
            })
        return httpx.Response(200, json={"result": True})

    http = httpx.AsyncClient(
        base_url="http://qdrant.test:6333", transport=httpx.MockTransport(handler),
    )
    store = QdrantVectorStore(client=http)
    name = collection_name(dimensions=3, model_content_hash="b" * 64)
    await store.ensure_collection(name=name, dimensions=3)
    await store.upsert(collection=name, points=[VectorPoint(
        id="chunk-1", vector=(1.0, 0.0, 0.0),
        payload={
            "tenant_id": "tenant-a", "knowledge_base_id": "base-a",
            "index_revision_id": "revision-a",
        },
    )])
    matches = await store.query(
        collection=name, vector=(1.0, 0.0, 0.0), tenant_id="tenant-a",
        revision_ids=["revision-a"], limit=8,
    )
    assert matches[0].id == "chunk-1"
    assert matches[0].score == pytest.approx(0.91)
    create = next(item for item in observed if item[0] == "PUT" and item[1] == f"/collections/{name}")
    assert create[2] == {"vectors": {"size": 3, "distance": "Cosine"}}
    query = next(item for item in observed if item[0] == "POST")
    assert query[2]["filter"]["must"] == [
        {"key": "tenant_id", "match": {"value": "tenant-a"}},
        {"key": "index_revision_id", "match": {"any": ["revision-a"]}},
    ]
    await http.aclose()


@pytest.mark.asyncio
async def test_semantic_revision_is_durable_and_worker_publishes_only_after_vectors(
    tmp_path, monkeypatch,
):
    tenant_id = "01e00000-0000-7000-8000-000000000001"
    account_id = "01e00000-0000-7000-8000-000000000002"
    model_id = "01e00000-0000-7000-8000-000000000003"
    version_id = "01e00000-0000-7000-8000-000000000004"
    monkeypatch.setattr(settings, "knowledge_root", str(tmp_path / "knowledge"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(knowledge_worker_module, "AsyncSessionLocal", factory)

    async with factory.begin() as db:
        db.add_all([
            Tenant(id=tenant_id, slug="semantic", name="Semantic", status="active"),
            UserIdentity(user_id="semantic-owner", status="active"),
            PlatformAccount(
                id=account_id, tenant_id=tenant_id, user_id="semantic-owner",
                display_name="Semantic Owner", status="active",
            ),
        ])
        await db.flush()
        secret = await store_secret(
            db, plaintext="same-bailian-key", purpose=f"model:{model_id}",
            created_by_account_id=account_id,
        )
        model = RegisteredModel(
            id=model_id, key="embedding.primary", name="Bailian Embedding",
            status="active", current_version_id=None, next_version_no=2,
            provider="openai_compatible", model_name="text-embedding-v4",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            credential_ref=secret_reference(secret), default_parameters={"dimensions": 8},
            capabilities={"model_kind": "embedding", "embedding_dimensions": 8},
            enabled=True, created_by_account_id=account_id,
        )
        db.add(model)
        await db.flush()
        version = ModelVersion(
            id=version_id, model_id=model_id, version_no=1,
            provider="openai_compatible", model_name="text-embedding-v4",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            credential_ref=secret_reference(secret),
            default_parameters={"dimensions": 8},
            capabilities={
                "model_kind": "embedding", "embedding_dimensions": 8,
                "embedding_max_batch_size": 10,
            },
            adapter_protocol="embedding.openai_compatible.v1", content_hash="c" * 64,
            published_by_account_id=account_id,
        )
        db.add(version)
        await db.flush()
        model.current_version_id = version.id
        db.add(ConformanceRun(
            subject_type="model_version", subject_id=version.id,
            suite_version=settings.embedding_conformance_suite_version,
            idempotency_key="embedding-pass", request_hash="d" * 64,
            status="passed", started_at=utcnow(), ended_at=utcnow(),
            result_summary={"passed": True}, content_hash="e" * 64,
        ))
        await db.flush()
        base = await create_knowledge_base(
            db, tenant_id=tenant_id, account_id=account_id,
            name="Recovery Semantics", description="semantic evidence",
            agent_ids=[], embedding_model_id=model_id,
        )
        _, _, revision, replay = await ingest_source(
            db, knowledge_base=base, account_id=account_id,
            filename="recovery.md",
            data="# 恢复\n\n节点故障后应从持久化检查点继续，不应重复结算。".encode(),
        )
        assert replay is False
        assert revision.status == "queued"
        assert base.current_index_revision_id is None
        assert base.pending_index_revision_id == revision.id
        command = await db.scalar(select(OutboxCommand).where(
            OutboxCommand.topic == "knowledge.index",
        ))
        assert command.aggregate_id == revision.id
        revision_id = revision.id
        base_id = base.id

    class FakeEmbedding:
        async def embed(self, *, spec, api_key, texts):
            assert api_key == "same-bailian-key"
            assert spec.model_name == "text-embedding-v4"
            return EmbeddingBatch(
                vectors=tuple((1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0) for _ in texts),
                input_tokens=len(texts),
            )

        async def close(self):
            return None

    class FakeVectors:
        def __init__(self):
            self.points = []

        async def ensure_collection(self, *, name, dimensions):
            assert dimensions == 8
            assert name.startswith("myclaw_knowledge_d8_")

        async def upsert(self, *, collection, points):
            del collection
            self.points.extend(points)

        async def close(self):
            return None

    vectors = FakeVectors()
    worker = KnowledgeIndexWorker(
        worker_id="knowledge-unit", embedding_client=FakeEmbedding(),
        vector_store=vectors,
    )
    claim = await worker.claim_one()
    assert claim is not None
    await worker.process(claim)
    await worker.close()

    async with factory() as db:
        revision = await db.get(KnowledgeIndexRevision, revision_id)
        base = await db.get(KnowledgeBase, base_id)
        chunks = list(await db.scalars(select(KnowledgeChunk).where(
            KnowledgeChunk.index_revision_id == revision_id,
        )))
        command = await db.scalar(select(OutboxCommand).where(
            OutboxCommand.aggregate_id == revision_id,
        ))
        assert revision.status == "ready"
        assert revision.vector_backend == "qdrant"
        assert revision.vector_point_count == len(chunks) == len(vectors.points) > 0
        assert all(chunk.embedding_checksum for chunk in chunks)
        assert base.current_index_revision_id == revision_id
        assert base.pending_index_revision_id is None
        assert command.status == "completed"
    await engine.dispose()
