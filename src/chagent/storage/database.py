"""Async PostgreSQL connection lifecycle and explicit transaction boundaries."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Self

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from chagent.config import Settings


class Database:
    """One pool per application instance, opened in its owning event loop.

    Connections use autocommit for standalone queries. Use transaction() for
    atomic work and pass its connection through all participating operations.
    """

    def __init__(self, settings: Settings) -> None:
        self._timeout = settings.db_timeout
        self._pool = AsyncConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            timeout=settings.db_timeout,
            open=False,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "connect_timeout": max(1, int(settings.db_timeout)),
                "application_name": "chagent",
            },
        )

    async def connect(self) -> None:
        """Open the pool and fail promptly if no connection can be obtained."""
        await self._pool.open()
        try:
            await self.check()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection[DictRow]]:
        async with self._pool.connection(timeout=self._timeout) as connection:
            yield connection

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection[DictRow]]:
        """Commit on success; rollback on exceptions or cancellation."""
        async with self.connection() as connection:
            async with connection.transaction():
                yield connection

    async def check(self) -> None:
        async with self.connection() as connection:
            await connection.execute("SELECT 1")

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
