"""Offline main-path diagnostics against a disposable PostgreSQL schema.

No provider requests or business-table writes. Timings include instrumentation.
Known defects are reported as observations, not converted into passing tests.
"""

import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
from statistics import median
from time import perf_counter, sleep
import tempfile
from uuid import uuid4

import httpx
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.agent.runner import RunFailedError
from chagent.bootstrap import create_runner
from chagent.config import Settings
from chagent.context.builder import BasicContextBuilder
from chagent.context.tokens import ByteTokenCounter
from chagent.domain.context import ContextPolicy
from chagent.domain.memories import MemoryPolicy
from chagent.domain.messages import AssistantMessage, TextContent, ToolCall, UserMessage
from chagent.domain.runs import StoredMessage
from chagent.domain.summaries import CompactionPolicy
from chagent.models.base import Model, ModelRequest
from chagent.models.events import ContentDeltaEvent, DoneEvent, StartEvent
from chagent.models.factory import create_model_client
from chagent.models.settings import ModelSettings
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import RunBusyError
from chagent.tools.context import ToolContext


PHASE = ContextVar("diagnostic_phase", default="other")


class Metrics:
    def __init__(self):
        self.sql = Counter()
        self.sql_ms = Counter()
        self.spans = defaultdict(list)
        # Text deltas are buffered by RunRepository.emit and only reach PostgreSQL
        # on the next flush, so acceptance and commit are two distinct timestamps.
        self.first_text_queued = None
        self.first_text_committed = None

    def snapshot(self):
        return {
            "sql_execute_calls": sum(self.sql.values()),
            "sql_by_phase": dict(self.sql),
            "sql_execute_ms_by_phase": {key: round(value, 3) for key, value in self.sql_ms.items()},
            "spans": {key: {"calls": len(values), "total_ms": round(sum(values), 3)}
                      for key, values in self.spans.items()},
        }


class ObservedConnection:
    def __init__(self, connection, database):
        self.connection, self.database = connection, database

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def execute(self, *args, **kwargs):
        phase = PHASE.get()
        started = perf_counter()
        try:
            return await self.connection.execute(*args, **kwargs)
        finally:
            self.database.metrics.sql[phase] += 1
            self.database.metrics.sql_ms[phase] += (perf_counter() - started) * 1000


class ObservedDatabase(Database):
    def __init__(self, settings):
        self.metrics = Metrics()
        super().__init__(settings)

    @asynccontextmanager
    async def connection(self):
        async with super().connection() as connection:
            yield ObservedConnection(connection, self)


class CountingCounter:
    name = "diagnostic_utf8_json_bytes"
    # Delegates to ByteTokenCounter, so per-message costs are additive.
    additive = True

    def __init__(self):
        self.calls = self.items = 0
        self.delegate = ByteTokenCounter()

    def count(self, messages):
        self.calls += 1
        self.items += len(messages)
        return self.delegate.count(messages)


