"""Broadcast wakeups for a run, in process and across processes.

The in-process bus is the fast path. When the process owns a database, a
LISTEN/NOTIFY listener is attached as well, so a decision made by another
process (a second controller, a CLI, a script) wakes this one instead of waiting
for the next poll tick.

Neither path carries state: both only say "look again". ``Subscription.wait``
therefore always times out eventually, and a lost notification costs latency.
"""

import asyncio
from contextlib import contextmanager
from uuid import UUID


class Subscription:
    def __init__(self):
        self.version = 0
        self.users = 0
        self._changed = asyncio.Event()

    def notify(self) -> None:
        self.version += 1
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait(self, version: int, *, timeout: float = 1.0) -> None:
        # No await between checking the version and capturing its event.
        if self.version != version:
            return
        changed = self._changed
        try:
            async with asyncio.timeout(timeout):
                await changed.wait()
        except TimeoutError:
            # A missed notification (listener reconnecting) is recovered by the
            # caller re-reading its rows; the timeout is that recovery bound.
            pass


class RunNotifications:
    def __init__(self):
        self._subscriptions: dict[UUID, Subscription] = {}
        self._database = None
        self._wakeups = None

    def attach(self, database) -> None:
        """Offer cross-process wakeups through this database, once it is open."""
        self._database = database

    def _ensure_listener(self) -> None:
        if self._wakeups is not None or self._database is None:
            return
        if not getattr(self._database, "connected", False):
            return
        from chagent.events.notify import PostgresWakeups

        self._wakeups = PostgresWakeups(self._database.conninfo, self.notify)
        self._wakeups.start()

    async def close(self) -> None:
        wakeups, self._wakeups = self._wakeups, None
        if wakeups is not None:
            await wakeups.stop()

    @contextmanager
    def subscribe(self, run_id: UUID):
        self._ensure_listener()
        subscription = self._subscriptions.setdefault(run_id, Subscription())
        subscription.users += 1
        try:
            yield subscription
        finally:
            subscription.users -= 1
            if not subscription.users:
                del self._subscriptions[run_id]

    def notify(self, run_id: UUID) -> None:
        # No listeners means no retained entry. This operation does not await,
        # write storage, or backpressure a successful commit.
        if subscription := self._subscriptions.get(run_id):
            subscription.notify()
