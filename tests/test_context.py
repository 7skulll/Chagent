from copy import deepcopy
from dataclasses import asdict
import json
import unittest
from uuid import uuid4

from chagent.context.builder import BasicContextBuilder, ContextBudgetError, ContextIntegrityError
from chagent.context.projection import ProjectionCache
from chagent.context.tokens import HeuristicTokenCounter, estimate_tokens
from chagent.domain.context import ContextPolicy, ObservedUsage
from chagent.domain.messages import AssistantMessage, SystemMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
from chagent.domain.runs import StoredMessage
from chagent.domain.sessions import TaskState
from chagent.domain.tools import Tool
from chagent.models.base import Model


class RecordingCounter:
    """Same numbers as HeuristicTokenCounter, but records serialization volume.

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

    async def test_identical_state_builds_an_identical_request(self):
        """The prompt must be a pure function of history, tools and per-run state.

        ``scripts/report_prompt_cache.py`` measures a 88% provider cache hit rate
        over 51 recorded runs; that only holds while this stays true.
        """
        run_id = uuid4()
        history = [self.record(1, UserMessage("约束" * 40, 1), run_id),
                   self.record(2, AssistantMessage([TextContent("ok")], "fake", "test", "test", 2, "stop"), run_id)]
        state = TaskState(goal="finish", constraints=["不要修改数据库"], revision=4)
        tools = [Tool("echo", "describe", {"type": "object"})]
        builder = BasicContextBuilder()
        first = await builder.build(history, self.model, tools, task_state=state, current_run_id=run_id)
        second = await builder.build(history, self.model, tools, task_state=state, current_run_id=run_id)
        self.assertEqual([asdict(message) for message in first.request.messages],
                         [asdict(message) for message in second.request.messages])
        self.assertEqual(first.snapshot.token_estimate, second.snapshot.token_estimate)
        # Per-run state travels in the leading message on purpose: the runner
        # loads it once per run, so it costs at most one cache miss per run.
        # Moving it into a trailing message would let a model read it as a new
        # turn and act again instead of stopping.
        self.assertIn("不要修改数据库", first.request.messages[0].text())

    async def test_each_turn_extends_the_previous_request_without_rewriting_it(self):
        """A provider can only cache turns that survive verbatim into the next one."""
        run_id = uuid4()
        first = [self.record(1, UserMessage("start", 1), run_id)]
        second = first + [self.record(2, AssistantMessage([TextContent("a")], "fake", "test", "test", 2, "stop"), run_id),
                          self.record(3, UserMessage("next", 3), run_id)]
        third = second + [self.record(4, AssistantMessage([ToolCall("c", "echo", {})], "fake", "test", "test", 4, "toolUse"), run_id),
                          self.record(5, ToolResultMessage("c", "echo", [TextContent("x")], False, 5), run_id)]
        state = TaskState(goal="finish", revision=2)
        builder = BasicContextBuilder()
        requests = [(await builder.build(history, self.model, [], task_state=state,
                                         current_run_id=run_id)).request.messages
                    for history in (first, second, third)]
        for previous, current in zip(requests, requests[1:]):
            self.assertGreater(len(current), len(previous))
            self.assertEqual(current[:len(previous)], previous,
                             "an earlier turn must remain a byte-identical prefix")

    async def test_task_constraints_and_tools_count_against_budget(self):
        history = [self.record(1, UserMessage("hello", 1))]
        state = TaskState(goal="finish task", constraints=["不要修改数据库"], pending=["verify"], revision=4)
        tools = [Tool("echo", "x" * 200, {"type": "object"})]
        builder = BasicContextBuilder()
        plain = await builder.build(history, self.model, [])
        built = await builder.build(history, self.model, tools, task_state=state)
        self.assertGreater(built.snapshot.token_estimate, plain.snapshot.token_estimate)
        self.assertIn("不要修改数据库", built.request.messages[0].text())
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
        self.assertEqual(built.snapshot.token_counter, HeuristicTokenCounter.name)
        self.assertEqual(built.snapshot.estimate_source, "coarse")
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


class PromptSectionTests(unittest.IsolatedAsyncioTestCase):
    """The system prompt is five named sections in a fixed order."""

    def setUp(self):
        self.model = Model("test", "test", "fake", 100_000, 128)

    def history(self):
        run = uuid4()
        return [StoredMessage(uuid4(), 1, UserMessage("hi", 1), run)]

    async def test_sections_are_named_and_ordered(self):
        builder = BasicContextBuilder(policy=ContextPolicy(safety_tokens=0))
        built = await builder.build(self.history(), self.model, [], task_state=TaskState(goal="ship"),
                                    environment="Working directory: /tmp")
        sections = built.request.messages[0].sections
        self.assertEqual(list(sections), ["preamble", "tools", "rules", "environment", "state"])
        self.assertEqual(built.request.messages[0].content, "")
        self.assertEqual(sections["environment"], "Working directory: /tmp")
        self.assertIn("ship", sections["state"])
        # The wire text is the sections in order, so the label cannot drift from
        # what the provider actually receives.
        self.assertTrue(built.request.messages[0].text().startswith(sections["preamble"]))
        self.assertLess(built.request.messages[0].text().index(sections["rules"]),
                        built.request.messages[0].text().index(sections["state"]))

    async def test_absent_run_facts_do_not_leave_empty_sections(self):
        builder = BasicContextBuilder(policy=ContextPolicy(safety_tokens=0))
        built = await builder.build(self.history(), self.model, [])
        self.assertEqual(list(built.request.messages[0].sections), ["preamble", "tools", "rules"])

    async def test_mode_section_names_the_session_and_the_other_face(self):
        """A work session in a code-capable build must not report a missing capability."""
        builder = BasicContextBuilder(policy=ContextPolicy(safety_tokens=0))
        work = await builder.build(self.history(), self.model, [], mode="work", other_modes=("code",))
        text = work.request.messages[0].sections["mode"]
        self.assertIn("work mode", text)
        self.assertIn("code mode", text)
        self.assertIn("exec_command", text)
        code = await builder.build(self.history(), self.model, [], mode="code", other_modes=("work",))
        self.assertIn("exec_command", code.request.messages[0].sections["mode"])
        # Order is the cache prefix: mode sits right after the tool list.
        self.assertEqual(list(code.request.messages[0].sections)[:4], ["preamble", "tools", "mode", "rules"])

    async def test_work_only_build_spends_no_tokens_on_a_mode_the_user_cannot_reach(self):
        builder = BasicContextBuilder(policy=ContextPolicy(safety_tokens=0))
        built = await builder.build(self.history(), self.model, [], mode="work", other_modes=())
        self.assertEqual(list(built.request.messages[0].sections), ["preamble", "tools", "rules"])

    async def test_each_section_is_byte_identical_across_turns(self):
        builder = BasicContextBuilder(policy=ContextPolicy(safety_tokens=0))
        first = await builder.build(self.history(), self.model, [], task_state=TaskState(goal="ship"),
                                    environment="Working directory: /tmp")
        second = await builder.build(self.history(), self.model, [], task_state=TaskState(goal="ship"),
                                     environment="Working directory: /tmp")
        self.assertEqual(first.request.messages[0].sections, second.request.messages[0].sections)

    async def test_snapshot_names_the_counter_that_produced_the_estimate(self):
        class StubCounter:
            name = "stub_counter"
            additive = True

            def count(self, messages):
                return 7 * len(messages)

        builder = BasicContextBuilder(counter=StubCounter(), policy=ContextPolicy(safety_tokens=0))
        built = await builder.build(self.history(), self.model, [])
        self.assertEqual(built.snapshot.token_counter, "stub_counter")
        self.assertEqual(built.snapshot.token_estimates, {"stub_counter": built.snapshot.token_estimate})
        self.assertEqual(built.snapshot.estimate_source, "coarse")


class ObservedUsageTests(unittest.IsolatedAsyncioTestCase):
    """The provider's own count anchors the estimate; only the tail is inferred."""

    def setUp(self):
        self.model = Model("test", "test", "fake", 100_000, 128)
        self.counter = HeuristicTokenCounter()

    def record(self, sequence, message, run_id=None):
        return StoredMessage(uuid4(), sequence, message, run_id)

    async def test_only_the_messages_appended_since_the_last_response_are_estimated(self):
        run_id = uuid4()
        history = [self.record(1, UserMessage("约束" * 60, 1), run_id),
                   self.record(2, AssistantMessage([ToolCall("a", "echo", {"text": "x"})], "fake", "test", "test", 2, "toolUse"), run_id),
                   self.record(3, ToolResultMessage("a", "echo", [TextContent("x" * 900)], False, 3), run_id)]
        built = await BasicContextBuilder(counter=self.counter).build(
            history, self.model, [], current_run_id=run_id, observed=ObservedUsage(4_000, 3_800, after_sequence=2))
        self.assertEqual(built.snapshot.estimate_source, "observed")
        self.assertEqual(built.snapshot.observed_prompt_tokens, 4_000)
        # Only the tool result is new; the user turn and the call were already paid for.
        self.assertEqual(built.snapshot.tail_token_estimate,
                         self.counter.count([built.request.messages[-1]]))
        self.assertEqual(built.snapshot.token_estimate, 4_000 + built.snapshot.tail_token_estimate)
        self.assertEqual(built.snapshot.token_estimate - built.snapshot.tail_token_estimate, 4_000)

    async def test_an_anchor_with_nothing_appended_is_exact(self):
        run_id = uuid4()
        history = [self.record(1, UserMessage("hello", 1), run_id),
                   self.record(2, AssistantMessage([TextContent("答案")], "fake", "test", "test", 2, "stop"), run_id)]
        built = await BasicContextBuilder(counter=self.counter).build(
            history, self.model, [], current_run_id=run_id, observed=ObservedUsage(1_234, 1_200, after_sequence=2))
        self.assertEqual((built.snapshot.token_estimate, built.snapshot.tail_token_estimate), (1_234, 0))

    async def test_a_summary_that_covers_the_anchor_invalidates_it(self):
        """After compaction the anchored prompt no longer exists; count afresh."""
        from chagent.domain.summaries import SessionSummary
        run_id = uuid4()
        history = [self.record(1, UserMessage("old", 1), run_id),
                   self.record(2, AssistantMessage([TextContent("old answer")], "fake", "test", "test", 2, "stop"), run_id),
                   self.record(3, UserMessage("new", 3), run_id)]
        summary = SessionSummary(
            id=uuid4(), session_id=uuid4(), revision=1, previous_summary_id=None,
            covered_through_sequence=2, first_kept_message_id=history[2].id, content="摘要",
            model="test", provider="test", prompt_version="v", compaction_id=uuid4(),
        )
        built = await BasicContextBuilder(counter=self.counter).build(
            history, self.model, [], current_run_id=run_id, summary=summary,
            observed=ObservedUsage(9_999, 9_500, after_sequence=1))
        self.assertEqual(built.snapshot.estimate_source, "coarse")
        self.assertIsNone(built.snapshot.observed_prompt_tokens)
        self.assertEqual(built.snapshot.token_estimate, self.counter.count(built.request.messages))

    async def test_the_snapshot_reports_how_much_of_a_result_the_model_saw(self):
        run_id = uuid4()
        history = [self.record(1, UserMessage("看日志", 1), run_id),
                   self.record(2, AssistantMessage([ToolCall("a", "exec_command", {})], "fake", "test", "test", 2, "toolUse"), run_id),
                   self.record(3, ToolResultMessage("a", "exec_command", [TextContent("日志" * 4_000)], False, 3), run_id)]
        built = await BasicContextBuilder(counter=self.counter, policy=ContextPolicy(max_tool_result_tokens=300)).build(
            history, self.model, [], current_run_id=run_id)
        self.assertGreater(built.snapshot.truncated_tool_results, 0)
        self.assertLessEqual(estimate_tokens(built.request.messages[-1].content[0].text), 300)


