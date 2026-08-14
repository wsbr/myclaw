"""Provider-neutral semantic Embedding client for immutable ModelVersions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import httpx

from app.models.registry import ModelVersion


class EmbeddingProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class EmbeddingSpec:
    provider: str
    endpoint: str
    model_name: str
    dimensions: int
    max_batch_size: int
    model_version_id: str
    model_content_hash: str
    parameters: dict[str, Any]

    def snapshot(self) -> dict[str, Any]:
        return {
            "profile": "semantic.openai_compatible.v1",
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model_name": self.model_name,
            "dimensions": self.dimensions,
            "max_batch_size": self.max_batch_size,
            "model_version_id": self.model_version_id,
            "model_content_hash": self.model_content_hash,
            "external_service": True,
        }


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int | None


def is_embedding_model(version: ModelVersion) -> bool:
    return (version.capabilities or {}).get("model_kind") == "embedding"


def embedding_spec(version: ModelVersion) -> EmbeddingSpec:
    if not is_embedding_model(version):
        raise EmbeddingProviderError(
            "embedding_model_kind_invalid",
            "The selected ModelVersion is not an embedding model",
            retryable=False,
        )
    if version.provider not in {"openai", "openai_compatible"}:
        raise EmbeddingProviderError(
            "embedding_provider_unsupported",
            f"Embedding provider {version.provider} is unsupported",
            retryable=False,
        )
    if not version.endpoint:
        raise EmbeddingProviderError(
            "embedding_endpoint_missing", "Embedding endpoint is missing", retryable=False,
        )
    capabilities = dict(version.capabilities or {})
    try:
        dimensions = int(capabilities["embedding_dimensions"])
        max_batch_size = int(capabilities.get("embedding_max_batch_size", 10))
    except (KeyError, TypeError, ValueError) as error:
        raise EmbeddingProviderError(
            "embedding_capabilities_invalid",
            "Frozen Embedding capabilities are invalid",
            retryable=False,
        ) from error
    if not 8 <= dimensions <= 4096 or not 1 <= max_batch_size <= 2048:
        raise EmbeddingProviderError(
            "embedding_capabilities_invalid",
            "Frozen Embedding capabilities are outside supported bounds",
            retryable=False,
        )
    parameters = dict(version.default_parameters or {})
    if parameters.get("dimensions") not in {None, dimensions}:
        raise EmbeddingProviderError(
            "embedding_dimensions_conflict",
            "Frozen Embedding dimensions do not match the declared capability",
            retryable=False,
        )
    return EmbeddingSpec(
        provider=version.provider,
        endpoint=version.endpoint.rstrip("/"),
        model_name=version.model_name,
        dimensions=dimensions,
        max_batch_size=max_batch_size,
        model_version_id=version.id,
        model_content_hash=version.content_hash,
        parameters=parameters,
    )


class OpenAICompatibleEmbeddingClient:
    def __init__(self, *, timeout_seconds: float, client: httpx.AsyncClient | None = None) -> None:
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def embed(
        self, *, spec: EmbeddingSpec, api_key: str, texts: list[str],
    ) -> EmbeddingBatch:
        if not texts or len(texts) > spec.max_batch_size:
            raise EmbeddingProviderError(
                "embedding_batch_invalid",
                f"Embedding batch must contain 1..{spec.max_batch_size} inputs",
                retryable=False,
            )
        payload: dict[str, Any] = {
            "model": spec.model_name,
            "input": texts,
            "dimensions": spec.dimensions,
            "encoding_format": "float",
        }
        try:
            response = await self._client.post(
                f"{spec.endpoint}/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise EmbeddingProviderError(
                "embedding_transport_unavailable", "Embedding provider is unavailable",
                retryable=True,
            ) from error
        if response.status_code >= 400:
            retryable = response.status_code in {408, 409, 425, 429} or response.status_code >= 500
            raise EmbeddingProviderError(
                "embedding_provider_rejected",
                f"Embedding provider returned HTTP {response.status_code}",
                retryable=retryable,
            )
        try:
            body = response.json()
            rows = sorted(body["data"], key=lambda item: int(item["index"]))
            if [int(item["index"]) for item in rows] != list(range(len(texts))):
                raise ValueError("embedding result indices are invalid")
            vectors = tuple(tuple(float(value) for value in item["embedding"]) for item in rows)
        except (KeyError, TypeError, ValueError) as error:
            raise EmbeddingProviderError(
                "embedding_response_invalid", "Embedding response schema is invalid",
                retryable=False,
            ) from error
        if len(vectors) != len(texts):
            raise EmbeddingProviderError(
                "embedding_result_count_mismatch", "Embedding result count is invalid",
                retryable=False,
            )
        for vector in vectors:
            if (
                len(vector) != spec.dimensions
                or not all(math.isfinite(value) for value in vector)
                or not any(value != 0.0 for value in vector)
            ):
                raise EmbeddingProviderError(
                    "embedding_vector_invalid", "Embedding vector shape or values are invalid",
                    retryable=False,
                )
        usage = body.get("usage") or {}
        input_tokens = usage.get("prompt_tokens", usage.get("total_tokens"))
        try:
            normalized_input_tokens = (
                int(input_tokens) if input_tokens is not None else None
            )
        except (TypeError, ValueError) as error:
            raise EmbeddingProviderError(
                "embedding_response_invalid", "Embedding usage metadata is invalid",
                retryable=False,
            ) from error
        return EmbeddingBatch(
            vectors=vectors,
            input_tokens=normalized_input_tokens,
        )
