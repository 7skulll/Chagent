"""Durable queue for automatic memory extraction.

The run's completion transaction only inserts a job; the model call happens in
a bounded worker so an unavailable or slow provider cannot delay or fail the
task the user is waiting for.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from chagent.storage.database import Database


@dataclass(frozen=True, slots=True)
class MemoryJob:
    id: UUID
    run_id: UUID
    session_id: UUID
    source_message_id: UUID
    attempts: int
    max_attempts: int


class MemoryJobRepository:
    def __init__(self, database: Database):
        self.database = database

    @asynccontextmanager
    async def _tx(self, connection):
        if connection is not None:
            yield connection
            return
        async with self.database.transaction() as owned:
            yield owned

    async def enqueue(self, connection, *, session_id: UUID, run_id: UUID,
                      source_message_id: UUID, prompt_version: str,
                      max_attempts: int = 3) -> UUID | None:
        """Enqueue inside the caller's transaction; one job per run."""
        identity = uuid4()
        cursor = await connection.execute(
            "INSERT INTO memory_jobs (id, run_id, session_id, source_message_id, prompt_version, max_attempts, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'queued') ON CONFLICT (run_id) DO NOTHING RETURNING id",
            (identity, run_id, session_id, source_message_id, prompt_version, max_attempts),
        )
        row = await cursor.fetchone()
        return row["id"] if row else None

    async def claim(self, *, lease_seconds: float) -> MemoryJob | None:
        """Take one queued job, or one whose worker lease expired.

        SKIP LOCKED keeps two workers from claiming the same row without
        blocking on a job another process is already extracting.
        """
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "WITH candidate AS ("
                "  SELECT id FROM memory_jobs"
                "  WHERE (status = 'queued' AND available_at <= now())"
                "     OR (status = 'running' AND lease_expires_at < now())"
                "  ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"
                ") "
                "UPDATE memory_jobs j SET status = 'running', "
                "  started_at = COALESCE(j.started_at, now()), "
                "  lease_expires_at = now() + make_interval(secs => %s) "
                "FROM candidate WHERE j.id = candidate.id "
                "RETURNING j.id, j.run_id, j.session_id, j.source_message_id, j.attempts, j.max_attempts",
                (lease_seconds,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return MemoryJob(row["id"], row["run_id"], row["session_id"], row["source_message_id"],
                         row["attempts"], row["max_attempts"])

    async def requeue(self, job_id: UUID, *, connection=None, reason: str | None = None) -> None:
        """Return a claimed job to the queue without consuming an attempt."""
        async with self._tx(connection) as conn:
            await conn.execute(
                "UPDATE memory_jobs SET status = 'queued', lease_expires_at = NULL, available_at = now(), "
                "last_error = COALESCE(%s, last_error) WHERE id = %s AND status = 'running'",
                (reason, job_id),
            )

    async def complete(self, job_id: UUID, *, connection=None) -> None:
        async with self._tx(connection) as conn:
            await conn.execute(
                "UPDATE memory_jobs SET status = 'completed', lease_expires_at = NULL, ended_at = now() "
                "WHERE id = %s AND status = 'running'", (job_id,),
            )

    async def fail(self, job_id: UUID, *, error: str, retry_delay: float = 5.0, connection=None) -> str:
        """Record a failure; retry until max_attempts, then stop.

        Returns the resulting status so callers can report it without guessing.
        """
        async with self._tx(connection) as conn:
            cursor = await conn.execute(
                "UPDATE memory_jobs SET attempts = attempts + 1, last_error = %s, "
                "  status = CASE WHEN attempts + 1 >= max_attempts THEN 'failed' ELSE 'queued' END, "
                "  available_at = CASE WHEN attempts + 1 >= max_attempts THEN available_at "
                "                       ELSE now() + make_interval(secs => %s) END, "
                "  lease_expires_at = NULL, "
                "  ended_at = CASE WHEN attempts + 1 >= max_attempts THEN now() ELSE NULL END "
                "WHERE id = %s AND status = 'running' RETURNING status",
                (error, retry_delay, job_id),
            )
            row = await cursor.fetchone()
        return row["status"] if row else "unknown"

    async def pending(self) -> int:
        """Jobs that can make progress now (a retry wait is not pending)."""
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT count(*) AS remaining FROM memory_jobs "
                "WHERE (status = 'queued' AND available_at <= now()) OR status = 'running'")
            return (await cursor.fetchone())["remaining"]

    async def unfinished(self) -> int:
        """Jobs that are not terminal, including retries waiting for their delay."""
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT count(*) AS remaining FROM memory_jobs WHERE status IN ('queued', 'running')")
            return (await cursor.fetchone())["remaining"]

    async def get(self, job_id: UUID):
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM memory_jobs WHERE id = %s", (job_id,))
            return await cursor.fetchone()

    async def for_run(self, run_id: UUID):
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM memory_jobs WHERE run_id = %s", (run_id,))
            return await cursor.fetchone()
