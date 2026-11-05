"""Cross-process wakeups over PostgreSQL LISTEN/NOTIFY.

The in-process bus (``RunNotifications``) is enough while one process both asks
and answers. It is not enough when the decision arrives from somewhere else — a
second controller, a script, or a CLI deciding for a run that a web process is
waiting on. Before this, the waiter only noticed such a decision on its next
poll tick, which is why the poll could not be removed without losing
cross-process promptness.

Design constraints:

- **The database is the record; a notification is only a doorbell.** Nothing here
  carries state. A missed notification costs latency, never correctness: every
  waiter re-reads its rows after any wake *and* after a bounded timeout.
- **One dedicated connection, outside the pool.** A listener holds its connection
  for the life of the process, so taking one from the pool would quietly shrink
  the pool that queries depend on.
- **Reconnect and re-LISTEN.** Notifications sent while the listener was down are
  lost (PostgreSQL does not queue them), which is precisely why the timeout path
  has to stay.
"""

import asyncio
import logging
from collections.abc import Callable
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

#: One channel for every run; the payload is the run id. A channel per run would
#: need LISTEN/UNLISTEN per wait and leak channels if a waiter was cancelled.
CHANNEL = "chagent_run_wakeup"

RECONNECT_DELAY = 0.5


class PostgresWakeups:
    def __init__(self, conninfo: str, on_wakeup: Callable[[UUID], None]):
        self.conninfo, self.on_wakeup = conninfo, on_wakeup
        self._task: asyncio.Task | None = None
        self._connection: AsyncConnection | None = None
        self.connected = False
        self.deliveries = 0
        self.reconnects = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._listen(), name="approval-wakeups")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001 - closing must not raise on shutdown
                logger.debug("Could not close the wakeup connection", exc_info=True)
        self.connected = False

    async def _listen(self) -> None:
        """Stay subscribed for the life of the process; reconnect on any failure."""
        while True:
            try:
                connection = await AsyncConnection.connect(
                    self.conninfo, autocommit=True, row_factory=dict_row,
                    connect_timeout=5, application_name="chagent-wakeup")
                self._connection = connection
                await connection.execute(f"LISTEN {CHANNEL}")
                self.connected = True
                if self.reconnects:
                    logger.info("Approval wakeups reconnected")
                async for notify in connection.notifies():
                    self.deliveries += 1
                    try:
                        self.on_wakeup(UUID(notify.payload))
                    except ValueError:
                        logger.warning("Ignoring a wakeup with an unparsable payload")
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - any failure means reconnect
                self.connected = False
                self.reconnects += 1
                logger.warning("Approval wakeups disconnected (%s); reconnecting",
                               type(error).__name__)
                await asyncio.sleep(RECONNECT_DELAY)
            finally:
                if self._connection is not None:
                    try:
                        await self._connection.close()
                    except Exception:  # noqa: BLE001
                        logger.debug("Could not close a dead wakeup connection", exc_info=True)
                    self._connection = None
                self.connected = False


def notify_sql() -> str:
    """The statement every approval mutation runs inside its own transaction.

    ``pg_notify`` is transactional in PostgreSQL: the payload is delivered when
    the transaction commits, so a decision that rolls back never wakes anyone and
    a waiter that wakes always finds the committed row.
    """
    return "SELECT pg_notify(%s, %s)"
