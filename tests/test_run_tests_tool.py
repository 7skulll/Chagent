"""run_tests: suite detection, digest parsing and one bounded process per call."""

import os
from pathlib import Path
import tempfile
import unittest

from chagent.domain.messages import ToolCall
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.context import ToolContext
from chagent.tools.executor import ToolExecutor
from chagent.tools.tests_runner import detect_suite, summarize_pytest, summarize_unittest


CORRECT = (
    "TIERS = ((100, 0.05), (1000, 0.15))\n\n\n"
    "def discount_rate(total):\n"
    "    rate = 0.0\n"
    "    for threshold, value in TIERS:\n"
    "        if total >= threshold:\n"
    "            rate = max(rate, value)\n"
    "    return rate\n"
)
BROKEN = (
    "TIERS = ((100, 0.05), (1000, 0.15))\n\n\n"
    "def discount_rate(total):\n"
    "    for threshold, rate in TIERS:\n"
    "        if total >= threshold:\n"
    "            return rate\n"
    "    return 0.0\n"
)
TEST_MODULE = """import unittest

from app.pricing import discount_rate


class PricingTests(unittest.TestCase):
    def test_best_tier(self):
        self.assertEqual(discount_rate(1000), 0.15)


if __name__ == "__main__":
    unittest.main()
"""
OTHER_MODULE = """import unittest


class OtherTests(unittest.TestCase):
    def test_always_fails(self):
        self.assertEqual(1, 2)
"""


@unittest.skipUnless(os.name == "posix", "POSIX workspace tools")
class RunTestsToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "project"
        (self.workspace / "app").mkdir(parents=True)
        (self.workspace / "tests").mkdir()
        (self.workspace / "app" / "__init__.py").write_text("", encoding="utf-8")
        (self.workspace / "app" / "pricing.py").write_text(CORRECT, encoding="utf-8")
        # Deliberately no tests/__init__.py: the common bare layout must work.
        (self.workspace / "tests" / "test_pricing.py").write_text(TEST_MODULE, encoding="utf-8")
        self.context = ToolContext(self.workspace)
        self.executor = ToolExecutor(create_builtin_registry(mode="code", allow_host_commands=True),
                                     allow_host_commands=True)

    async def call(self, **arguments):
        return await self.executor.execute(ToolCall("test-call", "run_tests", arguments),
                                           context=self.context)

    def test_detects_unittest_without_pytest_configuration(self):
        self.assertEqual(detect_suite(self.workspace), "unittest")

    def test_pytest_configuration_wins(self):
        (self.workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
        self.assertEqual(detect_suite(self.workspace), "pytest")

    async def test_passing_suite_reports_counts_without_output(self):
        result = await self.call()
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(result.details["suite"], "unittest")
        self.assertEqual(result.details["counts"], {"passed": 1, "failed": 0, "errors": 0})
        self.assertEqual(result.details["failures"], [])
        self.assertNotIn("output tail", result.content[0].text)
        self.assertEqual(result.details["exit_code"], 0)
        self.assertNotIn("-t", result.details["command"])

    async def test_package_layout_also_passes(self):
        (self.workspace / "tests" / "__init__.py").write_text("", encoding="utf-8")
        result = await self.call()
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(result.details["counts"]["passed"], 1)

    async def test_failing_suite_names_the_test_and_keeps_output_bounded(self):
        (self.workspace / "app" / "pricing.py").write_text(BROKEN, encoding="utf-8")
        result = await self.call()
        self.assertTrue(result.is_error)
        self.assertEqual(result.details["counts"]["failed"], 1)
        self.assertTrue(any("test_best_tier" in item["name"] for item in result.details["failures"]))
        self.assertIn("Failures (1 total", result.content[0].text)
        self.assertIn("output tail", result.content[0].text)
        self.assertLess(len(result.details["output"]), 3000)

    async def test_selection_filter_excludes_other_tests(self):
        (self.workspace / "tests" / "test_other.py").write_text(OTHER_MODULE, encoding="utf-8")
        self.assertTrue((await self.call()).is_error)
        filtered = await self.call(selection="test_pricing")
        self.assertFalse(filtered.is_error, filtered.content[0].text)
        self.assertEqual(filtered.details["counts"], {"passed": 1, "failed": 0, "errors": 0})

    async def test_host_execution_is_required(self):
        executor = ToolExecutor(create_builtin_registry(mode="code", allow_host_commands=True))
        result = await executor.execute(ToolCall("test-call", "run_tests", {}), context=self.context)
        self.assertTrue(result.is_error)
        self.assertIn("disabled", result.content[0].text)

    async def test_timeout_kills_the_process_group(self):
        (self.workspace / "tests" / "test_slow.py").write_text(
            "import time, unittest\n\n\nclass Slow(unittest.TestCase):\n"
            "    def test_sleep(self):\n        time.sleep(30)\n", encoding="utf-8")
        result = await self.call(timeout_ms=1000)
        self.assertTrue(result.is_error)
        self.assertTrue(result.outcome_unknown)
        self.assertTrue(result.details["timed_out"])
        self.assertIn("timed out", result.content[0].text)


class SummaryParsingTests(unittest.TestCase):
    def test_pytest_summary(self):
        output = (
            "tests/test_x.py .F\n"
            "FAILED tests/test_x.py::test_a - AssertionError: 950 != 850\n"
            "ERROR tests/test_y.py - ImportError: no module named y\n"
            "1 failed, 1 passed, 1 error in 0.12s\n"
        )
        counts, failures = summarize_pytest(output)
        self.assertEqual(counts, {"passed": 1, "failed": 1, "errors": 1})
        self.assertEqual([item["name"] for item in failures],
                         ["tests/test_x.py::test_a", "tests/test_y.py"])

    def test_unittest_summary(self):
        output = (
            "F.\n======================================================================\n"
            "FAIL: test_a (tests.test_x.PricingTests)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            '  File "x.py", line 6, in test_a\n'
            "AssertionError: 950 != 850\n\n"
            "----------------------------------------------------------------------\n"
            "Ran 2 tests in 0.001s\n\n"
            "FAILED (failures=1)\n"
        )
        counts, failures = summarize_unittest(output)
        self.assertEqual(counts, {"passed": 1, "failed": 1, "errors": 0})
        self.assertEqual(failures[0]["name"], "tests.test_x.PricingTests.test_a")
        self.assertIn("950 != 850", failures[0]["message"])
