"""Loop-local broadcast wakeups without per-client event queues."""

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
            pass  # Lost or cross-process notifications are recovered by polling.


class RunNotifications:
    def __init__(self):
        self._subscriptions: dict[UUID, Subscription] = {}

    @contextmanager
    def subscribe(self, run_id: UUID):
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
