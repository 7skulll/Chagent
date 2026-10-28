"""Offline event-path A/B samples; disposable schema, no provider requests."""

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from statistics import median
import tempfile
from time import perf_counter
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from diagnose_main_path import Metrics, ObservedDatabase, instrument, runner_for
from chagent.config import Settings
from chagent.domain.messages import AssistantMessage, TextContent
from chagent.models.events import ContentDeltaEvent, DoneEvent, StartEvent
from chagent.storage.migrate import migrate
from chagent.web.app import WebRuntime, events


class FixtureClient:
    def __init__(self, count, pause):
        self.count, self.pause = count, pause
        self.release = asyncio.Event()
        self.produced = []

    async def stream(self, request):
        message = AssistantMessage([TextContent("")], "fake", "local-test", "fixture", 0, "pending")
        yield StartEvent(message)
        await self.release.wait()
        for _ in range(self.count):
            if self.pause:
                await asyncio.sleep(self.pause)
            self.produced.append(perf_counter())
            message.content[0].text += "汉字abcd"
            yield ContentDeltaEvent("text_delta", 0, "汉字abcd", message)
        self.finished = perf_counter()
        message.stop_reason = "stop"
        yield DoneEvent("stop", message)


class ConnectedRequest:
    async def is_disconnected(self):
        return False


def stats(values):
    ordered = sorted(values)
    return {"median": round(median(ordered), 3), "p95": round(ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)], 3)}


async def sample(database, workspace, *, count, pause=0, sink_delay=0):
    client = FixtureClient(count, pause)
    runner = runner_for(database, client)
    session = await runner.conversations.create_session(workspace)
    runtime = WebRuntime(runner, workspace)
    commits, delivered = [], []
    instrument(runner, database, event_delay=sink_delay)
    original = runner.runs.event

    async def tracked(run_id, kind, payload):
        await original(run_id, kind, payload)
        if kind == "text_delta":
            commits.append(perf_counter())

    runner.runs.event = tracked
    database.metrics = Metrics()
    started = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(runner.run(session, "fixture", on_started=started.set_result))
    try:
        identity = await started
        response = await events(identity, ConnectedRequest(), runtime, after=0, last_event_id=None)

        async def consume():
            answer = ""
            async for block in response.body_iterator:
                if "event: update" not in block:
                    continue
                data = json.loads(next(line[6:] for line in block.splitlines() if line.startswith("data: ")))
                if data["type"] == "text_delta":
                    delivered.append(perf_counter())
                    answer += data["payload"]["delta"]
            return answer

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0)
        client.release.set()
        result = await task
        answer = await consumer
        metrics = database.metrics.snapshot()
        assert answer == result.message.content[0].text
        assert len(commits) == len(delivered)
        return {"text_transactions": len(commits), "sql": metrics["sql_execute_calls"],
                "model_consume_ms": (client.finished - client.produced[0]) * 1000,
                "first_commit_ms": (commits[0] - client.produced[0]) * 1000,
                "first_sse_ms": (delivered[0] - client.produced[0]) * 1000,
                "commit_to_sse_ms": [(seen - saved) * 1000 for saved, seen in zip(commits, delivered)]}
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if 'consumer' in locals():
            if not consumer.done():
                consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)


async def main(args):
    address = os.environ["CHAGENT_TEST_DATABASE_URL"]
    schema = "chagent_event_bench_" + uuid4().hex
    admin = await AsyncConnection.connect(address, autocommit=True)
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "repeats": args.repeats,
              "scope": "offline model; real PostgreSQL; direct SSE body generation, excludes browser/network; nearest-rank P95",
              "scenarios": {}, "schema_cleaned": False}
    try:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        async with ObservedDatabase(replace(Settings(), database_url=make_conninfo(address, options=f"-c search_path={schema}"))) as database:
            await migrate(database)
            with tempfile.TemporaryDirectory() as folder:
                workspace = Path(folder)
                await sample(database, workspace, count=2)
                for name, spec in {"burst_128": {"count": 128}, "paced_32": {"count": 32, "pause": .005},
                                   "slow_database_128": {"count": 128, "sink_delay": .005}}.items():
                    samples = [await sample(database, workspace, **spec) for _ in range(args.repeats)]
                    summary = {key: stats([item[key] for item in samples]) for key in
                               ("text_transactions", "sql", "model_consume_ms", "first_commit_ms", "first_sse_ms")}
                    summary["commit_to_sse_ms"] = stats([value for item in samples for value in item["commit_to_sse_ms"]])
                    report["scenarios"][name] = {"spec": spec, "summary": summary, "samples": samples}
                    print(json.dumps({"scenario": name, **summary}), flush=True)
    finally:
        await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        await admin.close()
        report["schema_cleaned"] = True
    sources = ("agent/runner.py", "storage/repositories.py", "web/app.py", "agent/stream_writer.py", "events/notifications.py")
    root = Path(__file__).resolve().parents[1]
    report["source_sha256"] = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name in sources
                               if (path := root / "src/chagent" / name).exists()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=30)
    asyncio.run(main(parser.parse_args()))
