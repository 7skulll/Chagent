"""A run's activity budget, pausable while a human is deciding.

The old implementation wrapped the run in ``asyncio.timeout``. That cannot be
paused: with a 180-second run limit and a 300-second approval window, every
waiting run would fail with ``run_timeout`` right before someone could answer.
Pausing shifts the deadline by the time actually spent waiting, and an already
expired budget cannot be revived.
"""

import asyncio
from collections.abc import Callable


class RunBudget:
    def __init__(self, seconds: float, expire: Callable[[], None]):
        if seconds <= 0:
            raise ValueError("Run budget must be positive")
        self._loop = asyncio.get_running_loop()
        self._expire = expire
        self._deadline = self._loop.time() + seconds
        self._handle: asyncio.TimerHandle | None = None
        self._paused_at: float | None = None
        self.expired = False
        self.paused = False

    def start(self) -> None:
        self._arm()

    def stop(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._loop.time())

    def _arm(self) -> None:
        self._handle = self._loop.call_later(self.remaining(), self._fire)

    def _fire(self) -> None:
        self._handle = None
        if not self.paused:
            self.expired = True
            self._expire()

    def pause(self) -> None:
        if self.paused:
            return
        self.paused = True
        self.stop()
        self._paused_at = self._loop.time()

    def resume(self) -> None:
        if not self.paused:
            return
        self.paused = False
        waited = self._loop.time() - (self._paused_at or self._loop.time())
        self._paused_at = None
        self._deadline += waited
        if not self.expired:
            self._arm()
