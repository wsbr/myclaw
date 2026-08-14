"""Durable semantic Embedding and Qdrant indexing worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, or_, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.common import utcnow, uuid7
from app.models.foundation import OutboxCommand
from app.models.knowledge_v1 import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeIndexRevision,
    KnowledgeSource,
)
from app.models.registry import ModelVersion
from app.persistence.reliable_writes import tenant_command_error
from app.services.embedding_service import (
    EmbeddingProviderError,
    EmbeddingSpec,
    OpenAICompatibleEmbeddingClient,
    embedding_spec,
)
from app.services.knowledge_service import (
    ChunkDraft,
    KnowledgeError,
    chunk_blocks,
    load_source_bytes,
    parse_source_bytes,
)
from app.services.provider_credentials import resolve_provider_credential
from app.services.vector_store_service import (
    QdrantVectorStore,
    VectorPoint,
    VectorStoreError,
    collection_name,
)
from app.services.worker_heartbeat_service import run_worker_heartbeat


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexChunk:
    id: str
    document_id: str
    source_id: str
    ordinal: int
    draft: ChunkDraft


@dataclass(frozen=True)
class KnowledgeIndexClaim:
    command_id: str
    lease_token: str
    attempt_count: int
    tenant_id: str
    account_id: str
    knowledge_base_id: str
    revision_id: str
    chunking_profile: dict
    manifest: tuple[dict, ...]
    sources: tuple[KnowledgeSource, ...]
    spec: EmbeddingSpec
    credential: str


def _embedding_checksum(vector: tuple[float, ...]) -> str:
    raw = json.dumps(vector, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _chunk_id(revision_id: str, source_id: str, ordinal: int) -> str:
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"myclaw:knowledge:{revision_id}:{source_id}:{ordinal}",
    ))


class KnowledgeIndexWorker:
    def __init__(
        self, worker_id: str | None = None,
        *, embedding_client: OpenAICompatibleEmbeddingClient | None = None,
        vector_store: QdrantVectorStore | None = None,
    ) -> None:
        self.worker_id = worker_id or (
            f"knowledge:{socket.gethostname()}:{os.getpid()}:{uuid7()}"
        )
        self.capacity = settings.knowledge_worker_concurrency
        self._active_slots = 0
        self._embedding = embedding_client or OpenAICompatibleEmbeddingClient(
            timeout_seconds=settings.knowledge_embedding_timeout_seconds,
        )
        self._vectors = vector_store or QdrantVectorStore()

    async def close(self) -> None:
        await asyncio.gather(
            self._embedding.close(), self._vectors.close(),
            return_exceptions=True,
        )

    async def claim_one(self) -> KnowledgeIndexClaim | None:
        async with AsyncSessionLocal.begin() as db:
            now = utcnow()
            command = await db.scalar(select(OutboxCommand).where(
                OutboxCommand.topic == "knowledge.index",
                OutboxCommand.action == "build_semantic_index",
                or_(
                    (
                        OutboxCommand.status.in_(("pending", "retry_wait"))
                        & (OutboxCommand.available_at <= now)
                    ),
                    (
                        (OutboxCommand.status == "claimed")
                        & (OutboxCommand.dispatch_lease_expires_at <= now)
                    ),
                ),
            ).order_by(
                OutboxCommand.priority, OutboxCommand.created_at,
            ).with_for_update(skip_locked=True).limit(1))
            if command is None:
                return None
            revision = await db.scalar(select(KnowledgeIndexRevision).where(
                KnowledgeIndexRevision.id == command.aggregate_id,
            ).with_for_update())
            knowledge_base = (
                await db.get(KnowledgeBase, revision.knowledge_base_id)
                if revision is not None else None
            )
            authority_error = (
                tenant_command_error(
                    command,
                    tenant_id=revision.tenant_id,
                    account_id=revision.created_by_account_id,
                )
                if revision is not None else "knowledge_revision_missing"
            )
            if (
                revision is None or knowledge_base is None
                or knowledge_base.id != command.partition_key
                or authority_error is not None
            ):
                command.status = "dead"
                command.last_error = authority_error or "knowledge_authority_mismatch"
                command.completed_at = now
                command.dispatch_lease_owner = None
                command.dispatch_lease_token = None
                command.dispatch_lease_expires_at = None
                return None
            if revision.status == "ready":
                command.status = "completed"
                command.completed_at = now
                command.last_error = None
                command.dispatch_lease_owner = None
                command.dispatch_lease_token = None
                command.dispatch_lease_expires_at = None
                return None
            model_version = (
                await db.get(ModelVersion, revision.embedding_model_version_id)
                if revision.embedding_model_version_id else None
            )
            if model_version is None:
                command.status = "dead"
                command.last_error = "embedding_model_version_missing"
                command.completed_at = now
                revision.status = "failed"
                revision.error_code = "embedding_model_version_missing"
                revision.completed_at = now
                command.dispatch_lease_owner = None
                command.dispatch_lease_token = None
                command.dispatch_lease_expires_at = None
                return None
            try:
                spec = embedding_spec(model_version)
                credential = await resolve_provider_credential(
                    db, model_version.credential_ref,
                )
            except Exception as error:
                command.status = "dead"
                command.last_error = getattr(error, "code", type(error).__name__)
                command.completed_at = now
                revision.status = "failed"
                revision.error_code = command.last_error
                revision.completed_at = now
                command.dispatch_lease_owner = None
                command.dispatch_lease_token = None
                command.dispatch_lease_expires_at = None
                return None
            source_ids = [str(item["source_id"]) for item in revision.source_manifest]
            sources = tuple(await db.scalars(select(KnowledgeSource).where(
                KnowledgeSource.tenant_id == revision.tenant_id,
                KnowledgeSource.id.in_(source_ids),
                KnowledgeSource.status.in_(("indexed", "superseded")),
            ))) if source_ids else ()
            if {item.id for item in sources} != set(source_ids):
                command.status = "dead"
                command.last_error = "knowledge_source_manifest_unavailable"
                command.completed_at = now
                revision.status = "failed"
                revision.error_code = command.last_error
                revision.completed_at = now
                command.dispatch_lease_owner = None
                command.dispatch_lease_token = None
                command.dispatch_lease_expires_at = None
                return None
            token = uuid7()
            command.status = "claimed"
            command.attempt_count += 1
            command.dispatch_lease_owner = self.worker_id
            command.dispatch_lease_token = token
            command.dispatch_lease_expires_at = now + timedelta(
                seconds=settings.knowledge_index_claim_lease_seconds,
            )
            revision.status = "running"
            revision.started_at = revision.started_at or now
            revision.error_code = None
            revision.error_detail = None
            return KnowledgeIndexClaim(
                command_id=command.id, lease_token=token,
                attempt_count=command.attempt_count,
                tenant_id=revision.tenant_id,
                account_id=revision.created_by_account_id,
                knowledge_base_id=revision.knowledge_base_id,
                revision_id=revision.id,
                chunking_profile=dict(revision.chunking_profile or {}),
                manifest=tuple(dict(item) for item in revision.source_manifest),
                sources=sources,
                spec=spec, credential=credential,
            )

    async def _heartbeat_claim(self, claim: KnowledgeIndexClaim, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.knowledge_index_heartbeat_seconds,
                )
                return
            except TimeoutError:
                pass
            async with AsyncSessionLocal.begin() as db:
                command = await db.scalar(select(OutboxCommand).where(
                    OutboxCommand.id == claim.command_id,
                    OutboxCommand.status == "claimed",
                    OutboxCommand.dispatch_lease_token == claim.lease_token,
                ).with_for_update())
                if command is None:
                    return
                command.dispatch_lease_expires_at = utcnow() + timedelta(
                    seconds=settings.knowledge_index_claim_lease_seconds,
                )

    async def _build(self, claim: KnowledgeIndexClaim) -> tuple[list[IndexChunk], list[tuple[float, ...]], str]:
        source_by_id = {item.id: item for item in claim.sources}
        chunks: list[IndexChunk] = []
        for manifest_item in claim.manifest:
            source = source_by_id[str(manifest_item["source_id"])]
            data = await asyncio.to_thread(load_source_bytes, source)
            blocks = await asyncio.to_thread(
                parse_source_bytes, data, media_type=source.media_type,
            )
            drafts = await asyncio.to_thread(
                chunk_blocks, blocks,
                chunk_chars=int(claim.chunking_profile["chunk_chars"]),
                overlap_chars=int(claim.chunking_profile["overlap_chars"]),
            )
            for ordinal, draft in enumerate(drafts, 1):
                chunks.append(IndexChunk(
                    id=_chunk_id(claim.revision_id, source.id, ordinal),
                    document_id=str(manifest_item["document_id"]),
                    source_id=source.id, ordinal=ordinal, draft=draft,
                ))
                if len(chunks) > settings.knowledge_max_chunks_per_base:
                    raise KnowledgeError(
                        "knowledge_chunk_limit",
                        f"Knowledge base exceeds {settings.knowledge_max_chunks_per_base} chunks",
                    )
        vectors: list[tuple[float, ...]] = []
        for offset in range(0, len(chunks), claim.spec.max_batch_size):
            batch = chunks[offset:offset + claim.spec.max_batch_size]
            result = await self._embedding.embed(
                spec=claim.spec, api_key=claim.credential,
                texts=[item.draft.content for item in batch],
            )
            vectors.extend(result.vectors)
        collection = collection_name(
            dimensions=claim.spec.dimensions,
            model_content_hash=claim.spec.model_content_hash,
        )
        await self._vectors.ensure_collection(
            name=collection, dimensions=claim.spec.dimensions,
        )
        for offset in range(0, len(chunks), 256):
            batch_chunks = chunks[offset:offset + 256]
            batch_vectors = vectors[offset:offset + 256]
            await self._vectors.upsert(
                collection=collection,
                points=[VectorPoint(
                    id=item.id, vector=vector,
                    payload={
                        "tenant_id": claim.tenant_id,
                        "knowledge_base_id": claim.knowledge_base_id,
                        "index_revision_id": claim.revision_id,
                        "document_id": item.document_id,
                        "source_id": item.source_id,
                        "ordinal": item.ordinal,
                    },
                ) for item, vector in zip(batch_chunks, batch_vectors)],
            )
        return chunks, vectors, collection

    async def _complete(
        self, claim: KnowledgeIndexClaim, chunks: list[IndexChunk],
        vectors: list[tuple[float, ...]], collection: str,
    ) -> None:
        async with AsyncSessionLocal.begin() as db:
            command = await db.scalar(select(OutboxCommand).where(
                OutboxCommand.id == claim.command_id,
                OutboxCommand.status == "claimed",
                OutboxCommand.dispatch_lease_token == claim.lease_token,
            ).with_for_update())
            revision = await db.scalar(select(KnowledgeIndexRevision).where(
                KnowledgeIndexRevision.id == claim.revision_id,
            ).with_for_update())
            if command is None or revision is None:
                return
            knowledge_base = await db.scalar(select(KnowledgeBase).where(
                KnowledgeBase.id == claim.knowledge_base_id,
                KnowledgeBase.tenant_id == claim.tenant_id,
            ).with_for_update())
            if knowledge_base is None:
                raise KnowledgeError(
                    "knowledge_base_not_found", "Knowledge base disappeared during indexing",
                )
            await db.execute(delete(KnowledgeChunk).where(
                KnowledgeChunk.index_revision_id == revision.id,
                KnowledgeChunk.tenant_id == claim.tenant_id,
            ))
            db.add_all([KnowledgeChunk(
                id=item.id, tenant_id=claim.tenant_id,
                index_revision_id=revision.id,
                knowledge_base_id=claim.knowledge_base_id,
                document_id=item.document_id, source_id=item.source_id,
                ordinal=item.ordinal, page_number=item.draft.page_number,
                section=item.draft.section, content=item.draft.content,
                content_hash=hashlib.sha256(item.draft.content.encode()).hexdigest(),
                term_frequencies=item.draft.terms, vector={},
                embedding_checksum=_embedding_checksum(vector),
                token_count=item.draft.token_count,
            ) for item, vector in zip(chunks, vectors)])
            now = utcnow()
            revision.status = "ready"
            revision.chunk_count = len(chunks)
            revision.vector_backend = "qdrant"
            revision.vector_collection = collection
            revision.vector_point_count = len(vectors)
            revision.error_code = None
            revision.error_detail = None
            revision.completed_at = now
            if knowledge_base.pending_index_revision_id == revision.id:
                knowledge_base.current_index_revision_id = revision.id
                knowledge_base.pending_index_revision_id = None
                knowledge_base.version += 1
            command.status = "completed"
            command.completed_at = now
            command.last_error = None
            command.dispatch_lease_owner = None
            command.dispatch_lease_token = None
            command.dispatch_lease_expires_at = None

    async def _fail(self, claim: KnowledgeIndexClaim, error: Exception) -> None:
        retryable = bool(getattr(error, "retryable", False))
        code = str(getattr(error, "code", type(error).__name__))[:100]
        detail = str(error)[:4000]
        async with AsyncSessionLocal.begin() as db:
            command = await db.scalar(select(OutboxCommand).where(
                OutboxCommand.id == claim.command_id,
                OutboxCommand.status == "claimed",
                OutboxCommand.dispatch_lease_token == claim.lease_token,
            ).with_for_update())
            revision = await db.scalar(select(KnowledgeIndexRevision).where(
                KnowledgeIndexRevision.id == claim.revision_id,
            ).with_for_update())
            if command is None or revision is None:
                return
            terminal = (
                not retryable
                or command.attempt_count >= settings.outbox_max_delivery_attempts
            )
            now = utcnow()
            revision.error_code = code
            revision.error_detail = detail
            if terminal:
                command.status = "dead"
                command.completed_at = now
                revision.status = "failed"
                revision.completed_at = now
                knowledge_base = await db.scalar(select(KnowledgeBase).where(
                    KnowledgeBase.id == claim.knowledge_base_id,
                    KnowledgeBase.tenant_id == claim.tenant_id,
                ).with_for_update())
                if knowledge_base is not None:
                    knowledge_base.version += 1
            else:
                command.status = "retry_wait"
                command.available_at = now + timedelta(
                    seconds=min(300, 2 ** min(command.attempt_count, 8)),
                )
                revision.status = "queued"
            command.last_error = f"{code}: {detail}"[:4000]
            command.dispatch_lease_owner = None
            command.dispatch_lease_token = None
            command.dispatch_lease_expires_at = None

    async def process(self, claim: KnowledgeIndexClaim) -> None:
        self._active_slots += 1
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_claim(claim, stop),
            name=f"knowledge-claim-heartbeat:{claim.command_id}",
        )
        try:
            chunks, vectors, collection = await self._build(claim)
            await self._complete(claim, chunks, vectors, collection)
        except Exception as error:
            logger.warning(
                "Semantic Knowledge indexing failed",
                extra={
                    "revision_id": claim.revision_id,
                    "error_code": getattr(error, "code", type(error).__name__),
                },
            )
            await self._fail(claim, error)
        finally:
            stop.set()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._active_slots -= 1

    async def run(self) -> None:
        tasks: set[asyncio.Task] = set()
        try:
            while True:
                finished = {task for task in tasks if task.done()}
                if finished:
                    await asyncio.gather(*finished, return_exceptions=True)
                    tasks -= finished
                claimed = False
                while len(tasks) < self.capacity:
                    claim = await self.claim_one()
                    if claim is None:
                        break
                    claimed = True
                    task = asyncio.create_task(
                        self.process(claim), name=f"knowledge-index:{claim.revision_id}",
                    )
                    tasks.add(task)
                if tasks and (len(tasks) >= self.capacity or not claimed):
                    await asyncio.wait(
                        tasks, timeout=settings.knowledge_worker_poll_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                elif not claimed:
                    await asyncio.sleep(settings.knowledge_worker_poll_seconds)
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


async def main() -> None:
    worker = KnowledgeIndexWorker()
    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(run_worker_heartbeat(
        worker_id=worker.worker_id, worker_kind="knowledge",
        capacity=worker.capacity, active_slots=lambda: worker._active_slots,
        stop=heartbeat_stop,
    ), name=f"worker-heartbeat:{worker.worker_id}")
    try:
        await worker.run()
    finally:
        heartbeat_stop.set()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
