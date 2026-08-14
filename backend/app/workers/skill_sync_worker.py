"""Durable consumer and cache warmer for immutable Skill packages.

Correctness does not depend on this worker: read_file also materializes the
exact RunnerSkillSnapshot version on demand.  This worker consumes publication
Outbox commands so caches are warm and commands do not remain pending forever.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.common import utcnow, uuid7
from app.models.foundation import OutboxCommand
from app.models.registry import RegisteredSkill, SkillVersion
from app.services.skill_package_service import InvalidSkillPackage, sync_package
from app.services.worker_heartbeat_service import run_worker_heartbeat


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillSyncClaim:
    command_id: str
    lease_token: str
    skill_id: str
    version_id: str
    checksum: str
    package_uri: str
    attempt_count: int


class SkillSyncWorker:
    def __init__(self, worker_id: str | None = None) -> None:
        self.worker_id = worker_id or (
            f"skill-sync:{socket.gethostname()}:{os.getpid()}:{uuid7()}"
        )
        self._active_slots = 0

    async def claim_one(self) -> SkillSyncClaim | None:
        async with AsyncSessionLocal.begin() as db:
            now = utcnow()
            command = await db.scalar(
                select(OutboxCommand).where(
                    OutboxCommand.topic == "runtime.maintenance",
                    OutboxCommand.action == "sync_skill",
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
                ).with_for_update(skip_locked=True).limit(1)
            )
            if command is None:
                return None
            version_id = str(command.payload_ref.get("version_id") or command.aggregate_id)
            skill_id = str(command.payload_ref.get("skill_id") or command.partition_key)
            version = await db.get(SkillVersion, version_id)
            if version is None or version.skill_id != skill_id:
                command.status = "dead"
                command.last_error = "SkillVersion referenced by sync command is missing"
                command.completed_at = now
                return None
            token = uuid7()
            command.status = "claimed"
            command.attempt_count += 1
            command.dispatch_lease_owner = self.worker_id
            command.dispatch_lease_token = token
            command.dispatch_lease_expires_at = now + timedelta(
                seconds=settings.skill_sync_claim_lease_seconds,
            )
            return SkillSyncClaim(
                command_id=command.id,
                lease_token=token,
                skill_id=skill_id,
                version_id=version.id,
                checksum=version.checksum,
                package_uri=version.package_uri,
                attempt_count=command.attempt_count,
            )

    async def _settle(self, claim: SkillSyncClaim, error: Exception | None) -> None:
        async with AsyncSessionLocal.begin() as db:
            command = await db.scalar(select(OutboxCommand).where(
                OutboxCommand.id == claim.command_id,
                OutboxCommand.status == "claimed",
                OutboxCommand.dispatch_lease_token == claim.lease_token,
            ).with_for_update())
            if command is None:
                return
            now = utcnow()
            if error is None:
                command.status = "completed"
                command.completed_at = now
                command.last_error = None
            elif command.attempt_count >= settings.outbox_max_delivery_attempts:
                command.status = "dead"
                command.completed_at = now
                command.last_error = str(error)[:4000]
            else:
                command.status = "retry_wait"
                command.available_at = now + timedelta(
                    seconds=min(300, 2 ** min(command.attempt_count, 8)),
                )
                command.last_error = str(error)[:4000]
            command.dispatch_lease_owner = None
            command.dispatch_lease_token = None
            command.dispatch_lease_expires_at = None

    async def run_once(self) -> bool:
        claim = await self.claim_one()
        if claim is None:
            return False
        self._active_slots = 1
        error = None
        try:
            await asyncio.to_thread(
                sync_package, claim.skill_id, claim.checksum, claim.package_uri,
            )
        except Exception as caught:  # durable retry boundary
            error = caught
            logger.warning(
                "Skill cache warm failed",
                extra={"skill_id": claim.skill_id, "version_id": claim.version_id},
            )
        try:
            await self._settle(claim, error)
        finally:
            self._active_slots = 0
        return True

    async def warm_current_versions(self) -> None:
        self._active_slots = 1
        try:
            async with AsyncSessionLocal() as db:
                versions = list(await db.scalars(
                    select(SkillVersion).join(
                        RegisteredSkill,
                        RegisteredSkill.current_published_version_id == SkillVersion.id,
                    ).where(
                        RegisteredSkill.status == "active",
                        SkillVersion.status == "published",
                    )
                ))
            for version in versions:
                try:
                    await asyncio.to_thread(
                        sync_package,
                        version.skill_id,
                        version.checksum,
                        version.package_uri,
                    )
                except InvalidSkillPackage:
                    logger.exception(
                        "Startup Skill cache warm failed",
                        extra={"skill_id": version.skill_id, "version_id": version.id},
                    )
        finally:
            self._active_slots = 0


async def main() -> None:
    worker = SkillSyncWorker()
    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(run_worker_heartbeat(
        worker_id=worker.worker_id, worker_kind="skill_sync", capacity=1,
        active_slots=lambda: worker._active_slots, stop=heartbeat_stop,
    ), name=f"worker-heartbeat:{worker.worker_id}")
    try:
        await worker.warm_current_versions()
        loop = asyncio.get_running_loop()
        next_reconcile = loop.time() + settings.skill_sync_reconcile_seconds
        while True:
            if loop.time() >= next_reconcile:
                try:
                    await worker.warm_current_versions()
                except Exception:
                    logger.exception("Periodic Skill cache reconciliation failed")
                finally:
                    next_reconcile = loop.time() + settings.skill_sync_reconcile_seconds
            try:
                worked = await worker.run_once()
            except Exception:
                logger.exception("Skill sync worker iteration failed")
                worked = False
            if not worked:
                await asyncio.sleep(settings.skill_sync_worker_poll_seconds)
    finally:
        heartbeat_stop.set()
        await asyncio.gather(heartbeat, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
