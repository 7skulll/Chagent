"""Bounded background worker for queued memory extraction.

Extraction is a second model call plus a validation pass. Running it inside
``AgentRunner.run`` meant a slow or failing provider could delay — or with the
run timeout, fail — an answer that was already saved. The worker keeps that
work outside the run, at the cost of memories becoming visible slightly later.
"""

import asyncio
import logging

from chagent.memory.service import MemoryService
from chagent.storage.memory_jobs import MemoryJob, MemoryJobRepository


logger = logging.getLogger(__name__)


class MemoryWorker:
    """Claims jobs with a lease, extracts, and commits candidates atomically.

    A lease makes an interrupted worker recoverable: once it expires another
    process reaps the job — counting the dead attempt — and may claim it again.
    Extraction itself is never replayed after a candidate was committed,
    because candidates and the job's terminal state commit together.
    """

    def __init__(self, jobs: MemoryJobRepository, service: MemoryService, agent, *,
                 concurrency: int = 1, poll_interval: float = 1.0,
                 lease_seconds: float = 120.0, retry_delay: float = 5.0):
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 4:
            raise ValueError("Memory worker concurrency must be an integer between 1 and 4")
        if not 0 < poll_interval <= 60 or not 1 <= lease_seconds <= 3600 or retry_delay < 0:
            raise ValueError("Invalid memory worker timing")
        self.jobs, self.service, self.agent = jobs, service, agent
        self.concurrency, self.poll_interval = concurrency, poll_interval
        self.lease_seconds, self.retry_delay = lease_seconds, retry_delay
        self._task: asyncio.Task | None = None
        self._wakeup = asyncio.Event()
        self._closed = False

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._closed = False
        self._task = asyncio.create_task(self._loop(), name="memory-worker")

    async def stop(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def wake(self) -> None:
        """Signal that a job was just enqueued; never blocks the caller."""
        self._wakeup.set()

    # -- processing -----------------------------------------------------

    async def run_once(self) -> int:
        """Reap dead leases, then claim up to ``concurrency`` jobs and process them."""
        for expired in await self.jobs.reap_expired():
            if expired["status"] == "failed":
                await self._quietly(self.service.runs.event, expired["run_id"], "memory_job_failed",
                                    {"job_id": str(expired["id"]), "reason": "LeaseExpired",
                                     "status": "failed", "attempt": expired["attempts"],
                                     "max_attempts": expired["max_attempts"]})
        claimed: list[MemoryJob] = []
        for _ in range(self.concurrency):
            job = await self.jobs.claim(lease_seconds=self.lease_seconds)
            if job is None:
                break
            claimed.append(job)
        if not claimed:
            return 0
        await asyncio.gather(*(self._process(job) for job in claimed), return_exceptions=True)
        return len(claimed)

    async def drain(self, timeout: float = 30.0) -> int:
        """Process everything that can run now; used by one-shot CLI runs and tests.

        A job waiting out its retry backoff is not "runnable now", so it is
        returned to the queue rather than waited for. Callers that report the
        outcome must check ``jobs.unfinished()`` instead of assuming success.
        """
        processed = 0
        async with asyncio.timeout(timeout):
            while True:
                count = await self.run_once()
                if count:
                    processed += count
                    continue
                if await self.jobs.pending() == 0:
                    return processed
                await asyncio.sleep(0.05)

    async def _process(self, job: MemoryJob) -> None:
        try:
            proposals = await self.service.run_job(job, self.agent.model, self.agent.client)
            # Candidates and the completed job share one transaction: a job is
            # never marked done while its candidates are missing, and a retry
            # never duplicates them.
            async with self.service.repository.database.transaction() as connection:
                if proposals:
                    await self.service.commit(connection, job.session_id, job.run_id, proposals)
                await self.jobs.complete(job.id, connection=connection)
            self.service.runs.notifications.notify(job.run_id)
        except asyncio.CancelledError:
            await self._quietly(self.jobs.requeue, job.id, reason="worker_stopped")
            raise
        except Exception as error:
            reason = type(error).__name__
            status = await self._quietly(self.jobs.fail, job.id, error=reason, retry_delay=self.retry_delay)
            await self._quietly(self.service.runs.event, job.run_id, "memory_job_failed",
                                {"job_id": str(job.id), "reason": reason, "status": status,
                                 "attempt": job.attempts + 1, "max_attempts": job.max_attempts})

    @staticmethod
    async def _quietly(action, *args, **kwargs):
        """Best-effort bookkeeping: it must not mask the original outcome."""
        try:
            return await action(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Memory job bookkeeping failed (%s)", type(error).__name__)
            return None

    async def _loop(self) -> None:
        while not self._closed:
            try:
                if await self.run_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Memory worker poll failed (%s)", type(error).__name__)
            try:
                await asyncio.wait_for(self._wakeup.wait(), self.poll_interval)
            except TimeoutError:
                pass
            self._wakeup.clear()
