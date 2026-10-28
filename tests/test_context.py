from copy import deepcopy
from dataclasses import asdict
import json
import unittest
from uuid import uuid4

from chagent.context.builder import BasicContextBuilder, ContextBudgetError, ContextIntegrityError
from chagent.context.projection import ProjectionCache
from chagent.domain.context import ContextPolicy
from chagent.domain.messages import AssistantMessage, SystemMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
from chagent.domain.runs import StoredMessage
from chagent.domain.sessions import TaskState
from chagent.domain.tools import Tool
from chagent.models.base import Model


class RecordingCounter:
    """Same numbers as ByteTokenCounter, but records serialization volume.

    ``additive=False`` mimics a future counter (for example one that models
    inter-message structure) that cannot promise additive per-message costs.
    """

    name = "recording_estimate"

    def __init__(self, additive=True):
        self.additive = additive
        self.calls = 0
        self.serialized = 0
        self.lengths = []

    def count(self, messages):
        self.calls += 1
        self.serialized += len(messages)
        self.lengths.append(len(messages))
        return sum(len(json.dumps(asdict(message), ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 8
                   for message in messages)


class ContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.model = Model("test", "test", "fake", 100_000, 128)

    def record(self, sequence, message, run_id=None):
        return StoredMessage(uuid4(), sequence, message, run_id)

    async def test_budget_selects_complete_recent_turn_and_preserves_tool_pair(self):
        old_run, current_run = uuid4(), uuid4()
        old = [self.record(1, UserMessage("old" * 2000, 1), old_run),
               self.record(2, AssistantMessage([TextContent("old answer")], "fake", "test", "test", 2, "stop"), old_run)]
        current = [self.record(3, UserMessage("current", 3), current_run),
                   self.record(4, AssistantMessage([ToolCall("a", "echo", {"text": "x"})], "fake", "test", "test", 4, "toolUse"), current_run),
                   self.record(5, ToolResultMessage("a", "echo", [TextContent("x")], False, 5), current_run)]
        baseline = await BasicContextBuilder(policy=ContextPolicy(safety_tokens=0)).build(current, self.model, [])
        model = Model("test", "test", "fake", baseline.snapshot.token_estimate + 128 + 10, 128)
        built = await BasicContextBuilder(policy=ContextPolicy(safety_tokens=0)).build(old + current, model, [], current_run_id=current_run)
        self.assertEqual(built.snapshot.source_message_ids, [str(item.id) for item in current])
        self.assertEqual([item.reason for item in built.snapshot.selections[:2]], ["token_budget", "token_budget"])
        self.assertLessEqual(built.snapshot.token_estimate, built.snapshot.input_budget)
        self.assertEqual([message.role for message in built.request.messages], ["system", "user", "assistant", "toolResult"])

    async def test_history_cap_keeps_current_run_and_pinned_system(self):
        history = [self.record(1, SystemMessage("Keep this rule", 1)),
                   self.record(2, UserMessage("previous", 2)),
                   self.record(3, AssistantMessage([TextContent("answer")], "fake", "test", "test", 3, "stop")),
                   self.record(4, UserMessage("now", 4))]
        built = await BasicContextBuilder(policy=ContextPolicy(max_history_messages=2)).build(history, self.model, [])
        self.assertEqual(built.snapshot.source_message_ids, [str(history[0].id), str(history[3].id)])
        self.assertEqual(built.snapshot.selections[1].reason, "history_limit")

    async def test_task_constraints_and_tools_count_against_budget(self):
        history = [self.record(1, UserMessage("hello", 1))]
        state = TaskState(goal="finish task", constraints=["不要修改数据库"], pending=["verify"], revision=4)
        tools = [Tool("echo", "x" * 200, {"type": "object"})]
        builder = BasicContextBuilder()
        plain = await builder.build(history, self.model, [])
        built = await builder.build(history, self.model, tools, task_state=state)
        self.assertGreater(built.snapshot.token_estimate, plain.snapshot.token_estimate)
        self.assertIn("不要修改数据库", built.request.messages[0].content)
        self.assertEqual(built.snapshot.task_state_revision, 4)
        with self.assertRaises(ContextBudgetError):
            await builder.build(history, Model("test", "test", "fake", 300, 128), tools, task_state=state)

    async def test_host_metadata_removed_without_mutating_original_history(self):
        history = [self.record(1, UserMessage("x", 1)),
                   self.record(2, AssistantMessage([ToolCall("a", "echo", {})], "fake", "test", "test", 2, "toolUse")),
                   self.record(3, ToolResultMessage("a", "echo", [TextContent("result")], False, 3, details={"private_ui": "secret"}))]
        original = deepcopy(history)
        built = await BasicContextBuilder().build(history, self.model, [])
        self.assertIsNone(built.request.messages[-1].details)
        self.assertEqual(history, original)
        built.request.messages[-1].content[0].text = "changed"
        self.assertEqual(history[-1].message.content[0].text, "result")

    async def test_required_input_is_not_silently_trimmed(self):
        history = [self.record(1, UserMessage("约束" * 10_000, 1))]
        with self.assertRaises(ContextBudgetError):
            await BasicContextBuilder().build(history, Model("test", "test", "fake", 4096, 128), [])

    async def test_reject_incomplete_or_mismatched_tool_history(self):
        user = self.record(1, UserMessage("x", 1))
        call = self.record(2, AssistantMessage([ToolCall("a", "echo", {})], "fake", "test", "test", 2, "toolUse"))
        result = self.record(3, ToolResultMessage("a", "different", [], False, 3))
        for history in ([user, call], [user, result], [user, call, result]):
            with self.subTest(history=history), self.assertRaises(ContextIntegrityError):
                await BasicContextBuilder().build(history, self.model, [])

    async def test_current_run_is_preserved_even_when_it_exceeds_history_count_limit(self):
        run_id = uuid4()
        history = [self.record(1, UserMessage("x", 1), run_id),
                   self.record(2, AssistantMessage([ToolCall("a", "echo", {})], "fake", "test", "test", 2, "toolUse"), run_id),
                   self.record(3, ToolResultMessage("a", "echo", [], False, 3), run_id)]
        built = await BasicContextBuilder(policy=ContextPolicy(max_history_messages=1)).build(history, self.model, [], current_run_id=run_id)
        self.assertEqual(len(built.snapshot.source_message_ids), 3)

    async def test_utf8_estimate_and_invalid_limits(self):
        history = [self.record(1, UserMessage("你好", 1))]
        built = await BasicContextBuilder().build(history, self.model, [])
        self.assertEqual(built.snapshot.token_counter, "utf8_json_bytes_estimate")
        self.assertEqual(built.snapshot.input_budget, 100_000 - 128 - 256)
        with self.assertRaises(ContextBudgetError):
            await BasicContextBuilder(policy=ContextPolicy(output_tokens=129)).build(history, self.model, [])


class CountingTests(unittest.IsolatedAsyncioTestCase):
    """The projection cache must not change what is selected, only its cost."""

    def setUp(self):
        self.model = Model("test", "test", "fake", 200_000, 128)

    def history(self, turns):
        records, sequence = [], 0
        for index in range(turns):
            sequence += 1
            records.append(StoredMessage(uuid4(), sequence, UserMessage(f"问题 {index} " + "字" * 180, sequence), uuid4()))
            sequence += 1
            records.append(StoredMessage(uuid4(), sequence, AssistantMessage(
                [TextContent(f"回答 {index} " + "字" * 180)], "fake", "test", "test", sequence, "stop"), records[-1].run_id))
        return records

    def snapshot(self, built):
        return (built.snapshot.source_message_ids, built.snapshot.token_estimate,
                built.snapshot.projected_token_estimate, built.snapshot.message_count,
                [(item.message_id, item.selected, item.reason) for item in built.snapshot.selections])

    async def test_additive_and_fallback_counters_select_the_same_context(self):
        history = self.history(60)
        model = Model("test", "test", "fake", 12_000, 128)
        results = []
        for counter in (RecordingCounter(True), RecordingCounter(False)):
            built = await BasicContextBuilder(counter=counter).build(history, model, [], current_run_id=history[-1].run_id)
            results.append(self.snapshot(built))
        self.assertEqual(results[0], results[1])
        self.assertTrue(results[0][0], "the budget must still select completed turns")

    async def test_selection_work_is_linear_and_reuses_the_projection_cache(self):
        history = self.history(60)
        counter = RecordingCounter(True)
        builder = BasicContextBuilder(counter=counter, policy=ContextPolicy(max_history_messages=1000))
        cache = ProjectionCache()
        first = await builder.build(history, self.model, [], current_run_id=history[-1].run_id, cache=cache)
        self.assertLessEqual(counter.serialized, 3 * len(history),
                             "selection must not re-serialize every candidate turn")
        baseline = counter.serialized
        # A second turn in the same run only projects and counts the new messages.
        appended = history + [StoredMessage(uuid4(), history[-1].sequence + 1, UserMessage("more", 1), history[-1].run_id)]
        second = await builder.build(appended, self.model, [], current_run_id=history[-1].run_id, cache=cache)
        self.assertLessEqual(counter.serialized - baseline, len(appended) + 8,
                             "a cached projection must not be recomputed for new turns")
        self.assertEqual(second.snapshot.message_count, first.snapshot.message_count + 1)

    async def test_non_additive_counter_never_prices_single_messages(self):
        """Only additive counters consume per-message costs, so the fallback must not compute one."""
        history = self.history(30)
        fallback = RecordingCounter(additive=False)
        await BasicContextBuilder(counter=fallback).build(
            history, self.model, [], current_run_id=history[-1].run_id)
        self.assertNotIn(1, fallback.lengths,
                         "a counter that cannot add must not serialize messages for an unread cost")
        additive = RecordingCounter(additive=True)
        await BasicContextBuilder(counter=additive).build(
            history, self.model, [], current_run_id=history[-1].run_id)
        self.assertIn(1, additive.lengths, "an additive counter projects each message once")

    async def test_cached_projection_does_not_leak_mutations_into_later_builds(self):
        history = self.history(3)
        cache = ProjectionCache()
        builder = BasicContextBuilder()
        first = await builder.build(history, self.model, [], current_run_id=history[-1].run_id, cache=cache)
        first.request.messages[-1].content[0].text = "mutated"
        second = await builder.build(history, self.model, [], current_run_id=history[-1].run_id, cache=cache)
        self.assertEqual(second.request.messages[-1].content[0].text, history[-1].message.content[0].text)
