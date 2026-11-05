"""A run's activity budget and an independent elapsed-time ceiling.

The old implementation wrapped the run in ``asyncio.timeout``. That cannot be
paused: with a 180-second run limit and a 300-second approval window, every
waiting run would fail with ``run_timeout`` right before someone could answer.
Pausing shifts the deadline by the time actually spent waiting, and an already
expired budget cannot be revived.

Two kinds of work are now paused out of the budget: a human deciding on an
approval, and tools executing. Both can overlap — a parallel batch may hold
several running tools while one of them waits for an answer — so pause and
resume are counted rather than boolean. A boolean would let the first tool to
finish restart the clock while its siblings are still running.
"""

import asyncio
from collections.abc import Callable


class RunBudget:
    def __init__(self, seconds: float, expire: Callable[[], None], *, wall_seconds: float | None = None):
        if seconds <= 0:
            raise ValueError("Run budget must be positive")
        if wall_seconds is not None and wall_seconds <= 0:
            raise ValueError("Run wall budget must be positive")
        self._loop = asyncio.get_running_loop()
        self._expire = expire
        self._deadline = self._loop.time() + seconds
        self._handle: asyncio.TimerHandle | None = None
        self._wall_seconds = wall_seconds
        self._wall_handle: asyncio.TimerHandle | None = None
        self._pause_cap: asyncio.TimerHandle | None = None
        # A paused clock must still end. Tool time is paused out of the budget,
        # so without a ceiling one tool that never returns would hold the run
        # open forever — the exact failure the budget exists to bound. One full
        # budget is the allowance for a single paused stretch: enough for a real
        # build, short of an indefinite hang.
        self._max_pause = seconds
        self._paused_at: float | None = None
        self._pauses = 0
        self.expired = False
        self.paused = False

    def start(self) -> None:
        self._arm()
        if self._wall_seconds is not None:
            self._wall_handle = self._loop.call_later(self._wall_seconds, self._fire_wall)

    def _fire_wall(self) -> None:
        self._wall_handle = None
        if self.expired:
            return
        self.expired = True
        self._expire()

    def stop(self) -> None:
        if self._wall_handle is not None:
            self._wall_handle.cancel()
            self._wall_handle = None
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if self._pause_cap is not None:
            self._pause_cap.cancel()
            self._pause_cap = None

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._loop.time())

    def _arm(self) -> None:
        self._handle = self._loop.call_later(self.remaining(), self._fire)

    def _fire(self) -> None:
        self._handle = None
        if not self.paused:
            self.expired = True
            self._expire()

    def _fire_paused(self) -> None:
        """End the run when one paused stretch outlasts the whole budget."""
        self._pause_cap = None
        if self.paused and not self.expired:
            self.expired = True
            self._expire()

    def pause(self, *, capped: bool = True) -> None:
        """Stop the clock for one waiting party; nested pauses are counted.

        ``capped=False`` is for a wait that already has its own ceiling — an
        approval is bounded by ``approval_timeout``, and applying the run
        budget's ceiling on top would end a run that is correctly waiting for
        a person. The independent wall ceiling still applies. Tool execution
        keeps the default, because nothing else bounds a stuck tool.
        """
        self._pauses += 1
        if self._pauses > 1:
            if not capped and self._pause_cap is not None:
                self._pause_cap.cancel()
                self._pause_cap = None
            return
        self.paused = True
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if self._pause_cap is not None:
            self._pause_cap.cancel()
            self._pause_cap = None
        self._paused_at = self._loop.time()
        if capped:
            self._pause_cap = self._loop.call_later(self._max_pause, self._fire_paused)

    def resume(self) -> None:
        """Give time back for the interval this pause was the outermost one."""
        if self._pauses == 0:
            return
        self._pauses -= 1
        if self._pauses > 0:
            return
        self.paused = False
        if self._pause_cap is not None:
            self._pause_cap.cancel()
            self._pause_cap = None
        waited = self._loop.time() - (self._paused_at or self._loop.time())
        self._paused_at = None
        self._deadline += waited
        if not self.expired:
            self._arm()
