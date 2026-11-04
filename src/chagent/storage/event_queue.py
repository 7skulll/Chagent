"""Ordered, batched persistence for high-frequency run events.

Text deltas arrive every few milliseconds and each one used to cost a
transaction, an ownership check and an fsync: a 1.6 second answer produced 76
commits for 76 single-row inserts. They are buffered per run here and committed
as one multi-row insert per interval, so the model stream is never gated by
commit latency.

Sequence numbers stay in the database. Buffered events are always flushed
before any other write to the same run, therefore the existing
``MAX(sequence) + 1`` allocator still yields a gap-free, increasing stream and
replay is unchanged.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from uuid import UUID

logger = logging.getLogger(__name__)

EventItem = tuple[str, dict]
EventWriter = Callable[[list[EventItem]], Awaitable[None]]


class EventStream:
    """One run's buffer, its ordering lock and its background flush task."""

    interval = .1
    max_batch = 64
    capacity = 256

    def __init__(self, run_id: UUID, write: EventWriter) -> None:
        self._write = write
        self._pending: deque[EventItem] = deque()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._failed = asyncio.Event()
        self._error: BaseException | None = None
        self._closing = False
        self._committed = 0
        self.task = asyncio.create_task(self._run(), name=f"run-event-flush-{run_id}")

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def append(self, kind: str, payload: dict) -> None:
        """Queue one event without touching the database.

        The caller is only blocked when the buffer is full, which turns a
        stalled database into backpressure instead of unbounded memory.
        """
        self._raise_if_failed()
        if len(self._pending) >= self.capacity:
            await self.flush()
        self._pending.append((kind, payload))
        self._wake.set()

    async def flush(self) -> None:
        """Commit everything pending now; empty buffers cost nothing."""
        async with self._lock:
            self._raise_if_failed()
            if not self._pending:
                return
            items = list(self._pending)
            try:
                await self._write(items)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                self._mark_failed(error)
                raise
            for _ in range(len(items)):
                self._pending.popleft()
            self._committed += len(items)

    async def failure(self) -> None:
        """Complete only after a batched write failed, then raise its error.

        Callers wait on this next to the model stream so a storage failure
        interrupts the run instead of letting it produce output that can no
        longer be recorded.
        """
        await self._failed.wait()
        raise self._error  # type: ignore[misc]

    def discard(self) -> None:
        """Drop buffered events: a rejected batch is not retried forever."""
        self._pending.clear()

    async def close(self, *, flush: bool = True) -> None:
        try:
            if flush:
                await self.flush()
        except Exception as error:
            # The run's own outcome is already decided; losing the tail of the
            # batch must not turn a completed run into a failed one.
            logger.warning("Dropped buffered run events on close (%s)", type(error).__name__)
        finally:
            self._closing = True
            self._wake.set()
            if not self.task.done():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def _raise_if_failed(self) -> None:
        if self._error is not None:
            raise self._error

    def _mark_failed(self, error: BaseException) -> None:
        if self._error is None:
            self._error = error
            self._failed.set()

    async def _run(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                if not self._pending:
                    if self._closing:
                        return
                    continue
                if self._committed:
                    # The first batch goes out immediately so the first visible
                    # characters are not delayed by a full window.
                    await self._settle()
                await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Reported through append()/flush()/failure(); a raising task would
            # only surface as an unhandled exception in the event loop.
            return

    async def _settle(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.interval
        while len(self._pending) < self.max_batch:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(self._wake.wait(), remaining)
            except TimeoutError:
                return
            self._wake.clear()
