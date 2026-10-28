"""Bounded, timed text persistence for one model response."""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
import math


class TextStreamWriter:
    def __init__(self, save: Callable[[str, int], Awaitable[None]], *, interval: float = .02,
                 batch_bytes: int = 4096, capacity_bytes: int = 65536):
        if not math.isfinite(interval) or interval <= 0 or not 4 <= batch_bytes <= capacity_bytes:
            raise ValueError("Invalid text writer limits")
        self.save, self.interval = save, interval
        self.batch_bytes, self.capacity_bytes = batch_bytes, capacity_bytes
        self._condition = asyncio.Condition()
        self._pending: deque[tuple[int, str, int, float]] = deque()
        self._buffered = self._queued = self._accepted = self._committed = self._flush_through = 0
        self._closing = False
        self._error: BaseException | None = None
        self.task = asyncio.create_task(self._run(), name="model-text-writer")

    def _check(self):
        if self._error is not None:
            raise self._error

    async def append(self, text: str) -> None:
        # Slice before encoding so one oversized provider delta does not create
        # another unbounded encoded copy. Four bytes cover a Unicode scalar.
        step = self.batch_bytes // 4
        for offset in range(0, len(text), step):
            part = text[offset:offset + step]
            size = len(part.encode("utf-8"))
            async with self._condition:
                await self._condition.wait_for(lambda: self._error is not None or self._closing
                                                or self._buffered + size <= self.capacity_bytes)
                self._check()
                if self._closing:
                    raise RuntimeError("Text writer is closing")
                self._accepted += 1
                self._buffered += size
                self._queued += size
                self._pending.append((self._accepted, part, size, asyncio.get_running_loop().time()))
                self._condition.notify_all()

    async def flush(self) -> None:
        async with self._condition:
            self._check()
            target = self._accepted
            self._flush_through = max(self._flush_through, target)
            self._condition.notify_all()
            await self._condition.wait_for(lambda: self._error is not None or self._committed >= target)
            self._check()

    async def close(self) -> None:
        async with self._condition:
            self._closing = True
            self._condition.notify_all()
        await asyncio.shield(self.task)

    async def stop(self) -> None:
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def _run(self) -> None:
        try:
            while True:
                async with self._condition:
                    while True:
                        if not self._pending:
                            if self._closing:
                                return
                            await self._condition.wait()
                            continue
                        deadline = self._pending[0][3] + self.interval
                        if (self._committed == 0 or self._closing or self._flush_through > self._committed
                                or self._queued >= self.batch_bytes or asyncio.get_running_loop().time() >= deadline):
                            break
                        try:
                            async with asyncio.timeout_at(deadline):
                                await self._condition.wait()
                        except TimeoutError:
                            pass
                    parts, size, through = [], 0, 0
                    while self._pending:
                        sequence, text, length, _ = self._pending[0]
                        if parts and (self._committed == 0 or size + length > self.batch_bytes):
                            break
                        self._pending.popleft()
                        parts.append(text)
                        size += length
                        through = sequence
                    self._queued -= size
                # Capacity includes this in-flight write; no lock/DB connection
                # is held by append() while it waits for capacity.
                await self.save("".join(parts), len(parts))
                async with self._condition:
                    self._buffered -= size
                    self._committed = through
                    self._condition.notify_all()
        except BaseException as error:
            async with self._condition:
                self._error = error
                self._condition.notify_all()
            raise
