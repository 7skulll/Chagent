"""B2：压缩配额跟随轮次预算，而不是固定四次。"""

import unittest

from chagent.domain.summaries import (
    MIN_COMPACTIONS_PER_RUN,
    TURNS_PER_COMPACTION,
    CompactionPolicy,
    compactions_for_turns,
)


class QuotaTests(unittest.TestCase):
    def test_measured_pace_drives_the_constant(self):
        # 一次实测 run：43 次模型调用里压了 3 次 ≈ 每 14 轮一次。常量为 12 时
        # 配额不会少于实际需要；如果哪天实测变了，这条断言会提醒改常量。
        self.assertEqual(TURNS_PER_COMPACTION, 12)

    def test_short_runs_keep_the_floor(self):
        self.assertEqual(compactions_for_turns(32), 4)
        self.assertEqual(compactions_for_turns(48), 4)

    def test_long_runs_scale(self):
        self.assertEqual(compactions_for_turns(96), 8)
        self.assertEqual(compactions_for_turns(128), 11)

    def test_unknown_turn_budget_keeps_the_floor(self):
        self.assertEqual(compactions_for_turns(None), MIN_COMPACTIONS_PER_RUN)
        self.assertEqual(compactions_for_turns(0), MIN_COMPACTIONS_PER_RUN)

    def test_env_tracks_max_turns(self):
        self.assertEqual(CompactionPolicy.from_env({}, max_turns=96).max_compactions_per_run, 8)
        self.assertEqual(CompactionPolicy.from_env({}, max_turns=32).max_compactions_per_run, 4)

    def test_explicit_override_still_wins(self):
        policy = CompactionPolicy.from_env({"CHAGENT_COMPACTION_MAX_PER_RUN": "4"}, max_turns=96)
        self.assertEqual(policy.max_compactions_per_run, 4)

    def test_no_input_keeps_the_previous_default(self):
        self.assertEqual(CompactionPolicy.from_env({}).max_compactions_per_run, 4)


if __name__ == "__main__":
    unittest.main()
