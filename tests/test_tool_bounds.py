"""B3：参数的数值边界必须出现在模型读得到的描述里。

实测：模型给 `timeout_ms` 传了 60000，schema 上限是 30000，于是整轮被一次
校验拒绝浪费掉。schema 是对的，声明是不完整的——模型只能靠失败去学习边界。
"""

import unittest

from chagent.tools.base import AgentTool, bounds_note
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.command import ExecCommandTool
from chagent.tools.filesystem import ReadFileTool


def _numeric_bounds(parameters):
    """(name, value) for every numeric minimum/maximum in a flat schema."""
    found = []
    for name, spec in (parameters.get("properties") or {}).items():
        if not isinstance(spec, dict):
            continue
        for key in ("minimum", "maximum"):
            value = spec.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                found.append((f"{name}.{key}", value))
    return found


class BoundsNoteTests(unittest.TestCase):
    def test_both_ends(self):
        note = bounds_note({"properties": {"n": {"minimum": 1, "maximum": 30_000}}})
        self.assertIn("n 1-30000", note)

    def test_upper_only(self):
        self.assertIn("n<=1000", bounds_note({"properties": {"n": {"maximum": 1000}}}))

    def test_lower_only(self):
        self.assertIn("n>=100", bounds_note({"properties": {"n": {"minimum": 100}}}))

    def test_no_bounds_no_noise(self):
        self.assertEqual(bounds_note({"properties": {"text": {"type": "string"}}}), "")
        self.assertEqual(bounds_note({"type": "object"}), "")


class DeclarationTests(unittest.TestCase):
    def test_every_tool_declares_its_numeric_bounds(self):
        """Registry-wide: a bound the schema enforces but the model cannot read is a defect."""
        registry = create_builtin_registry(mode="work", allow_host_commands=True)
        declarations = {tool.name: tool for tool in registry.definitions()}
        self.assertTrue(declarations, "registry produced no tools")
        for name, tool in declarations.items():
            for _label, value in _numeric_bounds(tool.parameters):
                self.assertIn(str(int(value)), tool.description,
                              f"{name} 的数值边界 {value} 没有出现在描述里")

    def test_the_regression_case(self):
        """The call that cost a real run one turn."""
        declaration = ExecCommandTool(None, None).declaration()
        self.assertIn("30000", declaration.description)
        self.assertIn("timeout_ms 1-30000", declaration.description)

    def test_declaration_is_stable(self):
        tool = ReadFileTool()
        first, second = tool.declaration(), tool.declaration()
        self.assertEqual(first.description, second.description)
        self.assertEqual(first.description.count("Bounds:"), 1)
        self.assertIn("limit 1-1000", first.description)

    def test_the_note_stays_within_its_own_budget(self):
        """This text is paid on every request of every run, so it has a cap.

        209 UTF-8 bytes today across the full command face, i.e. ~52 BPE tokens
        (~0.1% of the 48.9k production budget) but 209 units of the byte counter
        that small-window fixtures use. Compress the wording before raising this.
        """
        registry = create_builtin_registry(mode="code", allow_host_commands=True, include_echo=True)
        added = sum(len(tool.description.split("Bounds:")[-1])
                    for tool in registry.definitions() if "Bounds:" in tool.description)
        self.assertLessEqual(added, 240, f"边界文本 {added} 字节，超出预算")

    def test_parameters_are_not_mutated(self):
        tool = ReadFileTool()
        before = repr(tool.parameters)
        tool.declaration().parameters["properties"]["limit"]["maximum"] = 1
        self.assertEqual(repr(tool.parameters), before)


if __name__ == "__main__":
    unittest.main()
