"""Turn advice, tool-output thresholds, and a budget that can be paused."""

import asyncio
import unittest

from chagent.agent.budget import RunBudget
from chagent.agent.turn_note import is_turn_note, last_request_message, turn_note
from chagent.domain.messages import TextContent, ToolResultMessage, UserMessage
from chagent.storage.artifacts import INLINE_MAX_BYTES, INLINE_MIN_BYTES, inline_bytes_for


class TurnNoteTests(unittest.TestCase):
    def test_early_note_states_the_count_without_advice(self):
        note = turn_note(0, 10)
        self.assertTrue(note.startswith("[turn 1/10]"))
        self.assertIn("9 turns remain", note)
        self.assertNotIn("Stop exploring", note)
        self.assertNotIn("fewer, larger steps", note)

    def test_half_point_asks_for_convergence(self):
        note = turn_note(4, 10)
        self.assertTrue(note.startswith("[turn 5/10]"))
        self.assertIn("fewer, larger steps", note)
        self.assertNotIn("Stop exploring", note)

    def test_finish_point_demands_a_result(self):
        note = turn_note(9, 10)
        self.assertTrue(note.startswith("[turn 10/10]"))
        self.assertIn("0 turns remain", note)
        self.assertIn("Stop exploring", note)

    def test_singular_remaining_turn(self):
        self.assertIn("1 turn remains", turn_note(8, 10))

    def test_every_note_announces_it_is_not_a_request(self):
        """The note trails a tool result; it must not read as a new instruction."""
        for iteration in (0, 4, 9):
            with self.subTest(iteration=iteration):
                self.assertIn("not a new request", turn_note(iteration, 10))

    def test_invalid_bounds_are_rejected(self):
        with self.assertRaises(ValueError):
            turn_note(-1, 10)
        with self.assertRaises(ValueError):
            turn_note(0, 0)

    def test_recognising_and_skipping_the_note(self):
        note = UserMessage(turn_note(1, 10), 0)
        ask = UserMessage("do the task", 0)
        result = ToolResultMessage("call", "echo", [TextContent("out")], False, 0)
        self.assertTrue(is_turn_note(note))
        self.assertFalse(is_turn_note(ask))
        self.assertFalse(is_turn_note(result))
        self.assertIs(last_request_message([ask, result, note]), result)
        self.assertIs(last_request_message([ask]), ask)


class InlineThresholdTests(unittest.TestCase):
    def test_small_window_keeps_the_floor(self):
        self.assertEqual(inline_bytes_for(7_000), INLINE_MIN_BYTES)

    def test_threshold_tracks_the_budget(self):
        self.assertEqual(inline_bytes_for(40_000), 20_000)

    def test_large_window_is_capped(self):
        self.assertEqual(inline_bytes_for(200_000), INLINE_MAX_BYTES)

    def test_nonpositive_budget_is_refused(self):
        with self.assertRaises(ValueError):
            inline_bytes_for(0)


class RunBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_wall_limit_expires_even_while_approval_budget_is_paused(self):
        fired = asyncio.Event()
        budget = RunBudget(1.0, fired.set, wall_seconds=0.05)
        budget.start()
        budget.pause(capped=False)
        try:
            await asyncio.wait_for(fired.wait(), 0.5)
            self.assertTrue(budget.expired)
        finally:
            budget.stop()

    async def test_nested_pauses_need_matching_resumes(self):
        fired = []
        budget = RunBudget(5.0, lambda: fired.append(True))
        budget.start()
        budget.pause()
        budget.pause()
        budget.resume()
        self.assertTrue(budget.paused, "one resume must not restart a doubly paused clock")
        budget.resume()
        self.assertFalse(budget.paused)
        budget.stop()
        self.assertEqual(fired, [])

    async def test_pause_gives_the_waiting_time_back(self):
        budget = RunBudget(0.4, lambda: None)
        budget.start()
        before = budget.remaining()
        budget.pause()
        await asyncio.sleep(0.05)
        budget.resume()
        budget.stop()
        self.assertGreater(budget.remaining(), before - 0.02)

    async def test_a_paused_clock_still_ends(self):
        """Without a ceiling one stuck tool would hold the run open forever."""
        expired = asyncio.Event()
        budget = RunBudget(0.05, expired.set)
        budget.start()
        budget.pause()
        await asyncio.wait_for(expired.wait(), 1.0)
        self.assertTrue(budget.expired)
        budget.stop()

    async def test_an_approval_inside_a_tool_pause_keeps_the_ceiling(self):
        """An uncapped wait must not remove the only bound a running tool has.

        Approvals are asked for before tools run today, but the pause counting
        is documented as supporting overlapping waiters. If the uncapped one
        cancels the cap, the remaining tool has no ceiling at all and a tool
        that never returns leaves the run open until the wall clock instead of
        one budget.
        """
        expired = asyncio.Event()
        budget = RunBudget(0.05, expired.set)
        budget.start()
        budget.pause()                 # a tool is running: this needs a ceiling
        budget.pause(capped=False)     # an approval wait answers in the same stretch
        budget.resume()                # the approval was answered; the tool still runs
        await asyncio.wait_for(expired.wait(), 1.0)
        self.assertTrue(budget.expired)
        budget.resume()
        budget.stop()