class ScriptedClient:
    """Deterministic zero-network model with configurable tool batches/deltas."""

    def __init__(self, *, steps=0, calls=1, deltas=1, memory_delay=0, fail_after_tool=False,
                 tool_name="echo", tool_arguments=None):
        self.steps, self.calls, self.deltas = steps, calls, deltas
        self.memory_delay, self.fail_after_tool = memory_delay, fail_after_tool
        self.tool_name, self.tool_arguments = tool_name, tool_arguments
        self.task_calls = self.memory_calls = 0
        self.final_ready = None

    async def stream(self, request):
        message = AssistantMessage([], request.model.api, request.model.provider,
                                   request.model.id, 0, "pending")
        yield StartEvent(message)
        if request.purpose == "memory":
            self.memory_calls += 1
            await asyncio.sleep(self.memory_delay)
            message.content = [TextContent('{"memories":[]}')]
            message.stop_reason = "stop"
            yield DoneEvent("stop", message)
            return
        if request.purpose != "task":
            raise AssertionError("Compaction is disabled in this controlled fixture")
        self.task_calls += 1
        if self.fail_after_tool and self.task_calls > self.steps:
            raise RuntimeError("diagnostic failure after a completed tool")
        if self.task_calls <= self.steps:
            message.content = [ToolCall(f"call-{self.task_calls}-{index}", self.tool_name,
                                       self.tool_arguments or {"text": "fixture"})
                               for index in range(self.calls)]
            message.stop_reason = "toolUse"
        else:
            # Fixed total output size: only delta granularity changes.
            message.content = [TextContent("")]
            for index in range(self.deltas):
                delta = "x" * (1024 // self.deltas)
                message.content[0].text += delta
                yield ContentDeltaEvent("text_delta", 0, delta, message)
            message.stop_reason = "stop"
            self.final_ready = perf_counter()
        yield DoneEvent(message.stop_reason, message)


def runner_for(database, client, *, memory=False, extract=True, timeout=10, counter=None, max_concurrency=4):
    return create_runner(
        database, Model("offline-diagnostic", "local-test", "fake", 65536, 2048), client,
        timeout=timeout, max_turns=32, token_counter=counter, max_tool_concurrency=max_concurrency,
        compaction_policy=CompactionPolicy(enabled=False),
        memory_policy=MemoryPolicy(enabled=memory, extract=extract, timeout=2),
        # The scripted client calls ``echo`` and the timing case patches it, which
        # is exactly the probe role the live registry no longer spends a slot on.
        include_echo=True,
    )


def instrument(runner, database, *, event_delay=0):
    def wrap(owner, method_name, label):
        original = getattr(owner, method_name)

        async def measured(*args, **kwargs):
            phase = "text_delta" if label in ("event", "event_emit") and args[1] == "text_delta" else label
            token = PHASE.set(phase)
            started = perf_counter()
            try:
                if phase == "text_delta" and event_delay:
                    await asyncio.sleep(event_delay)
                return await original(*args, **kwargs)
            finally:
                database.metrics.spans[phase].append((perf_counter() - started) * 1000)
                if phase == "text_delta" and database.metrics.first_text_queued is None:
                    database.metrics.first_text_queued = perf_counter()
                if phase == "event_flush" and database.metrics.first_text_queued is not None \
                        and database.metrics.first_text_committed is None:
                    database.metrics.first_text_committed = perf_counter()
                PHASE.reset(token)

        setattr(owner, method_name, measured)

    for method in ("begin", "event", "emit", "start_model_call", "save_response", "start_tool_call",
                   "queue_tool_calls", "save_tool_result", "finish", "flush_events"):
        wrap(runner.runs, method, "event_emit" if method == "emit" else
             "event_flush" if method == "flush_events" else method)
    wrap(runner, "_response", "model_stream")
    wrap(runner.context_manager, "prepare", "context_prepare")
    wrap(runner.memory_service, "enrich", "memory_recall")
    wrap(runner.memory_service, "on_completed", "memory_enqueue")
    wrap(runner.tools, "execute", "tool_execute")


async def measure_run(database, workspace, spec):
    client = ScriptedClient(**spec.get("client", {}))
    counter = CountingCounter()
    runner = runner_for(database, client, memory=spec.get("memory", False),
                        extract=spec.get("extract", True), counter=counter, max_concurrency=spec.get("max_concurrency", 4))
    if delay := spec.get("tool_delay", 0):
        echo = runner.tools.registry.get("echo")
        original = echo.execute

        async def slow_echo(*args, **kwargs):
            await asyncio.sleep(delay)
            return await original(*args, **kwargs)

        echo.execute = slow_echo
    session = await runner.conversations.create_session(workspace)
    database.metrics = Metrics()
    instrument(runner, database, event_delay=spec.get("event_delay", 0))
    started = perf_counter()
    result = await runner.run(session, "diagnostic input " * (8 if spec.get("memory") else 1))
    ended = perf_counter()
    metrics = database.metrics.snapshot()
    # Validation reads below are outside the measured operation.
    run = await runner.runs.get(result.run_id)
    events = await runner.runs.events(result.run_id)
    assert run["status"] == "completed"
    assert events[-1]["type"] == "run_completed"
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert "".join(event["payload"]["delta"] for event in events if event["type"] == "text_delta") == result.message.content[0].text
    # Extraction is drained after the measurement so its cost is reported
    # separately instead of being folded into the run.
    drained_ms = None
    if spec.get("memory"):
        drain_started = perf_counter()
        await runner.memory_worker.drain(timeout=15)
        drained_ms = round((perf_counter() - drain_started) * 1000, 3)
    queued, committed = database.metrics.first_text_queued, database.metrics.first_text_committed
    return {
        "elapsed_ms": round((ended - started) * 1000, 3),
        "first_text_queued_ms": round((queued - started) * 1000, 3) if queued else None,
        "first_text_committed_ms": round((committed - started) * 1000, 3) if committed else None,
        "after_final_model_output_ms": round((ended - client.final_ready) * 1000, 3),
        "memory_drain_ms": drained_ms,
        "task_model_calls": client.task_calls, "memory_model_calls": client.memory_calls,
        "events": len(events), "counter_calls": counter.calls,
        "messages_serialized_for_count": counter.items, **metrics,
    }


async def builder_probe(repeats):
    results = []
    for turns in (20, 100, 200):
        history = []
        for index in range(turns):
            run_id = uuid4()
            history.extend([
                StoredMessage(uuid4(), index * 2 + 1, UserMessage("x" * 200, 0), run_id),
                StoredMessage(uuid4(), index * 2 + 2, AssistantMessage(
                    [TextContent("y" * 200)], "fake", "local-test", "fixture", 0, "stop"), run_id),
            ])
        samples = []
        for _ in range(repeats):
            counter = CountingCounter()
            builder = BasicContextBuilder(policy=ContextPolicy(max_history_messages=1000), counter=counter)
            started = perf_counter()
            built = await builder.build(history, Model("fixture", "local-test", "fake", 10_000_000, 1000), [])
            samples.append((perf_counter() - started) * 1000)
            assert len(built.snapshot.source_message_ids) == len(history)
        results.append({"messages": len(history), "median_ms": round(median(samples), 3),
                        "count_calls": counter.calls, "messages_serialized": counter.items})
    return results


async def fault_probes(database, workspace):
    budgets = {}
    for provider in ("openai", "deepseek", "qwen", "anthropic", "custom"):
        settings = ModelSettings.from_env({"CHAGENT_PROVIDER": provider, "CHAGENT_API_KEY": "offline-fixture",
                                           **({"CHAGENT_BASE_URL": "https://fixture.invalid/v1", "CHAGENT_MODEL": "fixture"}
                                              if provider == "custom" else {})})
        captured = []

        def handler(request):
            captured.append(json.loads(request.content))
            if provider == "anthropic":
                events = [
                    {"type": "message_start", "message": {"id": "fixture", "model": settings.model_id,
                                                             "usage": {"input_tokens": 1}}},
                    {"type": "content_block_start", "index": 0,
                     "content_block": {"type": "text", "text": "ok"}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                    {"type": "message_stop"},
                ]
                body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
            else:
                event = {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
                body = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

        async with create_model_client(settings, transport=httpx.MockTransport(handler)) as client:
            events = [event async for event in client.stream(ModelRequest(settings.model(), [UserMessage("hello", 0)], max_tokens=17))]
        assert isinstance(events[-1], DoneEvent), f"{provider} fixture did not complete"
        fields = {key: value for key, value in captured[0].items()
                  if key in {"max_tokens", "max_completion_tokens", "max_output_tokens"}}
        assert list(fields.values()) == [17], f"{provider} output budget was not transmitted"
        budgets[provider] = fields
    result = {"output_budget": {"requested": 17, "actual_transport_body_by_provider": budgets,
                                "all_transmitted": True}}

    # Extraction must no longer share the run's timeout: a 2s provider with a
    # 0.5s run budget used to fail an answer that was already durable.
    client = ScriptedClient(memory_delay=1)
    runner = runner_for(database, client, memory=True, timeout=0.5)
    session = await runner.conversations.create_session(workspace)
    try:
        completed = await runner.run(session, "long diagnostic input " * 8)
        outcome = "completed"
    except RunFailedError:
        completed, outcome = None, "failed"
    records = await runner.conversations.messages(session)
    job = await runner.memory_worker.jobs.for_run(completed.run_id) if completed else None
    result["memory_tail_isolation"] = {
        "status": outcome,
        "final_answer_persisted": any(
            isinstance(record.message, AssistantMessage) and record.message.stop_reason == "stop" for record in records),
        "memory_calls_during_run": client.memory_calls,
        "job_status_after_run": job["status"] if job else None,
    }
    if completed is not None:
        await runner.memory_worker.drain(timeout=15)
        result["memory_tail_isolation"]["memory_calls_after_drain"] = client.memory_calls
        result["memory_tail_isolation"]["job_status_after_drain"] = (
            await runner.memory_worker.jobs.for_run(completed.run_id))["status"]

    client = ScriptedClient(steps=1, fail_after_tool=True, tool_name="write_file",
                            tool_arguments={"path": "side-effect.txt", "content": "completed write"})
    runner = runner_for(database, client)
    session = await runner.conversations.create_session(workspace)
    try:
        await runner.run(session, "write then encounter a provider failure")
    except RunFailedError as error:
        failed_id = error.run_id
    else:
        raise AssertionError("Failure fixture unexpectedly completed")
    inspectable = await runner.conversations.messages(session)
    next_id = await runner.runs.begin(session, UserMessage("continue", 0))
    try:
        replayed = await runner.conversations.messages(session, current_run=next_id)
        result["failed_run_side_effect"] = {
            "file_exists": (workspace / "side-effect.txt").is_file(),
            "inspectable_messages_from_failed_run": sum(item.run_id == failed_id for item in inspectable),
            "replayed_messages_from_failed_run": sum(item.run_id == failed_id for item in replayed),
        }
    finally:
        await runner.runs.finish(next_id, "cancelled", "diagnostic cleanup")

    session = await runner.conversations.create_session(workspace)
    orphan_id = await runner.runs.begin(session, UserMessage("simulate owner loss", 0))
    fresh = runner_for(database, ScriptedClient())
    try:
        try:
            await fresh.run(session, "try continuing")
            blocked = False
        except RunBusyError:
            blocked = True
        result["ownerless_run"] = {"new_run_blocked": blocked, "fresh_runner_can_cancel": fresh.cancel(orphan_id),
                                   "persisted_status": (await fresh.runs.get(orphan_id))["status"]}
    finally:
        await runner.runs.finish(orphan_id, "interrupted", "diagnostic cleanup")

    # Inject a slow filesystem operation at the synchronous handle boundary.
    # This demonstrates event-loop blocking; it is not a disk-speed benchmark.
    runner = runner_for(database, ScriptedClient())
    reader = runner.tools.registry.get("read_file")
    original_handle = reader.handle

    def slow_handle(*args, **kwargs):
        sleep(0.05)
        return original_handle(*args, **kwargs)

    reader.handle = slow_handle
    runner.tools.timeout = 0.01
    (workspace / "read-probe.txt").write_text("fixture", encoding="utf-8")
    started = perf_counter()
    read_result = await runner.tools.execute(ToolCall("read-probe", "read_file", {"path": "read-probe.txt"}),
                                             context=ToolContext(workspace))
    result["blocking_file_timeout"] = {"configured_timeout_ms": 10, "injected_blocking_ms": 50,
                                       "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                                       "returned_error": read_result.is_error}
    return result


async def diagnose(args):
    address = os.environ.get("CHAGENT_TEST_DATABASE_URL")
    if not address:
        raise SystemExit("Set CHAGENT_TEST_DATABASE_URL; diagnostics never load .env")
    schema = "chagent_diag_" + uuid4().hex
    admin = await AsyncConnection.connect(address, autocommit=True, connect_timeout=5)
    try:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            async with ObservedDatabase(Settings(database_url=make_conninfo(address, options=f"-c search_path={schema}"))) as database:
                await migrate(database)
                cursor = await admin.execute("SHOW server_version")
                postgres_version = (await cursor.fetchone())[0]
                specs = {
                    "one_tool": {"client": {"steps": 1}},
                    "text_1_delta": {},
                    "text_32_deltas": {"client": {"deltas": 32}},
                    "text_128_deltas": {"client": {"deltas": 128}},
                    "text_32_deltas_5ms_sink": {"client": {"deltas": 32}, "event_delay": 0.005},
                    "three_20ms_tools_serial": {"client": {"steps": 1, "calls": 3}, "tool_delay": 0.02, "max_concurrency": 1},
                    "three_20ms_tools": {"client": {"steps": 1, "calls": 3}, "tool_delay": 0.02},
                    "eight_tool_rounds": {"client": {"steps": 8}},
                    "memory_recall_only": {"memory": True, "extract": False, "client": {"steps": 8}},
                    "memory_500ms_extraction": {"memory": True, "client": {"memory_delay": 0.5}},
                }
                scenarios = {}
                with tempfile.TemporaryDirectory(prefix="chagent-diag-") as temporary:
                    workspace = Path(temporary)
                    # Warm the pool and imports; warmup is excluded from reported samples.
                    await measure_run(database, workspace, {})
                    for name, spec in specs.items():
                        samples = [await measure_run(database, workspace, spec) for _ in range(args.repeats)]
                        scenarios[name] = {"spec": spec, "median_ms": median(item["elapsed_ms"] for item in samples),
                                           "median_after_final_model_output_ms": median(item["after_final_model_output_ms"] for item in samples),
                                           "samples": samples}
                    faults = await fault_probes(database, workspace)
                root = Path(__file__).resolve().parents[1]
                sources = ["agent/runner.py", "context/builder.py", "context/manager.py", "memory/service.py",
                           "storage/repositories.py", "tools/scheduler.py", "tools/executor.py", "tools/filesystem.py", "models/transcript.py", "models/openai_chat.py", "web/app.py"]
                return {
                    "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                    "environment": {"python": platform.python_version(), "platform": platform.system(),
                                    "postgresql": postgres_version},
                    "method": {"repeats": args.repeats, "provider": "offline scripted; budget probes use MockTransport, no external HTTP",
                               "sql_count_excludes": "implicit BEGIN/COMMIT, fetch, pool maintenance, setup and validation",
                               "span_times": "inclusive and nested; do not sum overlapping spans",
                               "output_chars": 1024, "builder_history_limit": 1000,
                               "scope": "single active run, no browser end-to-end or Pi/Codex speed comparison"},
                    "source_sha256": {name: hashlib.sha256((root / "src/chagent" / name).read_bytes()).hexdigest()
                                      for name in sources},
                    "scenarios": scenarios, "builder": await builder_probe(args.repeats), "faults": faults,
                }
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    finally:
        await admin.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 30:
        parser.error("--repeats must be between 1 and 30")
    report = asyncio.run(diagnose(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved diagnostics to {args.output.resolve()}")
    for name, scenario in report["scenarios"].items():
        print(f"{name}: median={scenario['median_ms']:.2f}ms, SQL={scenario['samples'][0]['sql_execute_calls']}")
    print(json.dumps(report["faults"], ensure_ascii=False))


if __name__ == "__main__":
    main()
