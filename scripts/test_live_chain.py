"""Opt-in paid-provider checks with synthetic fixtures and a disposable schema.

Run with uv run --env-file .env python scripts/test_live_chain.py --output PATH.
Only synthetic inputs and temporary workspace files are sent to the provider.
"""

import argparse
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
from time import perf_counter
import tempfile
import threading
from time import sleep
from uuid import uuid4

import httpx
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from diagnose_main_path import Metrics, ObservedDatabase, instrument
from run_metrics import summarize_tool_calls
from chagent.bootstrap import create_runner
from chagent.config import Settings
from chagent.domain.memories import MemoryPolicy
from chagent.domain.messages import AssistantMessage, TextContent, UserMessage
from chagent.domain.summaries import CompactionPolicy
from chagent.models.events import ContentDeltaEvent, DoneEvent, ErrorEvent
from chagent.models.factory import create_model_client
from chagent.models.settings import ModelSettings
from chagent.storage.memories import MemoryRepository
from chagent.storage.migrate import migrate
from chagent.web.app import WebRuntime, create_app
from chagent.tools.echo import EchoTool


class MeasuredClient:
    def __init__(self, client, *, max_calls):
        self.client, self.max_calls = client, max_calls
        self.calls = []

    async def stream(self, request):
        if len(self.calls) >= self.max_calls:
            raise RuntimeError("Live-test request cap reached")
        started = perf_counter()
        record = {"purpose": request.purpose, "output_limit": request.max_tokens,
                  "messages": len(request.messages), "first_text_ms": None,
                  "consumer_wait_ms": 0, "text_deltas": 0, "usage": None, "stop_reason": None}
        self.calls.append(record)
        try:
            async for event in self.client.stream(request):
                if isinstance(event, ContentDeltaEvent) and event.type == "text_delta":
                    record["text_deltas"] += 1
                if isinstance(event, ContentDeltaEvent) and event.type == "text_delta" and record["first_text_ms"] is None:
                    record["first_text_ms"] = round((perf_counter() - started) * 1000, 3)
                if isinstance(event, (DoneEvent, ErrorEvent)):
                    message = event.message if isinstance(event, DoneEvent) else event.error
                    record["stop_reason"] = message.stop_reason
                    record["response_model"] = message.response_model
                    record["usage"] = asdict(message.usage) if message.usage else None
                yielded = perf_counter()
                yield event
                record["consumer_wait_ms"] += (perf_counter() - yielded) * 1000
        finally:
            record["wall_ms"] = round((perf_counter() - started) * 1000, 3)
            record["consumer_wait_ms"] = round(record["consumer_wait_ms"], 3)


def text(message):
    if message is None:
        return ""
    return "\n".join(block.text for block in message.content if isinstance(block, TextContent))