class EstimateScaleTests(unittest.IsolatedAsyncioTestCase):
    """The provider's measured error must reach turn selection, not just the report."""

    def setUp(self):
        self.builder = BasicContextBuilder()

    def history(self, turns=8, width=300):
        """One completed run per turn, so only the last turn is required."""
        records, sequence, run_id = [], 0, None
        for index in range(turns):
            run_id = uuid4()
            sequence += 1
            records.append(StoredMessage(uuid4(), sequence, UserMessage(f"turn {index} " + "x" * width, sequence), run_id))
            sequence += 1
            records.append(StoredMessage(uuid4(), sequence, AssistantMessage(
                [TextContent("ok")], "fake", "test", "test", sequence, "stop"), run_id))
        return records, run_id

    def test_scale_is_the_measured_ratio_and_is_bounded(self):
        self.assertEqual(ObservedUsage(300, 300, after_sequence=1, coarse_prompt_tokens=200).scale, 1.5)
        self.assertEqual(ObservedUsage(300, 300, after_sequence=1).scale, 1.0, "no coarse reference")
        self.assertEqual(ObservedUsage(300, 300, after_sequence=1, coarse_prompt_tokens=1).scale, 3.0)
        self.assertEqual(ObservedUsage(300, 30, after_sequence=1, coarse_prompt_tokens=100).scale, 1.0,
                         "a ratio below one never widens the budget")

    async def test_a_shortfalling_heuristic_tightens_selection(self):
        history, run_id = self.history()
        model = Model("test", "test", "fake", 1_600, 128)
        plain = await self.builder.build(history, model, [], current_run_id=run_id)
        coarse = plain.snapshot.coarse_token_estimate
        anchor = ObservedUsage(prompt_tokens=round(coarse * 1.3), input_tokens=round(coarse * 1.3),
                               after_sequence=history[-1].sequence, coarse_prompt_tokens=coarse)
        self.assertAlmostEqual(anchor.scale, 1.3, places=2)
        tightened = await self.builder.build(history, model, [], current_run_id=run_id, observed=anchor)
        self.assertLess(len(tightened.snapshot.source_message_ids),
                        len(plain.snapshot.source_message_ids),
                        "the same content must not be admitted twice over")
        self.assertGreater(tightened.snapshot.projected_token_estimate,
                           plain.snapshot.projected_token_estimate,
                           "the projection is reported in the provider's units")
        self.assertLessEqual(tightened.snapshot.token_estimate, tightened.snapshot.input_budget)

    async def test_the_estimate_and_the_projection_agree_on_what_fits(self):
        """A budget-maximal selection may not be rejected by the anchored total."""
        history, run_id = self.history()
        model = Model("test", "test", "fake", 1_600, 128)
        plain = await self.builder.build(history, model, [], current_run_id=run_id)
        coarse = plain.snapshot.coarse_token_estimate
        anchor = ObservedUsage(prompt_tokens=round(coarse * 1.3), input_tokens=round(coarse * 1.3),
                               after_sequence=history[-1].sequence, coarse_prompt_tokens=coarse)
        built = await self.builder.build(history, model, [], current_run_id=run_id, observed=anchor)
        self.assertLessEqual(built.snapshot.token_estimate, built.snapshot.input_budget,
                             "selection and the final check must agree on what fits")

    async def test_dropping_an_anchored_turn_falls_back_to_the_corrected_estimate(self):
        """An anchor that covers a dropped turn describes a prompt that no longer exists."""
        history, run_id = self.history()
        model = Model("test", "test", "fake", 2_000, 128)
        plain = await self.builder.build(history, model, [], current_run_id=run_id)
        coarse = plain.snapshot.coarse_token_estimate
        anchor = ObservedUsage(prompt_tokens=round(coarse * 1.5), input_tokens=round(coarse * 1.5),
                               after_sequence=history[-1].sequence, coarse_prompt_tokens=coarse)
        # The correction alone pushes the oldest turns out: the anchor now covers
        # messages the request will not contain.
        built = await self.builder.build(history, model, [], current_run_id=run_id, observed=anchor)
        self.assertEqual(built.snapshot.estimate_source, "scaled")
        self.assertLess(len(built.snapshot.source_message_ids), len(plain.snapshot.source_message_ids))
        self.assertLessEqual(built.snapshot.token_estimate, built.snapshot.input_budget)
