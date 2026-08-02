"""A small explicit async unit of work.

Services receive one session and never commit piecemeal. Domain state, its event
or audit record, and any outbox command therefore share the same transaction.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import AsyncSessionLocal


class UnitOfWork:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    ) -> None:
        self._session_factory = session_factory
        self.session: AsyncSession

    async def __aenter__(self) -> Self:
        self.session = self._session_factory()
        await self.session.begin()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                await self.session.commit()
            else:
                await self.session.rollback()
        finally:
            await self.session.close()

    async def flush(self) -> None:
        await self.session.flush()
