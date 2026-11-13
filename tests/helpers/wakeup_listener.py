"""Standalone LISTEN probe kept with the test instead of in ephemeral /tmp."""

import asyncio
import json
import sys
from time import monotonic
from uuid import UUID

from psycopg import AsyncConnection
from chagent.events.notify import CHANNEL


async def main():
    dsn, _schema, run_id, timeout = sys.argv[1:]
    target = UUID(run_id)
    async with await AsyncConnection.connect(dsn, autocommit=True) as connection:
        await connection.execute(f"LISTEN {CHANNEL}")
        print("READY", flush=True)
        started = monotonic()
        async with asyncio.timeout(float(timeout)):
            async for notification in connection.notifies():
                if notification.payload == str(target):
                    print(json.dumps({"payload": notification.payload, "latency_ms": (monotonic()-started)*1000}), flush=True)
                    return


if __name__ == "__main__":
    asyncio.run(main())
