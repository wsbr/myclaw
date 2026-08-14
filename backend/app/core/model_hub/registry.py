from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections import OrderedDict
from dataclasses import dataclass

from app.core.model_hub.base import BaseLLM
from app.core.model_hub.anthropic import AnthropicLLM
from app.core.model_hub.openai import OpenAILLM

_OPENAI_COMPATIBLE = {"openai", "openai_compatible", "azure_openai", "ollama"}


class ModelHub:
    @staticmethod
    def get(model_config: dict, provider_type: str, api_key: str, base_url: str | None = None) -> BaseLLM:
        if provider_type == "anthropic":
            return AnthropicLLM(api_key=api_key, base_url=base_url)
        elif provider_type in _OPENAI_COMPATIBLE:
            return OpenAILLM(
                api_key=api_key,
                base_url=base_url,
                tool_strict=provider_type == "openai",
            )
        raise ValueError(f"Unsupported provider: {provider_type}")


async def _close_provider(provider: BaseLLM) -> None:
    """Close a provider SDK client without depending on one SDK's spelling."""

    client = getattr(provider, "client", None)
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close is None:
        return
    returned = close()
    if inspect.isawaitable(returned):
        await returned


@dataclass
class _ProviderEntry:
    provider: BaseLLM
    borrowers: int = 0


class ProviderLease:
    """A ref-counted lease for a shared async Provider client."""

    def __init__(
        self, pool: "ProviderPool", key: tuple[str, str, str], provider: BaseLLM,
    ) -> None:
        self._pool = pool
        self._key = key
        self.provider = provider
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._pool._release(self._key)


class ProviderPool:
    """Process-local LRU of SDK clients and their HTTP connection pools.

    Entries in active use are never evicted. A temporary burst of distinct
    credentials may exceed ``max_entries``; idle entries are retired as soon
    as their final lease is released.
    """

    def __init__(self, *, max_entries: int = 32) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str, str], _ProviderEntry] = (
            OrderedDict()
        )
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _key(
        provider_type: str, api_key: str, base_url: str | None,
    ) -> tuple[str, str, str]:
        # The SDK client necessarily retains its credential, but the cache key
        # must not duplicate or accidentally expose the raw secret.
        credential_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
        return (
            provider_type.casefold(),
            (base_url or "").rstrip("/"),
            credential_fingerprint,
        )

    async def acquire(
        self,
        model_config: dict,
        provider_type: str,
        api_key: str,
        base_url: str | None = None,
    ) -> ProviderLease:
        key = self._key(provider_type, api_key, base_url)
        retired: list[BaseLLM] = []
        async with self._lock:
            if self._closed:
                raise RuntimeError("ProviderPool is closed")
            entry = self._entries.get(key)
            if entry is None:
                entry = _ProviderEntry(ModelHub.get(
                    model_config, provider_type, api_key, base_url,
                ))
                self._entries[key] = entry
            else:
                self._entries.move_to_end(key)
            entry.borrowers += 1
            retired = self._retire_idle_locked()
            lease = ProviderLease(self, key, entry.provider)
        for provider in retired:
            await _close_provider(provider)
        return lease

    def _retire_idle_locked(self) -> list[BaseLLM]:
        retired: list[BaseLLM] = []
        if len(self._entries) <= self.max_entries:
            return retired
        for key, entry in list(self._entries.items()):
            if len(self._entries) <= self.max_entries:
                break
            if entry.borrowers:
                continue
            retired.append(self._entries.pop(key).provider)
        return retired

    async def _release(self, key: tuple[str, str, str]) -> None:
        retired: list[BaseLLM] = []
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            if entry.borrowers < 1:
                raise RuntimeError("Provider lease released without a borrower")
            entry.borrowers -= 1
            retired = self._retire_idle_locked()
        for provider in retired:
            await _close_provider(provider)

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            providers = [entry.provider for entry in self._entries.values()]
            self._entries.clear()
        for provider in providers:
            await _close_provider(provider)