class LiveSuite:
    def __init__(self, database, selected, client, workspace):
        self.database, self.selected, self.client, self.workspace = database, selected, client, workspace
        self.results = []
        self.memory = MemoryRepository(database)
        self.nonce = uuid4().hex[:10]

    def runner(self, *, memory=False, extract=False, compact=False):
        model = self.selected.model()
        if compact:
            model.context_window = 14000
        return create_runner(self.database, model, self.client, timeout=90, max_turns=6,
                             memory_policy=MemoryPolicy(enabled=memory, extract=extract, timeout=25),
                             compaction_policy=CompactionPolicy(enabled=compact, max_calls=3, timeout=45))

    async def settle_memory(self, runner, run_id):
        """Process queued extraction outside the run, then re-read the stream.

        Extraction is deliberately not part of ``runner.run`` any more, so the
        live check has to advance the queue itself and prove that the model call
        happened after the run reached its terminal state.
        """
        record = {"extraction_ms": None, "memory_job": None}
        if getattr(runner, "memory_worker", None) is None or run_id is None:
            return record
        started = perf_counter()
        try:
            record["processed_jobs"] = await runner.memory_worker.drain(timeout=120)
        except TimeoutError:
            record["processed_jobs"], record["extraction_timed_out"] = None, True
        record["extraction_ms"] = round((perf_counter() - started) * 1000, 3)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT status, attempts, max_attempts, last_error FROM memory_jobs WHERE run_id = %s", (run_id,))
            row = await cursor.fetchone()
        record["memory_job"] = dict(row) if row else None
        return record

    async def run(self, name, runner, prompt, *, session=None, verify=None, settle_memory=False):
        session = session or await runner.conversations.create_session(self.workspace, name)
        self.database.metrics = Metrics()
        instrument(runner, self.database)
        first_call = len(self.client.calls)
        started = perf_counter()
        identities = []
        result = None
        error = None
        try:
            result = await runner.run(session, prompt, on_started=identities.append)
        except Exception as caught:
            error = type(caught).__name__
        elapsed = (perf_counter() - started) * 1000
        metrics = self.database.metrics.snapshot()
        # Settle before reading the stream so extraction events are included,
        # but after ``elapsed`` so the reported latency stays pure run time.
        extraction = (await self.settle_memory(runner, identities[0] if identities else None)
                      if settle_memory else {})
        events = await runner.runs.events(identities[0]) if identities else []
        persisted_run = await runner.runs.get(identities[0]) if identities else None
        model_rows = []
        tool_rows = []
        if identities:
            async with self.database.connection() as connection:
                cursor = await connection.execute("SELECT request, context_snapshot FROM model_calls WHERE run_id = %s ORDER BY iteration", (identities[0],))
                model_rows = await cursor.fetchall()
                cursor = await connection.execute(
                    "SELECT name, arguments, status, call_index FROM tool_calls "
                    "WHERE run_id = %s ORDER BY created_at, call_index", (identities[0],))
                tool_rows = await cursor.fetchall()
        tool_metrics = summarize_tool_calls(tool_rows)
        types = [event["type"] for event in events]
        terminal = types.index("run_completed") if "run_completed" in types else None
        # Extraction events append to the same stream after the terminal event,
        # so "terminal" means: nothing but memory bookkeeping follows it.
        checks = {"run_completed": result is not None,
                  "ordered_events": [event["sequence"] for event in events] == list(range(1, len(events) + 1)),
                  "terminal_event": terminal is not None and all(kind.startswith("memory_") for kind in types[terminal + 1:])}
        if result is not None:
            checks["end_reason_consistent"] = (result.end_reason == persisted_run["end_reason"]
                                                == events[terminal]["payload"]["end_reason"])
        extra = {}
        if result is not None and verify is not None:
            extra_checks, extra = await verify(result, events, model_rows)
            checks.update(extra_checks)
        record = {"name": name, "passed": all(checks.values()), "checks": checks,
                  "elapsed_ms": round(elapsed, 3), "error_type": error, **extraction,
                  "failure_reason": (persisted_run["error"] or "").replace(self.selected.api_key, "[redacted]") if persisted_run else None,
                  "end_reason": result.end_reason if result else None,
                  "answer": text(result.message)[:1500] if result else None,
                  "model_calls": len(model_rows), **tool_metrics,
                  "event_types": [event["type"] for event in events],
                  "provider_calls": self.client.calls[first_call:], "metrics": metrics, **extra}
        self.results.append(record)
        summary = {"case": name, "passed": record["passed"], "elapsed_ms": record["elapsed_ms"],
                   "model_calls": record["model_calls"], "tool_calls": record["tool_calls"],
                   "repeated_tool_calls": record["repeated_tool_calls"], "checks": checks}
        if not record["passed"]:
            summary["tool_sequence"] = record["tool_sequence"]
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return result, session, record

    async def execute(self, *, core_only=False):
        async def smoke(result, events, rows):
            return {"exact_marker": text(result.message).strip() == "CHAGENT_LIVE_OK"}, {}

        result, _, _ = await self.run("smoke", self.runner(), "只回答 CHAGENT_LIVE_OK，不要调用工具。", verify=smoke)
        if result is None:
            return

        (self.workspace / "numbers.json").write_text(json.dumps({"values": [7, 11, 23], "marker": self.nonce}), encoding="utf-8")

        async def files(result, events, rows):
            path = self.workspace / "result.json"
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                value = {}
            names = [block.get("name") for row in rows for message in row["request"]["messages"]
                     if message["role"] == "assistant" for block in message["content"] if block["type"] == "toolCall"]
            return {"sum_from_file": value.get("sum") == 41, "read_tool_used": "read_file" in names,
                    "write_tool_used": path.is_file()}, {"result_file": value}

        _, session, _ = await self.run("file_tool_chain", self.runner(),
            '用 read_file 读取 numbers.json，计算 values 的总和，用 write_file 创建 result.json，内容为 {"sum":总和}。'
            '仅操作这两个文件，最终只回答 sum=数值。', verify=files)

        async def history(result, events, rows):
            answer = text(result.message)
            return {"sum_recalled": "41" in answer, "hidden_marker_recalled": self.nonce in answer,
                    "no_new_tools": not any(event["type"] == "tool_started" for event in events)}, {}

        await self.run("persisted_history", self.runner(),
                       "不要调用工具，仅依据本会话此前读取的文件内容，回答 values 总和与 marker 的原始值。", session=session, verify=history)

        if core_only:
            await self.compaction_case()
            await self.tool_stop_case()
            await self.web_case()
            return

        code = "ORBIT-" + self.nonce

        async def explicit(result, events, rows):
            session_info = await runner.conversations.get_session(result.session_id)
            memory = await self.memory.current("workspace", session_info.workspace_id, "project.launch_code")
            return {"active_memory_saved": memory is not None and code in memory.content,
                    "no_extraction_request": not any(call["purpose"] == "memory" for call in self.client.calls[start:]),
                    "no_unnecessary_file_tools": not any(event["type"] == "tool_started" for event in events)}, {}

        runner = self.runner(memory=True)
        start = len(self.client.calls)
        await self.run("explicit_memory", runner,
                       f"/remember workspace project.launch_code = 项目启动口令为 {code}", verify=explicit)

        async def recall(result, events, rows):
            return {"memory_answer": code in text(result.message),
                    "snapshot_has_memory": bool(rows and rows[0]["context_snapshot"]["memory_references"])}, {}

        await self.run("cross_session_memory", self.runner(memory=True),
                       "本项目的启动口令是什么？只回答口令，不要调用工具。", verify=recall)

        nickname = "星河测试员" + self.nonce

        async def extracted(result, events, rows):
            session_info = await runner.conversations.get_session(result.session_id)
            page = await self.memory.list(session_info.workspace_id, status="candidate")
            candidates = [item for item in page["items"] if item.source_run_id == result.run_id]
            valid = [item for item in candidates if nickname in item.content and item.evidence and item.evidence in preference]
            # Review a correct model candidate through the actual repository API.
            if valid:
                await self.memory.change(valid[0].id, session_info.workspace_id,
                                         expected_revision=valid[0].revision, action="accept")
            kinds = [event["type"] for event in events]
            async with self.database.connection() as connection:
                cursor = await connection.execute(
                    "SELECT status, attempts FROM memory_jobs WHERE run_id = %s", (result.run_id,))
                job = await cursor.fetchone()
            # The whole point of the refactor: the answer was already terminal
            # when extraction started, and the job is what carries the retries.
            return {"correct_candidate": bool(valid), "candidate_requires_review": bool(valid) and valid[0].status == "candidate",
                    "queued_in_completion_txn": "memory_job_queued" in kinds,
                    "extraction_started_after_run": ("memory_extraction_started" in kinds
                                                      and kinds.index("memory_extraction_started") > kinds.index("run_completed")),
                    "job_reached_terminal_state": job is not None and job["status"] == "completed"}, {
                "memory_job": dict(job) if job else None,
                "candidates": [{"key": item.key, "content": item.content, "status_before_review": item.status,
                                "evidence": item.evidence} for item in candidates]}

        preference = (f"这是用于验证长期偏好的测试数据。以后请默认称呼我为{nickname}。这个称呼是我的长期偏好，"
                      "请在今后的对话中沿用，而不是只用于这一次回答。这次仅用一句话确认，不要调用工具，也不要写任何文件。")
        runner = self.runner(memory=True, extract=True)
        await self.run("automatic_memory_candidate", runner, preference, verify=extracted, settle_memory=True)

        async def nickname_recall(result, events, rows):
            return {"reviewed_preference_recalled": nickname in text(result.message),
                    "snapshot_has_memory": bool(rows and rows[0]["context_snapshot"]["memory_references"])}, {}

        await self.run("reviewed_memory_recall", self.runner(memory=True),
                       "按我已保存的偏好，你应该怎么称呼我？只回答称呼，不要调用工具。", verify=nickname_recall)

        await self.compaction_case()
        await self.web_case()

    async def compaction_case(self):
        runner = self.runner(compact=True)
        session = await runner.conversations.create_session(self.workspace, "compaction fixture")
        anchor = "COMPACT-" + self.nonce
        for index in range(4):
            run_id = await runner.runs.begin(session, UserMessage(
                f"受控历史样本 {index}。发布目标是 staging，禁止删除文件，验收暗号为 {anchor}。\n" + "示例日志条目：运行正常。\n" * 80, 0))
            async with self.database.transaction() as connection:
                await runner.runs._message(connection, session, run_id, AssistantMessage(
                    [TextContent("已记录这些受控样本约束；没有执行文件修改。")], self.selected.api,
                    self.selected.provider, self.selected.model_id, 0, "stop"))
            await runner.runs.finish(run_id, "completed")

        async def compact(result, events, rows):
            answer = text(result.message)
            return {"compaction_committed": any(event["type"] == "compaction_completed" for event in events),
                    "anchor_preserved": anchor in answer, "target_preserved": "staging" in answer,
                    "constraint_preserved": "删除" in answer}, {}

        await self.run("real_compaction", runner,
                       "仅根据历史回答发布目标、禁止的操作和验收暗号，不要调用工具。", session=session, verify=compact)

    async def tool_stop_case(self):
        class FinishTool(EchoTool):
            def __init__(self):
                super().__init__()
                self.name = "finish_test"
                self.description = "Return the supplied text and request normal termination of this run."

            async def execute(self, *args, **kwargs):
                result = await super().execute(*args, **kwargs)
                result.terminate = True
                return result

        runner = self.runner()
        runner.tools.registry.register(FinishTool())
        started = len(self.client.calls)

        async def verify(result, events, rows):
            return {"normal_tool_stop": result.end_reason == "tool_requested_stop", "no_final_model_reply": result.message is None,
                    "one_provider_call": len(self.client.calls) == started + 1,
                    "tool_result_returned": len(result.tool_results) == 1 and result.tool_results[0].content[0].text == "done"}, {}

        await self.run("normal_tool_stop", runner,
                       "必须调用 finish_test 工具，参数 text 为 done。不要调用其他工具。", verify=verify)

    async def web_case(self):
        # ASGI exercises the real web handlers and durable event replay. Its
        # in-process transport buffers SSE; it does not measure browser TTFT.
        web_runner = self.runner()
        runtime = WebRuntime(web_runner, self.workspace)
        app = create_app(workspace=self.workspace, injected_runtime=runtime)
        first = len(self.client.calls)
        started = perf_counter()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as http:
                session_response = await http.post("/api/sessions", json={"title": "live-web"})
                session_response.raise_for_status()
                session_id = session_response.json()["id"]
                response = await http.post(f"/api/sessions/{session_id}/runs", json={"text": "只回答 WEB_LIVE_OK，不要调用工具。"})
                response.raise_for_status()
                run_id = response.json()["run_id"]
                stream = await http.get(f"/api/runs/{run_id}/events")
                status = await http.get(f"/api/runs/{run_id}")
                replay = await http.get(f"/api/runs/{run_id}/events")
        deltas = []
        for block in stream.text.split("\n\n"):
            if "event: update" not in block:
                continue
            data = "\n".join(line[6:] for line in block.splitlines() if line.startswith("data: "))
            event = json.loads(data)
            if event["type"] == "text_delta":
                deltas.append(event["payload"]["delta"])
        answer = "".join(deltas)
        checks = {"run_completed": status.json()["status"] == "completed", "sse_terminal": "event: end" in stream.text,
                  "sse_answer": answer.strip() == "WEB_LIVE_OK", "durable_replay": stream.text == replay.text,
                  "model_stop_reason": status.json()["end_reason"] == "model_stop"}
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT name, arguments, status, call_index FROM tool_calls "
                "WHERE run_id = %s ORDER BY created_at, call_index", (run_id,))
            tool_rows = await cursor.fetchall()
        provider_calls = self.client.calls[first:]
        record = {"name": "web_api_sse_replay", "passed": all(checks.values()), "checks": checks,
                  "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                  "answer": answer, "sse_delta_count": len(deltas),
                  "model_calls": len(provider_calls), **summarize_tool_calls(tool_rows),
                  "provider_calls": provider_calls}
        self.results.append(record)
        summary = {"case": record["name"], "passed": record["passed"],
                   "model_calls": record["model_calls"], "tool_calls": record["tool_calls"],
                   "repeated_tool_calls": record["repeated_tool_calls"], "checks": checks}
        if not record["passed"]:
            summary["tool_sequence"] = record["tool_sequence"]
        print(json.dumps(summary, ensure_ascii=False), flush=True)

    async def events_case(self):
        first_call = len(self.client.calls)

        async def verify(result, events, rows):
            deltas = [event["payload"]["delta"] for event in events if event["type"] == "text_delta"]
            raw = sum(call["text_deltas"] for call in self.client.calls[first_call:])
            return {"exact_stream_reconstruction": "".join(deltas) == text(result.message),
                    "nonempty_answer": len(text(result.message)) >= 100,
                    "one_provider_request": len(self.client.calls) == first_call + 1,
                    "no_tools": not any(event["type"] == "tool_started" for event in events),
                    "not_more_text_transactions_than_deltas": 0 < len(deltas) <= raw}, {
                    "provider_text_deltas": raw, "persisted_text_events": len(deltas)}

        await self.run("real_text_stream_coalescing", self.runner(),
                       "不要调用工具。用简体中文分六条说明如何检查一份测试报告，每条约四十字。只输出正文。", verify=verify)
        await self.web_case()

    async def scheduling_case(self):
        runner = self.runner()
        values = [13, 29, 47]
        markers = [uuid4().hex[:12] for _ in values]
        for index, (value, marker) in enumerate(zip(values, markers)):
            (self.workspace / f"batch-{index}.json").write_text(
                json.dumps({"value": value, "marker": marker}), encoding="utf-8")
        active = peak = 0
        lock = threading.Lock()
        read_tool = runner.tools.registry.get("read_file")
        original = read_tool.handle

        def observed_read(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                # Controlled I/O latency makes real worker overlap observable.
                sleep(.05)
                return original(*args, **kwargs)
            finally:
                with lock:
                    active -= 1

        read_tool.handle = observed_read

        async def verify(result, events, rows):
            try:
                saved = json.loads((self.workspace / "batch-result.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = {}
            async with self.database.connection() as connection:
                cursor = await connection.execute(
                    "SELECT model_call_id, call_id, name, status, call_index FROM tool_calls WHERE run_id = %s ORDER BY created_at, call_index",
                    (result.run_id,))
                calls = await cursor.fetchall()
            read_groups = {}
            for call in calls:
                if call["name"] == "read_file":
                    read_groups.setdefault(str(call["model_call_id"]), []).append(call)
            completed = [event["payload"]["tool_call_id"] for event in events if event["type"] == "tool_completed"]
            queued = [event["payload"]["tool_call_id"] for event in events if event["type"] == "tool_queued"]
            return {"correct_sum": saved.get("sum") == sum(values), "hidden_markers": saved.get("markers") == markers,
                    "same_response_three_reads": any(len(group) == 3 for group in read_groups.values()),
                    "read_workers_overlap": peak >= 2, "ordered_results": completed == queued,
                    "all_calls_completed": bool(calls) and all(call["status"] == "completed" for call in calls),
                    "no_remaining_workers": active == 0}, {"read_worker_peak": peak, "result_file": saved,
                    "injected_read_latency_ms": 50, "tool_batch_sizes": [len(group) for group in read_groups.values()]}

        await self.run("real_parallel_read_then_write", runner,
                       '必须在同一个 assistant 响应中发出三个独立的 read_file 调用，分别读取 batch-0.json、batch-1.json、batch-2.json。'
                       '这三个读取没有依赖，应当一起提交。拿到全部结果后，将三个 value 的和及按文件编号排序的 marker 原值数组'
                       '用 write_file 创建 batch-result.json，格式为 {"sum":数值,"markers":[三个原值]}。'
                       '只操作这四个文件，最后简短报告结果。', verify=verify)

    async def explicit_case(self):
        runner = self.runner(memory=True)

        async def verify(result, events, rows):
            session = await runner.conversations.get_session(result.session_id)
            memory = await self.memory.current("workspace", session.workspace_id, "project.response_language")
            return {"active_memory_saved": memory is not None and "简体中文" in memory.content,
                    "no_unnecessary_file_tools": not any(event["type"] == "tool_started" for event in events)}, {}

        await self.run("explicit_memory_nonsecret", runner,
                       "/remember workspace project.response_language = 本项目默认使用简体中文回答", verify=verify)


async def main_async(args):
    configured = Settings.from_env()
    selected = replace(ModelSettings.from_env(), max_tokens=768, request_timeout=40, run_timeout=90)
    schema = "chagent_live_" + uuid4().hex
    report = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "configuration": {"provider": selected.provider, "model": selected.model_id, "api": selected.api,
                                "output_limit": selected.max_tokens, "max_provider_requests": args.max_calls,
                                "key_source": "CHAGENT_API_KEY" if os.environ.get("CHAGENT_API_KEY", "").strip()
                                else "named provider key", "case_selection": args.case},
              "environment": {"python": platform.python_version(), "platform": platform.system()},
              "limits": "single sample per scenario; synthetic data; no browser TTFT or comparative Pi/Codex benchmark",
              "results": [], "schema_cleaned": False}
    admin = await AsyncConnection.connect(configured.database_url, autocommit=True, connect_timeout=5)
    observed = None
    try:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            async with ObservedDatabase(replace(configured, database_url=make_conninfo(
                    configured.database_url, options=f"-c search_path={schema}"))) as database:
                await migrate(database)
                async with create_model_client(selected) as client:
                    observed = MeasuredClient(client, max_calls=args.max_calls)
                    with tempfile.TemporaryDirectory(prefix="chagent-live-") as directory:
                        suite = LiveSuite(database, selected, observed, Path(directory).resolve())
                        report["results"] = suite.results
                        try:
                            if args.case == "all":
                                await suite.execute()
                            elif args.case == "core":
                                await suite.execute(core_only=True)
                            elif args.case == "events":
                                await suite.events_case()
                            elif args.case == "scheduling":
                                await suite.scheduling_case()
                            elif args.case == "web":
                                await suite.web_case()
                            else:
                                await suite.explicit_case()
                        except Exception as error:
                            # Do not serialize arbitrary exception strings or provider bodies.
                            report["suite_error_type"] = type(error).__name__
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            report["schema_cleaned"] = True
    finally:
        await admin.close()
        calls = observed.calls if observed else []
        report["total_provider_requests"] = len(calls)
        report["total_model_calls"] = sum(case.get("model_calls", 0) for case in report["results"])
        report["total_tool_calls"] = sum(case.get("tool_calls", 0) for case in report["results"])
        report["total_repeated_tool_calls"] = sum(case.get("repeated_tool_calls", 0) for case in report["results"])
        report["reported_tokens"] = {key: sum((call["usage"] or {}).get(key, 0) for call in calls)
                                      for key in ("input", "output", "cache_read", "cache_write", "total_tokens")}
        report["usage_missing_calls"] = sum(call["usage"] is None for call in calls)
        report["monetary_cost"] = None
        report["monetary_cost_note"] = "No monetary price assumed; see provider-reported token usage."
        expected = 9 if args.case == "all" else 6 if args.case == "core" else 2 if args.case == "events" else 1
        report["passed"] = len(report["results"]) == expected and all(case["passed"] for case in report["results"]) and "suite_error_type" not in report
        root = Path(__file__).resolve().parents[1]
        report["source_sha256"] = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in (root / "src/chagent/agent/runner.py", root / "src/chagent/memory/service.py",
                                                root / "src/chagent/models/openai_chat.py", root / "src/chagent/context/manager.py",
                                                root / "src/chagent/tools/scheduler.py", root / "src/chagent/tools/executor.py",
                                                root / "src/chagent/tools/filesystem.py", root / "src/chagent/agent/stream_writer.py",
                                                root / "src/chagent/events/notifications.py", root / "src/chagent/web/app.py")}
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        assert selected.api_key not in serialized, "Refusing to save a credential-bearing report"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=24)
    parser.add_argument("--case", choices=("all", "core", "web", "explicit", "scheduling", "events"), default="all")
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 30:
        parser.error("--max-calls must be between 1 and 30")
    report = asyncio.run(main_async(args))
    print(json.dumps({"passed": report["passed"], "requests": report["total_provider_requests"],
                      "reported_tokens": report["reported_tokens"], "schema_cleaned": report["schema_cleaned"],
                      "report": str(args.output.resolve())}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
