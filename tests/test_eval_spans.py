"""P1 timeline: span projection and the timing arithmetic that must not lie.

The deterministic half needs no database: it builds the row shapes ``_build``
consumes and asserts the invariants the plan names — concurrent tools are not
charged twice, approval wait is not subtracted from execution it already sits
outside of, speculative work overlaps rather than adds, post-turn work stays
outside the run's occupancy, and no consumer can ever see negative framework
time.

The database half inserts the same shapes through real SQL, because the joins
and the payload matching (``tool_call_id`` is a provider string on tool events
and a row UUID on approval events) are exactly where a projection like this
silently attaches the wrong evidence.
"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4

from eval.spans import _build, format_timeline

REPOSITORY = Path(__file__).resolve().parent.parent
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def run_row(*, start: float = 0, end: float | None = 10, status: str = "completed",
            end_reason: str | None = "model_stop", root_path: str | None = None) -> dict:
    return {
        "id": uuid4(), "status": status, "end_reason": end_reason,
        "created_at": at(start), "started_at": at(start),
        "ended_at": at(end) if end is not None else None,
        "session_id": uuid4(), "root_path": root_path,
    }


def model_row(iteration: int, start: float, end: float | None, *, tokens=None,
              status: str = "completed", duration_ms=None) -> dict:
    return {"id": uuid4(), "iteration": iteration, "status": status,
            "created_at": at(start), "ended_at": at(end) if end is not None else None,
            "total_tokens": tokens, "duration_ms": duration_ms}


def tool_row(call_id: str, name: str, queued: float, ended: float | None, *,
             status: str = "completed", attempt: int = 1, call_index: int = 0,
             iteration: int = 0) -> dict:
    return {"id": uuid4(), "call_id": call_id, "name": name, "status": status,
            "attempt": attempt, "call_index": call_index, "iteration": iteration,
            "created_at": at(queued), "ended_at": at(ended) if ended is not None else None}


def event(sequence: int, kind: str, when: float, payload: dict) -> dict:
    return {"sequence": sequence, "type": kind, "created_at": at(when), "payload": payload}


def rows(run: dict, *, models=(), tools=(), approvals=(), compactions=None,
         memory=(), events=(), first_text=None) -> dict:
    return {"run": run, "models": list(models), "tools": list(tools),
            "approvals": list(approvals), "compactions": compactions or {},
            "memory": list(memory), "events": list(events), "first_text": first_text}


def build(**kwargs) -> dict:
    data = rows(**kwargs)
    return _build(data["run"]["id"], data)


class OccupancyTests(unittest.TestCase):
    """Wall, cumulative and unexplained, checked against hand-computed numbers."""

    def test_run_span_is_the_window_and_never_the_occupancy(self):
        """Regression: counting the run span made unexplained time zero by construction."""
        document = build(run=run_row())
        accounting = document["accounting"]
        self.assertEqual(accounting["window"]["wall_ms"], 10_000)
        self.assertEqual(accounting["union_ms"], 0)
        self.assertEqual(accounting["cumulative_ms"], 0)
        self.assertEqual(accounting["unexplained_ms"], 10_000)

    def test_overlapping_tools_are_counted_once_in_occupancy(self):
        document = build(
            run=run_row(),
            tools=[tool_row("a", "exec_command", 1, 4), tool_row("b", "exec_command", 2, 5)],
            events=[
                event(1, "tool_started", 1, {"tool_call_id": "a", "name": "exec_command"}),
                event(2, "tool_started", 2, {"tool_call_id": "b", "name": "exec_command"}),
            ])
        accounting = document["accounting"]
        # 3 s + 3 s of calls inside a 4 s window: the sum is allowed to exceed
        # the wall, the union is not allowed to exceed it.
        self.assertEqual(accounting["by_kind"]["tool"]["cumulative_ms"], 6_000)
        self.assertEqual(accounting["by_kind"]["tool"]["union_ms"], 4_000)
        self.assertEqual(accounting["cumulative_ms"], 6_000)
        self.assertEqual(accounting["union_ms"], 4_000)
        self.assertEqual(accounting["concurrency"]["max_overlapping"], 2)
        self.assertEqual(accounting["unexplained_ms"], 6_000)

    def test_three_parallel_tools_do_not_inflate_wall_occupancy(self):
        document = build(
            run=run_row(),
            tools=[tool_row("a", "create_file", 1, 3), tool_row("b", "create_file", 1, 3),
                   tool_row("c", "create_file", 1, 3)],
            events=[event(index, "tool_started", 1, {"tool_call_id": key, "name": "create_file"})
                    for index, key in enumerate("abc", start=1)])
        accounting = document["accounting"]
        self.assertEqual(accounting["by_kind"]["tool"]["cumulative_ms"], 6_000)
        self.assertEqual(accounting["by_kind"]["tool"]["union_ms"], 2_000)
        self.assertEqual(accounting["concurrency"]["max_overlapping"], 3)
        self.assertLessEqual(accounting["union_ms"], accounting["window"]["wall_ms"])

    def test_touching_intervals_are_not_concurrent(self):
        document = build(
            run=run_row(),
            tools=[tool_row("a", "read_file", 1, 3), tool_row("b", "read_file", 3, 5)],
            events=[
                event(1, "tool_started", 1, {"tool_call_id": "a"}),
                event(2, "tool_started", 3, {"tool_call_id": "b"}),
            ])
        self.assertEqual(document["accounting"]["concurrency"]["max_overlapping"], 1)
        self.assertEqual(document["accounting"]["union_ms"], 4_000)

    def test_speculative_compaction_overlaps_the_answer(self):
        run = run_row()
        compaction_id = str(uuid4())
        document = build(
            run=run,
            models=[model_row(0, 1, 9)],
            compactions={compaction_id: {"compaction_id": compaction_id, "calls": 1,
                                         "duration_ms": 6000, "status": "completed"}},
            events=[
                event(1, "compaction_started", 2, {"compaction_id": compaction_id,
                                                   "speculative": True}),
                event(2, "compaction_completed", 8, {"compaction_id": compaction_id,
                                                     "speculative": True}),
            ])
        accounting = document["accounting"]
        self.assertEqual(accounting["by_kind"]["compaction"]["union_ms"], 6_000)
        self.assertEqual(accounting["by_kind"]["model"]["union_ms"], 8_000)
        # The compaction runs inside the model call: 8 s of coverage, not 14 s.
        self.assertEqual(accounting["union_ms"], 8_000)
        span = next(span for span in document["spans"] if span["kind"] == "compaction")
        self.assertTrue(span["attributes"]["speculative"])
        self.assertEqual(span["timing_precision"], "transaction_time")

    def test_post_turn_memory_stays_outside_the_run_window(self):
        run = run_row()
        document = build(
            run=run,
            memory=[{"id": uuid4(), "status": "failed", "created_at": at(11),
                     "started_at": at(12), "ended_at": at(20), "attempts": 3,
                     "max_attempts": 3}])
        accounting = document["accounting"]
        self.assertEqual(accounting["union_ms"], 0)
        self.assertEqual(accounting["outside_window_ms"], 8_000)
        # A failed background job after the answer is not part of the answer.
        self.assertEqual(accounting["unexplained_ms"], 10_000)
        memory = next(span for span in document["spans"] if span["kind"] == "memory")
        self.assertIsNone(memory["parent_id"])
        self.assertEqual(memory["links"], [{"kind": "caused_by", "type": "run",
                                            "run_id": memory["run_id"]}])

    def test_negative_interval_is_repaired_and_never_reaches_the_arithmetic(self):
        document = build(
            run=run_row(),
            tools=[tool_row("a", "exec_command", 1, 4, status="failed")],
            events=[event(1, "tool_started", 5, {"tool_call_id": "a"})])
        span = next(span for span in document["spans"] if span["kind"] == "tool")
        self.assertEqual(span["duration_ms"], 0)
        self.assertIn("negative interval repaired to zero: end preceded start", span["warnings"])
        self.assertEqual(document["accounting"]["unexplained_ms"], 10_000)
        self.assertTrue(any(item["item"] == "negative intervals repaired" and item["count"] == 1
                            for item in document["missing"]))

    def test_unexplained_time_is_never_negative_across_shapes(self):
        cases = [
            dict(run=run_row()),
            dict(run=run_row(), models=[model_row(0, 0, 10)]),
            dict(run=run_row(), models=[model_row(0, 0, 12)]),  # ends after the window
            dict(run=run_row(), tools=[tool_row("a", "x", 0, 10)], events=[
                event(1, "tool_started", -2, {"tool_call_id": "a"})]),  # starts before it
            dict(run=run_row(end=None), models=[model_row(0, 1, 5)]),
        ]
        for case in cases:
            with self.subTest(case=sorted(case)):
                accounting = build(**case)["accounting"]
                self.assertGreaterEqual(accounting["unexplained_ms"], 0)
                self.assertLessEqual(accounting["union_ms"], accounting["window"]["wall_ms"])


class EvidenceTests(unittest.TestCase):
    """Spans keep what is known apart from what is assumed."""

    def test_missing_start_is_null_and_not_backfilled_from_enqueue_time(self):
        document = build(
            run=run_row(),
            tools=[tool_row("a", "exec_command", 1, 4)])  # no tool_started event
        span = next(span for span in document["spans"] if span["kind"] == "tool")
        self.assertIsNone(span["started_at"])
        self.assertIsNone(span["duration_ms"])
        self.assertIsNone(span["wait_ms"])
        self.assertEqual(span["timing_precision"], "unknown")
        self.assertEqual(document["accounting"]["union_ms"], 0)
        self.assertTrue(any(item["item"] == "tool spans with no start event" and item["count"] == 1
                            for item in document["missing"]))

    def test_queue_time_is_reported_as_wait_not_as_execution(self):
        document = build(
            run=run_row(),
            tools=[tool_row("a", "exec_command", 1, 4)],
            events=[event(1, "tool_started", 3, {"tool_call_id": "a"})])
        span = next(span for span in document["spans"] if span["kind"] == "tool")
        self.assertEqual(span["wait_ms"], 2_000)
        self.assertEqual(span["duration_ms"], 1_000)
        self.assertEqual(span["timing_source"], "tool_event")

    def test_approval_wait_is_its_own_span(self):
        run = run_row()
        tool = tool_row("a", "exec_command", 1, 4, status="failed")
        approval_id = uuid4()
        document = build(
            run=run,
            tools=[tool],
            approvals=[{"id": approval_id, "tool_record_id": tool["id"], "tool_name": "exec_command",
                        "status": "denied", "created_at": at(1), "decided_at": at(3),
                        "batch_id": None, "batch_index": None}],
            events=[
                event(1, "tool_queued", 1, {"tool_call_id": "a", "name": "exec_command"}),
                event(2, "approval_requested", 1, {"approval_id": str(approval_id),
                                                   "tool_call_id": str(tool["id"])}),
                event(3, "tool_started", 3, {"tool_call_id": "a"}),
            ])
        tool_span = next(span for span in document["spans"] if span["kind"] == "tool")
        approval = next(span for span in document["spans"] if span["kind"] == "approval")
        self.assertEqual(approval["duration_ms"], 2_000)
        self.assertEqual(tool_span["duration_ms"], 1_000)
        self.assertEqual(tool_span["wait_ms"], 2_000)
        # Not nested: nesting would make the run look 2 s longer than it was.
        self.assertIsNone(approval["parent_id"])
        self.assertIn({"kind": "gates", "type": "tool", "tool_record_id": str(tool["id"])},
                      approval["links"])
        self.assertIn({"kind": "waits_for", "type": "approval", "approval_id": str(approval_id)},
                      tool_span["links"])
        self.assertEqual(document["accounting"]["by_kind"]["approval"]["union_ms"], 2_000)

    def test_undecided_approval_is_open_not_zero(self):
        run = run_row()
        tool = tool_row("a", "write_file", 1, None, status="queued")
        document = build(
            run=run, tools=[tool],
            approvals=[{"id": uuid4(), "tool_record_id": tool["id"], "tool_name": "write_file",
                        "status": "pending", "created_at": at(1), "decided_at": None,
                        "batch_id": None, "batch_index": None}])
        approval = next(span for span in document["spans"] if span["kind"] == "approval")
        self.assertIsNone(approval["execution_ended_at"])
        self.assertIsNone(approval["duration_ms"])
        self.assertTrue(approval["open"])
        self.assertTrue(any(item["item"] == "undecided approvals" for item in document["missing"]))

    def test_compaction_rows_alone_yield_duration_without_an_interval(self):
        compaction_id = str(uuid4())
        document = build(
            run=run_row(),
            compactions={compaction_id: {"compaction_id": compaction_id, "calls": 2,
                                         "duration_ms": 900, "status": "completed"}})
        span = next(span for span in document["spans"] if span["kind"] == "compaction")
        self.assertIsNone(span["started_at"])
        self.assertIsNone(span["execution_ended_at"])
        self.assertEqual(span["timing_source"], "compaction_duration")
        self.assertEqual(span["timing_precision"], "duration_only")
        self.assertEqual(span["attributes"]["duration_ms"], 900)
        self.assertEqual(document["accounting"]["union_ms"], 0)
        self.assertTrue(any(item["item"] == "compactions anchored by duration only" and item["count"] == 1
                            for item in document["missing"]))

    def test_discarded_speculation_is_marked_and_never_counted_as_a_summary(self):
        compaction_id = str(uuid4())
        document = build(
            run=run_row(),
            compactions={compaction_id: {"compaction_id": compaction_id, "calls": 1,
                                         "duration_ms": 100, "status": "failed"}},
            events=[
                event(1, "compaction_started", 2, {"compaction_id": compaction_id,
                                                   "speculative": True}),
                event(2, "compaction_discarded", 5, {"compaction_id": compaction_id,
                                                     "reason": "invalidated"}),
            ])
        span = next(span for span in document["spans"] if span["kind"] == "compaction")
        self.assertEqual(span["status"], "discarded")
        self.assertIn("discarded: invalidated", " ".join(span["warnings"]))
        self.assertEqual(span["duration_ms"], 3_000)

    def test_provider_duration_disagreement_is_flagged(self):
        document = build(
            run=run_row(),
            models=[model_row(0, 1, 5, duration_ms=200)])
        span = next(span for span in document["spans"] if span["kind"] == "model")
        self.assertTrue(any("provider duration" in warning for warning in span["warnings"]))
        quiet = build(run=run_row(), models=[model_row(0, 1, 5, duration_ms=4010)])
        self.assertFalse(any("provider duration" in warning
                             for warning in next(s for s in quiet["spans"] if s["kind"] == "model")["warnings"]))

    def test_tokens_come_from_the_recorded_usage(self):
        document = build(run=run_row(), models=[model_row(0, 1, 5, tokens=1234)])
        span = next(span for span in document["spans"] if span["kind"] == "model")
        self.assertEqual(span["tokens"], 1234)

    def test_document_is_json_serialisable_and_ordered(self):
        document = build(
            run=run_row(),
            models=[model_row(1, 4, 6), model_row(0, 1, 3)],
            tools=[tool_row("a", "exec_command", 3, 4)])
        encoded = json.loads(json.dumps(document))
        starts = [span["started_at"] for span in encoded["spans"] if span["started_at"]]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(encoded["timeline_version"], 2)
        self.assertIsNone(encoded["accounting"]["critical_path"])

    def test_workspace_paths_are_projected_out_of_names(self):
        workspace = Path("/tmp/chagent-eval-workspace")
        run = run_row(root_path=str(workspace))
        tool = tool_row("a", f"exec_command {workspace}/secret", 1, 4)
        document = build(run=run, tools=[tool],
                         events=[event(1, "tool_started", 1, {"tool_call_id": "a"})])
        blob = json.dumps(document)
        self.assertNotIn(str(workspace), blob)
        self.assertIn("/workspace/secret", blob)


class GapRegressionTests(unittest.TestCase):
    def test_reused_provider_id_does_not_join_different_iterations(self):
        document = build(
            run=run_row(),
            tools=[tool_row('same', 'exec_command', 1, 2, iteration=0),
                   tool_row('same', 'exec_command', 5, 6, iteration=1)],
            events=[event(1, 'tool_started', 1, {'tool_call_id': 'same'}),
                    event(2, 'tool_completed', 2, {'tool_call_id': 'same'}),
                    event(3, 'tool_started', 5, {'tool_call_id': 'same'}),
                    event(4, 'tool_completed', 6, {'tool_call_id': 'same'})])
        tools = [s for s in document['spans'] if s['kind'] == 'tool']
        self.assertEqual([s['duration_ms'] for s in tools], [1000, 1000])
        self.assertEqual(document['accounting']['union_ms'], 2000)
        self.assertIn({'kind': 'event', 'type': 'tool_completed', 'sequence': 4}, tools[1]['links'])

    def test_ambiguous_legacy_ids_stay_unknown(self):
        document = build(run=run_row(),
                         tools=[tool_row('same', 'exec_command', 1, 5),
                                tool_row('same', 'exec_command', 1, 5, iteration=1)],
                         events=[event(1, 'tool_started', 2, {'tool_call_id': 'same'})])
        tools = [s for s in document['spans'] if s['kind'] == 'tool']
        self.assertTrue(all(s['started_at'] is None for s in tools))
        self.assertEqual(document['accounting']['union_ms'], 0)
        self.assertTrue(any(x['item'] == 'tool spans with no start event' and x['count'] == 2
                            for x in document['missing']))

    def test_record_identity_disambiguates_same_transaction(self):
        tools = [tool_row('same', 'exec_command', 1, 5),
                 tool_row('same', 'exec_command', 1, 5, iteration=1)]
        document = build(run=run_row(), tools=tools,
                         events=[event(i, 'tool_started', 2 + i,
                                       {'tool_call_id': 'same', 'tool_record_id': str(t['id'])})
                                 for i, t in enumerate(tools, 1)])
        self.assertEqual([s['duration_ms'] for s in document['spans'] if s['kind'] == 'tool'],
                         [2000, 1000])

    def test_event_only_compactions_cover_all_terminal_paths(self):
        for status in ('completed', 'discarded', 'failed', 'cancelled'):
            with self.subTest(status=status):
                identity = str(uuid4())
                document = build(run=run_row(), events=[
                    event(1, 'compaction_started', 1, {'compaction_id': identity}),
                    event(2, f'compaction_{status}', 3, {'compaction_id': identity})])
                span = next(s for s in document['spans'] if s['kind'] == 'compaction')
                self.assertEqual(span['status'], status)
                self.assertEqual(span['duration_ms'], 2000)
                self.assertFalse(span['open'])
                self.assertEqual(span['source_record_ref']['sequences'], [1, 2])
                self.assertEqual(document['accounting']['union_ms'], 2000)

    def test_event_only_speculation_is_linked_and_pending_work_visible(self):
        identity = str(uuid4())
        document = build(run=run_row(), events=[event(
            1, 'compaction_started', 1, {'compaction_id': identity, 'speculative': True})])
        span = next(s for s in document['spans'] if s['kind'] == 'compaction')
        self.assertIsNone(span['parent_id'])
        self.assertEqual(span['links'][-1]['kind'], 'caused_by')
        self.assertTrue(span['open'])
        self.assertEqual(document['accounting']['by_kind']['compaction']['open'], 1)

    def test_cross_window_accounting_and_text_identify_partial_outside_work(self):
        document = build(run=run_row(), memory=[
            {'id': uuid4(), 'status': 'completed', 'created_at': at(8), 'started_at': at(8),
             'ended_at': at(12), 'attempts': 1, 'max_attempts': 3}])
        accounting = document['accounting']
        self.assertEqual(accounting['union_ms'], 2000)
        self.assertEqual(accounting['outside_window_ms'], 2000)
        self.assertEqual(accounting['cumulative_ms'], 4000)
        self.assertIn('outside the run window', format_timeline(document))

    def test_running_tool_is_counted_without_fabricating_occupancy(self):
        document = build(run=run_row(end=None, status='running'),
                         tools=[tool_row('a', 'exec_command', 1, None, status='running')],
                         events=[event(1, 'tool_started', 2, {'tool_call_id': 'a'})])
        group = document['accounting']['by_kind']['tool']
        self.assertEqual((group['spans'], group['open'], group['union_ms']), (1, 1, 0))

    def test_duration_only_terminal_is_not_an_open_interval(self):
        identity = str(uuid4())
        document = build(run=run_row(), compactions={identity: {
            'calls': 1, 'status': 'completed', 'duration_ms': 900}})
        span = next(s for s in document['spans'] if s['kind'] == 'compaction')
        self.assertFalse(span['open'])
        self.assertEqual(span['duration_ms'], 900)
        self.assertEqual(document['accounting']['cumulative_ms'], 900)
        self.assertEqual(document['accounting']['union_ms'], 0)

    def test_fractional_millisecond_union_stays_inside_wall(self):
        document = build(run=run_row(end=.0022), models=[
            model_row(0, 0, .0006), model_row(1, .0008, .0014),
            model_row(2, .0016, .0022)])
        accounting = document['accounting']
        self.assertLessEqual(accounting['union_ms'], accounting['window']['wall_ms'])
        self.assertEqual(accounting['union_ms'], 2)

    def test_warning_paths_and_credentials_are_filtered_with_or_without_root(self):
        from chagent.boundaries import protected_source
        for root in ('/tmp/p1-private-workspace', None):
            with self.subTest(root=root):
                identity = str(uuid4())
                reason = (f'{root}/private.txt {protected_source()}/private.py '
                          'token=sk-test-abcdefghijklmnop password=mySecret '
                          'postgresql://alice:secret@db/private')
                document = build(run=run_row(root_path=root), events=[
                    event(1, 'compaction_started', 1, {'compaction_id': identity}),
                    event(2, 'compaction_discarded', 2, {'compaction_id': identity, 'reason': reason})])
                for blob in (json.dumps(document), format_timeline(document)):
                    for secret in ('sk-test-', 'mySecret', 'alice:secret', str(protected_source())):
                        self.assertNotIn(secret, blob)
                    if root:
                        self.assertNotIn(root, blob)
                        self.assertNotIn(str(Path(root).resolve()), blob)

    def test_no_commit_instant_or_provider_bound_is_fabricated(self):
        document = build(run=run_row(), models=[model_row(0, 1, 3)], first_text=at(2))
        self.assertTrue(all(s['committed_at'] is None for s in document['spans']))
        self.assertIn('transaction timestamp', document['stream']['first_text_delta_kind'])
        self.assertTrue(any(x['item'] == 'actual commit timestamps' for x in document['missing']))

    def test_approval_fixture_actually_exercises_the_overwrite_gate(self):
        import hashlib
        import shutil
        import tempfile
        from eval import casefile
        from chagent.tools.gate import grade_write

        case = casefile.load(REPOSITORY / 'eval/cases/execution-boundary-approval-gated-write.json')
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            shutil.copytree(REPOSITORY / case.fixture['path'], workspace, dirs_exist_ok=True)
            original = (workspace / 'data.txt').read_bytes()
            self.assertEqual(original, b'before approval\n')
            self.assertIn(hashlib.sha256(original).hexdigest(), case.fixture['notes'])
            self.assertEqual(grade_write('write_file', {'path': 'data.txt', 'overwrite': True},
                                         workspace).reason, 'overwrite')
        self.assertNotIn('tool_calls.started_at', json.dumps(case.to_json()))
        self.assertIn('pre-decision file snapshot', case.graders[1]['evidence'])


class RenderingTests(unittest.TestCase):
    """The CLI text has to carry the caveats, not only the numbers."""

    def document(self) -> dict:
        return build(
            run=run_row(),
            models=[model_row(0, 1, 5, tokens=10)],
            tools=[tool_row("a", "exec_command", 5, 6)],
            events=[event(1, "tool_started", 5, {"tool_call_id": "a"})],
            first_text=at(2))

    def test_text_output_states_accounting_gaps_and_missing(self):
        text = format_timeline(self.document())
        self.assertIn("accounting  cumulative", text)
        self.assertIn("unexplained", text)
        self.assertIn("critical path  not claimed", text)
        self.assertIn("missing evidence", text)
        self.assertIn("browser receipt time (TTFT)", text)
        self.assertIn("first visible text", text)
        self.assertNotIn("None", text)

    def test_text_output_truncates_long_runs_without_hiding_the_count(self):
        tools = [tool_row(f"c{index}", "read_file", 1 + index, 1.5 + index)
                 for index in range(12)]
        text = format_timeline(build(run=run_row(), tools=tools), limit=5)
        # 12 tools plus the run span: five shown, eight accounted for.
        self.assertIn("… 8 more span(s)", text)

    def test_warnings_are_grouped_not_repeated(self):
        tools = [tool_row(f"c{index}", "read_file", 1, 2) for index in range(4)]
        text = format_timeline(build(run=run_row(), tools=tools))
        section = text.split("span warnings", 1)[1]
        self.assertIn("[4x] no unambiguous tool_started event: execution start is unknown, not zero", section)
        # One aggregated line, not four.
        self.assertEqual(section.count("e.g. tool:"), 1)


class CaseFixtureTests(unittest.TestCase):
    """The first case batch is a declaration; it has to at least be valid."""

    def test_shipped_cases_cover_the_p1_capabilities(self):
        from eval import casefile

        cases = casefile.load_all(REPOSITORY / "eval" / "cases")
        self.assertGreaterEqual(len(cases), 5)
        covered = {case.capability for case in cases}
        self.assertTrue(
            {"project-work", "execution-boundary", "multi-turn-context", "skill-selection"}
            <= covered, covered)
        for case in cases:
            # No case may read like a result. Since P4 some of them have been
            # run; those carry a machine-decidable spec, and the ones that still
            # cannot be graded by a machine have to say so in their notes.
            self.assertIsNone(getattr(case, "result", None), case.key)
            executable = [grader for grader in case.graders
                          if (grader.get("spec") or {}).get("check")]
            if not executable:
                self.assertIn("Unexecuted", case.notes, case.key)


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class ProjectionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """The joins and payload matching, against real SQL."""

    async def asyncSetUp(self):
        from psycopg import AsyncConnection, sql
        from psycopg.conninfo import make_conninfo

        from chagent.config import Settings
        from chagent.storage.database import Database
        from chagent.storage.migrate import migrate

        self.schema = "chagent_spans_" + uuid4().hex
        self.admin = await AsyncConnection.connect(
            os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self._drop)
        settings = Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}"))
        self.database = Database(settings)
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)

        from psycopg.types.json import Jsonb

        self.run_id, session_id, workspace_id = uuid4(), uuid4(), uuid4()
        model_id, tool_id, approval_id, compaction_id, memory_id = (
            uuid4(), uuid4(), uuid4(), uuid4(), uuid4())
        write_tool_id = uuid4()
        async with self.database.transaction() as connection:
            await connection.execute("INSERT INTO workspaces (id, root_path) VALUES (%s, %s)",
                                     (workspace_id, "/tmp/chagent-spans"))
            await connection.execute(
                "INSERT INTO sessions (id, workspace_id, title) VALUES (%s, %s, 'spans')",
                (session_id, workspace_id))
            await connection.execute(
                "INSERT INTO runs (id, session_id, status, end_reason, started_at, ended_at) "
                "VALUES (%s, %s, 'completed', 'model_stop', "
                "now() - interval '10 seconds', now())", (self.run_id, session_id))
            await connection.execute(
                "INSERT INTO model_calls (id, run_id, iteration, status, request, context_snapshot, "
                "response, created_at, ended_at) VALUES (%s, %s, 0, 'completed', %s, %s, %s, "
                "now() - interval '9 seconds', now() - interval '5 seconds')",
                (model_id, self.run_id, Jsonb({"model": {"id": "test"}}), Jsonb({}),
                 Jsonb({"usage": {"total_tokens": 77}, "duration_ms": 4000})))
            await connection.execute(
                "INSERT INTO tool_calls (id, run_id, model_call_id, call_id, name, arguments, "
                "status, call_index, attempt, created_at, ended_at) VALUES "
                "(%s, %s, %s, 'call-1', 'exec_command', %s, 'completed', 0, 1, "
                "now() - interval '5 seconds', now() - interval '3 seconds')",
                (tool_id, self.run_id, model_id, Jsonb({"argv": ["true"]})))
            await connection.execute(
                "INSERT INTO tool_calls (id, run_id, model_call_id, call_id, name, arguments, "
                "status, call_index, attempt, created_at, ended_at) VALUES "
                "(%s, %s, %s, 'call-2', 'write_file', %s, 'failed', 1, 1, "
                "now() - interval '3 seconds', NULL)",
                (write_tool_id, self.run_id, model_id, Jsonb({"path": "a.txt"})))
            await connection.execute(
                "INSERT INTO approval_requests (id, run_id, tool_record_id, fingerprint, tool_name, "
                "cwd, subject, status, owner_token, expires_at, decided_at, created_at) VALUES "
                "(%s, %s, %s, 'f', 'write_file', '/tmp', 'write a.txt', 'denied', 'o', "
                "now() + interval '1 minute', now() - interval '4 seconds', "
                "now() - interval '5 seconds')",
                (approval_id, self.run_id, write_tool_id))
            await connection.execute(
                "INSERT INTO compaction_calls (id, compaction_id, run_id, iteration, status, "
                "request, duration_ms) VALUES (%s, %s, %s, 0, 'completed', %s, 900)",
                (uuid4(), compaction_id, self.run_id, Jsonb({})))
            message_id = uuid4()
            await connection.execute(
                "INSERT INTO messages (id, session_id, run_id, role, payload) "
                "VALUES (%s, %s, %s, 'user', %s)",
                (message_id, session_id, self.run_id, Jsonb({"content": []})))
            await connection.execute(
                "INSERT INTO memory_jobs (id, run_id, session_id, source_message_id, prompt_version, "
                "status, started_at, ended_at, created_at) VALUES (%s, %s, %s, %s, 'v1', "
                "'completed', now() + interval '2 seconds', now() + interval '4 seconds', "
                "now() + interval '1 second')",
                (memory_id, self.run_id, session_id, message_id))

            sequence = 0
            # Explicit offsets, not defaults: ``now()`` is fixed for a whole
            # transaction, so events inserted in one batch would all share a
            # timestamp and describe no ordering at all.
            for kind, payload, offset in (
                    ("run_started", {"session_id": str(session_id)}, -10),
                    ("model_started", {"model_call_id": str(model_id)}, -9),
                    ("model_completed", {"model_call_id": str(model_id)}, -5),
                    ("tool_queued", {"tool_call_id": "call-1", "name": "exec_command"}, -5),
                    ("tool_started", {"tool_call_id": "call-1", "name": "exec_command"}, -5),
                    ("tool_completed", {"tool_call_id": "call-1", "is_error": False}, -3),
                    ("approval_requested", {"approval_id": str(approval_id),
                                            "tool_call_id": str(write_tool_id)}, -5),
                    ("compaction_started", {"compaction_id": str(compaction_id),
                                            "speculative": True}, -4),
                    ("compaction_completed", {"compaction_id": str(compaction_id),
                                              "speculative": True}, -3)):
                sequence += 1
                await connection.execute(
                    "INSERT INTO run_events (run_id, sequence, type, payload, created_at) "
                    "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))",
                    (self.run_id, sequence, kind, Jsonb(payload), offset))

    async def _drop(self):
        from psycopg import sql

        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def project(self) -> dict:
        from eval.spans import SpanProjection

        return await SpanProjection(self.database).project(self.run_id)

    async def test_projection_covers_every_recorded_kind(self):
        document = await self.project()
        kinds = {span["kind"] for span in document["spans"]}
        self.assertEqual(kinds, {"run", "model", "tool", "approval", "compaction", "memory"})
        tools = [span for span in document["spans"] if span["kind"] == "tool"]
        self.assertEqual(len(tools), 2)
        started = next(span for span in tools if span["name"] == "exec_command")
        self.assertIsNotNone(started["started_at"])
        # 5 s to 3 s before now, measured from the event-backed start.
        self.assertEqual(started["duration_ms"] // 1000, 2)
        unstarted = next(span for span in tools if span["name"] == "write_file")
        self.assertIsNone(unstarted["started_at"])
        self.assertIsNone(unstarted["duration_ms"])

    async def test_approval_links_to_its_tool_row(self):
        from eval.manifests import canonical

        document = await self.project()
        approval = next(span for span in document["spans"] if span["kind"] == "approval")
        tool = next(span for span in document["spans"]
                    if span["kind"] == "tool" and span["name"] == "write_file")
        self.assertEqual(approval["links"][0]["type"], "tool")
        self.assertEqual(approval["source_record_ref"]["tool_record_id"],
                         tool["source_record_ref"]["id"])
        self.assertIn({"kind": "waits_for", "type": "approval",
                       "approval_id": approval["source_record_ref"]["id"]}, tool["links"])
        self.assertTrue(canonical(document))

    async def test_speculation_and_post_turn_work_are_excluded(self):
        document = await self.project()
        accounting = document["accounting"]
        compaction = next(span for span in document["spans"] if span["kind"] == "compaction")
        self.assertTrue(compaction["attributes"]["speculative"])
        self.assertGreater(accounting["outside_window_ms"], 0)
        memory = next(span for span in document["spans"] if span["kind"] == "memory")
        self.assertIsNone(memory["parent_id"])
        self.assertGreaterEqual(accounting["unexplained_ms"], 0)

    async def test_event_only_terminal_compactions_survive_database_projection(self):
        from psycopg.types.json import Jsonb

        identities = {status: str(uuid4()) for status in ('completed', 'discarded', 'failed', 'cancelled')}
        async with self.database.transaction() as connection:
            for index, (status, identity) in enumerate(identities.items()):
                for offset, kind in enumerate(('started', status)):
                    await connection.execute(
                        "INSERT INTO run_events (run_id, sequence, type, payload, created_at) "
                        "SELECT %s, %s, %s, %s, started_at + make_interval(secs => %s) "
                        "FROM runs WHERE id = %s",
                        (self.run_id, 20 + index * 2 + offset, 'compaction_' + kind,
                         Jsonb({'compaction_id': identity, 'speculative': status == 'discarded'}),
                         index + offset, self.run_id))
        document = await self.project()
        projected = {s['source_record_ref']['compaction_id']: s for s in document['spans']
                     if s['kind'] == 'compaction'}
        for status, identity in identities.items():
            self.assertEqual(projected[identity]['status'], status)
            self.assertEqual(projected[identity]['duration_ms'], 1000)
            self.assertEqual(projected[identity]['source_record_ref']['table'], 'run_events')

    async def test_unknown_run_is_not_found(self):
        from eval.spans import SpanProjection

        with self.assertRaises(LookupError):
            await SpanProjection(self.database).project(uuid4())

    async def test_cli_timeline_json_and_text(self):
        """End to end through the real entry point.

        A subprocess, not ``cli.main``: the command calls ``asyncio.run``, which
        cannot start inside a running loop, and this also proves the module is
        importable the way an operator runs it (``python -m eval.cli``).
        """
        url = (os.environ["CHAGENT_TEST_DATABASE_URL"]
               + f"?options=-csearch_path%3D{self.schema}")
        base = [sys.executable, "-m", "eval.cli", "timeline", str(self.run_id), "--db", url]
        environment = {**os.environ, "PYTHONPATH": "src", "NO_PROXY": "127.0.0.1,localhost"}

        done = subprocess.run(base + ["--json"], cwd=REPOSITORY, capture_output=True,
                              text=True, env=environment)
        self.assertEqual(done.returncode, 0, done.stderr)
        document = json.loads(done.stdout)
        self.assertEqual(document["run"]["id"], str(self.run_id))

        done = subprocess.run(base, cwd=REPOSITORY, capture_output=True, text=True,
                              env=environment)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("accounting  cumulative", done.stdout)
        self.assertIn("missing evidence", done.stdout)

        missing = subprocess.run(
            [sys.executable, "-m", "eval.cli", "timeline", str(uuid4()), "--db", url],
            cwd=REPOSITORY, capture_output=True, text=True, env=environment)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("run not found", missing.stderr)


if __name__ == "__main__":
    unittest.main()
