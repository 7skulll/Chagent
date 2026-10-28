"""Write-path baseline: explicit SQL, transactions, WAL bytes and wall time per run.

Runs against a disposable schema with offline clients only; no provider requests.
Two scenarios: a two-turn tool run, and a slow streamed answer.
"""

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import median
import tempfile
from time import perf_counter, time_ns
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.bootstrap import create_runner
from chagent.config import Settings
from chagent.domain.messages import AssistantMessage, TextContent
from chagent.models.base import Model, ModelRequest
from chagent.models.events import (
    ContentDeltaEvent, ContentEndEvent, ContentStartEvent, DoneEvent, StartEvent,
)
from chagent.models.fake import FakeModelClient
from chagent.storage.database import Database
from chagent.storage.migrate import migrate


class SlowStreamClient:
    """Emits a fixed number of text deltas at a fixed spacing, like a provider."""

    def __init__(self, deltas: int = 150, gap: float = 0.01):
        self.deltas, self.gap = deltas, gap

    async def stream(self, request: ModelRequest):
        partial = AssistantMessage(content=[], api=request.model.api, provider=request.model.provider,
                                   model=request.model.id, timestamp=time_ns() // 1_000_000,
                                   stop_reason="pending")
        yield StartEvent(partial=partial)
        partial.content.append(TextContent(text=""))
        yield ContentStartEvent(type="text_start", content_index=0, partial=partial)
        text = ""
        for index in range(self.deltas):
            await asyncio.sleep(self.gap)
            piece = f"片段{index}，"
            text += piece
            partial.content[0].text = text
            yield ContentDeltaEvent(type="text_delta", content_index=0, delta=piece, partial=partial)
        yield ContentEndEvent(type="text_end", content_index=0, content=text, partial=partial)
        final = AssistantMessage(content=[TextContent(text=text)], api=request.model.api,
                                 provider=request.model.provider, model=request.model.id,
                                 timestamp=time_ns() // 1_000_000, stop_reason="stop")
        yield DoneEvent(reason="stop", message=final)


class CountingDatabase(Database):
    def __init__(self, settings):
        self.counter = {"sql": 0, "tx": 0}
        super().__init__(settings)

    @asynccontextmanager
    async def connection(self):
        async with super().connection() as connection:
            yield _Connection(connection, self.counter)

    @asynccontextmanager
    async def transaction(self):
        self.counter["tx"] += 1
        async with super().transaction() as connection:
            yield connection


class _Connection:
    def __init__(self, connection, counter):
        self._connection, self._counter = connection, counter

    async def execute(self, *args, **kwargs):
        self._counter["sql"] += 1
        return await self._connection.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


async def lsn(database) -> str:
    async with database.connection() as connection:
        cursor = await connection.execute("SELECT pg_current_wal_lsn() AS lsn")
        return (await cursor.fetchone())["lsn"]


async def wal_since(database, mark: str) -> int:
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s::pg_lsn) AS walked", (mark,))
        return int((await cursor.fetchone())["walked"])


async def sample(database, runner, session_id, text, arguments=None):
    runner.client = (FakeModelClient("echo", arguments) if arguments is not None
                     else SlowStreamClient())
    before = await lsn(database)
    database.counter.update(sql=0, tx=0)
    started = perf_counter()
    result = await runner.run(session_id, text)
    elapsed = (perf_counter() - started) * 1000
    walked = await wal_since(database, before)
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE type = 'text_delta') AS deltas "
            "FROM run_events WHERE run_id = %s", (result.run_id,))
        row = await cursor.fetchone()
    return {"sql": database.counter["sql"], "transactions": database.counter["tx"],
            "events": row["total"], "text_delta": row["deltas"],
            "wal_bytes": walked, "wall_ms": round(elapsed, 1)}


def summarise(samples):
    keys = samples[0].keys()
    return {key: median(sample[key] for sample in samples) for key in keys}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()

    settings = Settings.from_env()
    address = make_conninfo(settings.database_url)
    admin = await AsyncConnection.connect(address, autocommit=True)
    schema = f"chagent_bench_{uuid4().hex[:12]}"
    await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    result = {"measured_at": datetime.now(timezone.utc).isoformat(), "repeats": options.repeats}
    try:
        scoped = Settings(database_url=make_conninfo(address, options=f"-c search_path={schema}"))
        async with CountingDatabase(scoped) as database:
            await migrate(database)
            model = Model(id="bench", provider="local", api="fake", context_window=128000, max_tokens=1024)
            with tempfile.TemporaryDirectory() as workspace:
                runner = create_runner(database, model, FakeModelClient())
                session_id = await runner.conversations.create_session(Path(workspace), title="bench")
                await runner.run(session_id, "预热")
                for name, text, arguments in (
                    ("tool_turn", "回显这句话。", {"text": "基线"}),
                    ("stream", "请输出一段较长的说明。", None),
                ):
                    samples = []
                    for index in range(options.repeats):
                        samples.append(await sample(database, runner, session_id,
                                                    f"{text} 第 {index} 次", arguments))
                    result[name] = summarise(samples)
    finally:
        await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        await admin.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if options.output:
        options.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
