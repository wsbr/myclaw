from __future__ import annotations

import asyncio

import pytest

from app.workers.runtime_worker import CommandClaim, RunnerFence, RuntimeWorker


pytestmark = pytest.mark.unit


class _BeginContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _SessionFactory:
    def begin(self):
        return _BeginContext()


class _ProviderPool:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def _eventually(predicate, *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_runtime_worker_claims_only_up_to_bounded_concurrency(monkeypatch):
    provider_pool = _ProviderPool()
    worker = RuntimeWorker(
        worker_id="bounded-worker", max_concurrency=2,
        provider_pool=provider_pool,
    )
    claims = [CommandClaim(f"command-{index}", f"token-{index}", f"runner-{index}")
              for index in range(3)]
    gates = {claim.runner_id: asyncio.Event() for claim in claims}
    started: list[str] = []

    async def no_control(*args, **kwargs):
        return False

    async def claim_one():
        return claims.pop(0) if claims else None

    async def acquire_runner(claim):
        return RunnerFence(
            claim.runner_id, f"work-{claim.runner_id}", f"run-{claim.runner_id}",
            f"session-{claim.runner_id}", 1, f"lease-{claim.runner_id}",
        )

    async def execute(fence):
        started.append(fence.runner_id)
        await gates[fence.runner_id].wait()

    monkeypatch.setattr(
        "app.workers.runtime_worker.AsyncSessionLocal", _SessionFactory(),
    )
    monkeypatch.setattr(
        "app.workers.runtime_worker.expire_one_run_deadline", no_control,
    )
    monkeypatch.setattr(worker, "recover_one_expired_runner", no_control)
    monkeypatch.setattr(worker, "process_one_group_control", no_control)
    monkeypatch.setattr(worker, "claim_one", claim_one)
    monkeypatch.setattr(worker, "acquire_runner", acquire_runner)
    monkeypatch.setattr(worker, "execute", execute)

    loop = asyncio.create_task(worker.run_forever(poll_seconds=0.001))
    try:
        await _eventually(lambda: len(started) == 2)
        assert set(started) == {"runner-0", "runner-1"}
        assert len(claims) == 1

        gates["runner-0"].set()
        await _eventually(lambda: len(started) == 3)
        assert started[-1] == "runner-2"
    finally:
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop

    assert provider_pool.closed is True
