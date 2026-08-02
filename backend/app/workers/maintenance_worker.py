"""Single-leader periodic retention maintenance process."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from app.config import settings
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import engine
from app.models.common import uuid7
from app.services.retention_service import run_retention_cleanup


logger = logging.getLogger(__name__)
_LOCK_NAME = "myclaw.retention_cleanup"


async def run_once() -> bool:
    async with engine.connect() as connection:
        mysql = connection.dialect.name == "mysql"
        acquired = True
        if mysql:
            acquired = bool(await connection.scalar(
                text("SELECT GET_LOCK(:name, 0)"), {"name": _LOCK_NAME},
            ))
            await connection.commit()
        if not acquired:
            return False
        try:
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                async with db.begin():
                    result = await run_retention_cleanup(db, job_id=uuid7())
            logger.info("retention cleanup completed", extra={"deleted_counts": result.__dict__})
            return True
        finally:
            if mysql:
                await connection.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": _LOCK_NAME})
                await connection.commit()


async def main() -> None:
    while True:
        try:
            await run_once()
        except Exception:
            logger.exception("retention cleanup failed")
        await asyncio.sleep(settings.maintenance_poll_seconds)


if __name__ == "__main__":
    asyncio.run(main())
