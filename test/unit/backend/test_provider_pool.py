from __future__ import annotations

import pytest

from app.core.model_hub.registry import ModelHub, ProviderPool


pytestmark = pytest.mark.unit


class _Client:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _Provider:
    def __init__(self) -> None:
        self.client = _Client()


@pytest.mark.asyncio
async def test_provider_pool_reuses_clients_and_never_evicts_active_lease(monkeypatch):
    created: list[_Provider] = []

    def create(*args, **kwargs):
        provider = _Provider()
        created.append(provider)
        return provider

    monkeypatch.setattr(ModelHub, "get", staticmethod(create))
    pool = ProviderPool(max_entries=1)

    first = await pool.acquire({}, "openai", "secret-a", "https://models.test/v1")
    same = await pool.acquire({}, "openai", "secret-a", "https://models.test/v1/")
    second = await pool.acquire({}, "openai", "secret-b", "https://models.test/v1")

    assert first.provider is same.provider
    assert second.provider is not first.provider
    assert len(created) == 2
    assert created[0].client.close_calls == 0

    await first.release()
    assert created[0].client.close_calls == 0
    await same.release()
    assert created[0].client.close_calls == 1

    await second.release()
    await pool.aclose()
    assert created[1].client.close_calls == 1
