"""Qdrant-backed, rebuildable semantic vector projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.services.sparse_retrieval_service import SparseVector


class VectorStoreError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class VectorPoint:
    id: str
    vector: tuple[float, ...]
    payload: dict[str, Any]
    bm25: SparseVector | None = None
    lexicon: SparseVector | None = None


@dataclass(frozen=True)
class VectorMatch:
    id: str
    score: float


def collection_name(*, dimensions: int, model_content_hash: str) -> str:
    return (
        f"{settings.knowledge_qdrant_collection_prefix}_"
        f"d{dimensions}_{model_content_hash[:12]}"
    )


def hybrid_collection_name(*, dimensions: int, model_content_hash: str) -> str:
    return (
        f"{settings.knowledge_qdrant_collection_prefix}_hybrid_v1_"
        f"d{dimensions}_{model_content_hash[:12]}"
    )


class QdrantVectorStore:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owned = client is None
        headers = (
            {"api-key": settings.knowledge_qdrant_api_key}
            if settings.knowledge_qdrant_api_key else None
        )
        self._client = client or httpx.AsyncClient(
            base_url=settings.knowledge_qdrant_url,
            headers=headers,
            timeout=settings.knowledge_qdrant_timeout_seconds,
            # Qdrant is an internal platform dependency. Never route its
            # cluster-local address through inherited HTTP(S)_PROXY settings.
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise VectorStoreError(
                "vector_store_unavailable", "Qdrant is unavailable", retryable=True,
            ) from error
        if response.status_code >= 400:
            retryable = response.status_code in {408, 409, 425, 429} or response.status_code >= 500
            raise VectorStoreError(
                "vector_store_rejected",
                f"Qdrant returned HTTP {response.status_code}",
                retryable=retryable,
            )
        return response

    async def health(self) -> bool:
        await self._request("GET", "/healthz")
        return True

    async def ensure_collection(self, *, name: str, dimensions: int) -> None:
        try:
            response = await self._client.get(f"/collections/{name}")
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise VectorStoreError(
                "vector_store_unavailable", "Qdrant is unavailable", retryable=True,
            ) from error
        created = response.status_code == 404
        if created:
            try:
                await self._request(
                    "PUT", f"/collections/{name}",
                    json={"vectors": {"size": dimensions, "distance": "Cosine"}},
                )
            except VectorStoreError as error:
                # Multiple index workers may race to create the same immutable
                # model/dimension collection. Re-read and validate the winner.
                if error.code != "vector_store_rejected" or not error.retryable:
                    raise
            response = await self._request("GET", f"/collections/{name}")
        elif response.status_code >= 400:
            raise VectorStoreError(
                "vector_store_rejected", f"Qdrant returned HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )
        try:
            configured = int(response.json()["result"]["config"]["params"]["vectors"]["size"])
        except (KeyError, TypeError, ValueError) as error:
            raise VectorStoreError(
                "vector_collection_invalid", "Qdrant collection metadata is invalid",
                retryable=False,
            ) from error
        if configured != dimensions:
            raise VectorStoreError(
                "vector_dimensions_mismatch",
                f"Qdrant collection dimension {configured} does not match {dimensions}",
                retryable=False,
            )
        # Payload-index creation is idempotent. Reconcile it on every build so
        # a process crash after collection creation cannot leave a permanently
        # unindexed filter path.
        for field in ("tenant_id", "knowledge_base_id", "index_revision_id"):
            await self._request(
                "PUT", f"/collections/{name}/index?wait=true",
                json={"field_name": field, "field_schema": "keyword"},
            )

    async def ensure_hybrid_collection(self, *, name: str, dimensions: int) -> None:
        try:
            response = await self._client.get(f"/collections/{name}")
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise VectorStoreError(
                "vector_store_unavailable", "Qdrant is unavailable", retryable=True,
            ) from error
        if response.status_code == 404:
            try:
                await self._request(
                    "PUT", f"/collections/{name}",
                    json={
                        "vectors": {
                            "dense": {"size": dimensions, "distance": "Cosine"},
                        },
                        "sparse_vectors": {
                            "bm25": {},
                            "lexicon": {},
                        },
                    },
                )
            except VectorStoreError as error:
                if error.code != "vector_store_rejected" or not error.retryable:
                    raise
            response = await self._request("GET", f"/collections/{name}")
        elif response.status_code >= 400:
            raise VectorStoreError(
                "vector_store_rejected", f"Qdrant returned HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )
        try:
            params = response.json()["result"]["config"]["params"]
            configured = int(params["vectors"]["dense"]["size"])
            sparse_vectors = params["sparse_vectors"]
            bm25_config = sparse_vectors["bm25"]
            lexicon_config = sparse_vectors["lexicon"]
        except (KeyError, TypeError, ValueError) as error:
            raise VectorStoreError(
                "vector_collection_invalid",
                "Qdrant hybrid collection metadata is invalid",
                retryable=False,
            ) from error
        if configured != dimensions:
            raise VectorStoreError(
                "vector_dimensions_mismatch",
                f"Qdrant collection dimension {configured} does not match {dimensions}",
                retryable=False,
            )
        if not isinstance(bm25_config, dict) or bm25_config.get("modifier") is not None:
            raise VectorStoreError(
                "vector_collection_profile_mismatch",
                "Qdrant BM25 must use revision-frozen IDF, not collection IDF",
                retryable=False,
            )
        if not isinstance(lexicon_config, dict):
            raise VectorStoreError(
                "vector_collection_profile_mismatch",
                "Qdrant sparse vector profile does not match MyClaw hybrid v1",
                retryable=False,
            )
        for field in ("tenant_id", "knowledge_base_id", "index_revision_id"):
            await self._request(
                "PUT", f"/collections/{name}/index?wait=true",
                json={"field_name": field, "field_schema": "keyword"},
            )

    async def upsert(self, *, collection: str, points: list[VectorPoint]) -> None:
        if not points:
            return
        await self._request(
            "PUT", f"/collections/{collection}/points?wait=true",
            json={"points": [{
                "id": item.id,
                "vector": (
                    {
                        "dense": list(item.vector),
                        "bm25": {
                            "indices": list(item.bm25.indices),
                            "values": list(item.bm25.values),
                        },
                        "lexicon": {
                            "indices": list(item.lexicon.indices),
                            "values": list(item.lexicon.values),
                        },
                    }
                    if item.bm25 is not None and item.lexicon is not None
                    else list(item.vector)
                ),
                "payload": item.payload,
            } for item in points]},
        )

    async def query(
        self, *, collection: str, vector: tuple[float, ...], tenant_id: str,
        revision_ids: list[str], limit: int,
    ) -> list[VectorMatch]:
        if not revision_ids:
            return []
        response = await self._request(
            "POST", f"/collections/{collection}/points/query",
            json={
                "query": list(vector),
                "filter": {"must": [
                    {"key": "tenant_id", "match": {"value": tenant_id}},
                    {"key": "index_revision_id", "match": {"any": revision_ids}},
                ]},
                "limit": limit,
                "with_payload": False,
                "with_vector": False,
            },
        )
        try:
            rows = response.json()["result"]["points"]
            return [VectorMatch(id=str(item["id"]), score=float(item["score"])) for item in rows]
        except (KeyError, TypeError, ValueError) as error:
            raise VectorStoreError(
                "vector_query_response_invalid", "Qdrant query response is invalid",
                retryable=False,
            ) from error

    @staticmethod
    def _filter(*, tenant_id: str, revision_ids: list[str]) -> dict:
        return {"must": [
            {"key": "tenant_id", "match": {"value": tenant_id}},
            {"key": "index_revision_id", "match": {"any": revision_ids}},
        ]}

    async def query_dense(
        self, *, collection: str, vector: tuple[float, ...], tenant_id: str,
        revision_ids: list[str], limit: int,
    ) -> list[VectorMatch]:
        return await self._query_named(
            collection=collection, query=list(vector), using="dense",
            tenant_id=tenant_id, revision_ids=revision_ids, limit=limit,
        )

    async def query_sparse(
        self, *, collection: str, vector: SparseVector, using: str,
        tenant_id: str, revision_ids: list[str], limit: int,
    ) -> list[VectorMatch]:
        if using not in {"bm25", "lexicon"}:
            raise ValueError("Unsupported sparse vector route")
        if not vector.indices or not revision_ids:
            return []
        return await self._query_named(
            collection=collection,
            query={"indices": list(vector.indices), "values": list(vector.values)},
            using=using, tenant_id=tenant_id,
            revision_ids=revision_ids, limit=limit,
        )

    async def _query_named(
        self, *, collection: str, query: list[float] | dict[str, list],
        using: str, tenant_id: str, revision_ids: list[str], limit: int,
    ) -> list[VectorMatch]:
        if not revision_ids:
            return []
        response = await self._request(
            "POST", f"/collections/{collection}/points/query",
            json={
                "query": query,
                "using": using,
                "filter": self._filter(
                    tenant_id=tenant_id, revision_ids=revision_ids,
                ),
                "limit": limit,
                "with_payload": False,
                "with_vector": False,
            },
        )
        try:
            rows = response.json()["result"]["points"]
            return [
                VectorMatch(id=str(item["id"]), score=float(item["score"]))
                for item in rows
            ]
        except (KeyError, TypeError, ValueError) as error:
            raise VectorStoreError(
                "vector_query_response_invalid", "Qdrant query response is invalid",
                retryable=False,
            ) from error
