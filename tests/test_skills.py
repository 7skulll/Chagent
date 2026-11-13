"""Bundled workflows use the ordinary tool boundary and model request budget."""

import asyncio
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import unittest
from uuid import uuid4

from chagent.context.builder import BasicContextBuilder
from chagent.domain.messages import ToolCall, UserMessage
from chagent.domain.runs import StoredMessage
from chagent.models.base import Model
from chagent.models.outbound import protect_payload
from chagent.skills import SKILL_NAMES, read_skill
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.executor import ToolExecutor


class SkillTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_skill_loads_without_workspace_or_execution_permission(self):
        registry = create_builtin_registry(mode="work", allow_workspace_writes=False)
        executor = ToolExecutor(registry)
        for name in SKILL_NAMES:
            with self.subTest(name=name):
                result = await executor.execute(ToolCall(name, "read_skill", {"name": name}))
                self.assertFalse(result.is_error)
                contents = read_skill(name)
                digest = sha256(contents.encode("utf-8")).hexdigest()
                self.assertEqual(result.details, {"skill_name": name, "sha256": digest, "source": "builtin"})
                self.assertIn(contents, result.content[0].text)
                self.assertIn(digest, result.content[0].text)
                self.assertEqual(protect_payload(asdict(result), Path("/tmp/skill-workspace")), asdict(result))

    async def test_unknown_names_and_arbitrary_paths_are_not_readable(self):
        executor = ToolExecutor(create_builtin_registry(mode="work"))
        for name in ("missing", "../config", "/etc/passwd", "project-explorer/SKILL.md"):
            result = await executor.execute(ToolCall("bad", "read_skill", {"name": name}))
            self.assertTrue(result.is_error)
            with self.assertRaises(ValueError):
                read_skill(name)

    async def test_cancelled_read_propagates_cancellation(self):
        executor = ToolExecutor(create_builtin_registry(mode="work"))
        signal = asyncio.Event()
        signal.set()
        with self.assertRaises(asyncio.CancelledError):
            await executor.execute(ToolCall("cancel", "read_skill", {"name": SKILL_NAMES[0]}), signal=signal)

    async def test_catalog_reaches_model_without_workflow_bodies_even_with_deferral(self):
        registry = create_builtin_registry(mode="code", allow_host_commands=True, defer_tools=True)
        tool = registry.get("read_skill")
        self.assertIn("read_skill", {item.name for item in registry.declarations()})
        model = Model(id="fake", provider="test", api="fake", context_window=16384, max_tokens=512)
        builder = BasicContextBuilder()
        history = [StoredMessage(uuid4(), 1, UserMessage("了解项目", 0))]
        built = await builder.build(history, model, registry.declarations())
        for name in SKILL_NAMES:
            self.assertIn(name, tool.description)
            self.assertIn(name, str(asdict(built.request)))
            body = read_skill(name).split("---", 2)[2].strip()
            self.assertNotIn(body, tool.description)
        without = await builder.build(history, model, [t for t in registry.declarations() if t.name != "read_skill"])
        self.assertGreater(built.snapshot.token_estimate, without.snapshot.token_estimate)


if __name__ == "__main__":
    unittest.main()
