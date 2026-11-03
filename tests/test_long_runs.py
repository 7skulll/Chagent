"""Long-task checkpoint lifecycle and immutable tool-artifact round trips."""

import asyncio
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
import tempfile
import unittest
from uuid import UUID, uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.agent.runner import RunFailedError
from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.context.builder import BasicContextBuilder
from chagent.domain.messages import AssistantMessage, TextContent, ToolCall, UserMessage
from chagent.domain.summaries import CompactionPolicy
from chagent.models.events import DoneEvent, StartEvent
from chagent.models.fake import FakeModelClient
from chagent.storage.artifacts import ArtifactRepository
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.summaries import SummaryRepository
from chagent.tools.base import AgentTool, AgentToolResult
from chagent.tools.context import ToolContext


# Six 2.4 KB tool results force compaction inside this window; the window also has
# to hold the run-static environment snapshot, so it is not the bare minimum.
# Sized so a 2.4 KB tool result plus the fixed prompt still leaves room for the
# retained input. The named system sections added ~150 bytes of prefix; the
# fixture follows the prompt rather than the prompt being trimmed to the fixture.
LONG_RUN_WINDOW = 14_500


class EmitTool(AgentTool):
    def __init__(self, text):
        super().__init__(name="emit", label="Emit", description="Return test text", parameters={"type": "object"})
        self.text = text

    async def execute(self, *args, **kwargs):
        return AgentToolResult([TextContent(self.text)], details={"output": self.text})


class LongClient(FakeModelClient):
    def __init__(self, *, steps=6, ending="stop"):
        super().__init__()
        self.steps, self.ending = steps, ending
        self.requests, self.step = [], 0
        self.blocked = asyncio.Event()

    async def stream(self, request):
        self.requests.append(deepcopy(request))
        if request.purpose == "summary":
            async for event in super().stream(request):
                yield event
            return
        if self.step >= self.steps and self.ending == "fail":
            raise RuntimeError("Task failed after active checkpoints")
        if self.step >= self.steps and self.ending == "block":
            self.blocked.set()
            await asyncio.Event().wait()
        self.step += 1
        message = AssistantMessage([], request.model.api, request.model.provider, request.model.id, 1, "pending")
        yield StartEvent(partial=message)
        message = deepcopy(message)
        message.stop_reason = "toolUse" if self.step <= self.steps else "stop"
        message.content = [ToolCall(f"call-{self.step}", "emit", {"step": self.step})] if message.stop_reason == "toolUse" else [TextContent("finished")]
        yield DoneEvent(reason=message.stop_reason, message=message)


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class LongRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_long_test_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}",
        )))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.runner = create_demo_runner(self.database, compaction_policy=CompactionPolicy())
        self.runner.model.context_window = LONG_RUN_WINDOW
        self.runner.tools.registry.register(EmitTool("tool result\n" + "x" * 2400))
        self.client = LongClient()
        self.runner.client = self.client
        # An empty workspace keeps the run-static environment snapshot small; these
        # tests are about compaction arithmetic, not about what the directory holds.
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.session = await self.runner.conversations.create_session(Path(self.temporary.name))
        self.summaries = SummaryRepository(self.database)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def test_repeated_in_run_compaction_preserves_input_and_tool_batches(self):
        original = "EXACT USER GOAL: keep all user constraints verbatim"
        result = await self.runner.run(self.session, original)
        self.assertEqual(result.message.content[0].text, "finished")
        summaries = await self.summaries.list(self.session)
        self.assertGreaterEqual(len(summaries), 2)
        self.assertTrue(all(summary.source_run_id == result.run_id for summary in summaries))
        records = await self.runner.conversations.messages(self.session)
        self.assertEqual(len(records), 1 + 6 * 2 + 1)
        for summary in summaries:
            self.assertEqual(summary.protected_input_id, records[0].id)
            self.assertNotIn(str(records[0].id), summary.source_message_ids)
        for request in self.client.requests:
            if request.purpose == "task":
                self.assertTrue(any(isinstance(message, UserMessage) and message.content == original for message in request.messages))
                # The raw and projected transcripts both maintain complete batches.
                BasicContextBuilder._validate([type(records[0])(uuid4(), i, message) for i, message in enumerate(request.messages)])
                self.assertLessEqual(self.runner.context.counter.count(request.messages) + request.max_tokens + 256, LONG_RUN_WINDOW)
        self.assertEqual((await self.summaries.latest(self.session)).id, summaries[0].id)
        restored = create_demo_runner(self.database)
        restored.model.context_window = LONG_RUN_WINDOW
        await restored.run(self.session, "continue from completed long task")

    async def test_failed_active_checkpoints_are_not_replayed_and_revisions_can_branch(self):
        self.client.ending = "fail"
        with self.assertRaises(RunFailedError) as error:
            await self.runner.run(self.session, "FAILED TASK INPUT")
        invalid = await self.summaries.list(self.session)
        self.assertTrue(invalid)
        self.assertIsNone(await self.summaries.latest(self.session))
        self.runner.client = LongClient()
        result = await self.runner.run(self.session, "NEW CLEAN TASK INPUT")
        self.assertEqual(result.message.stop_reason, "stop")
        valid = await self.summaries.latest(self.session)
        self.assertIsNotNone(valid)
        self.assertGreater(valid.revision, invalid[0].revision)
        first_summary_request = next(request for request in self.runner.client.requests if request.purpose == "summary")
        self.assertNotIn("FAILED TASK INPUT", str(first_summary_request.messages))
        self.assertNotEqual(valid.source_run_id, error.exception.run_id)

    async def test_cancel_after_active_checkpoint_keeps_it_auditable_but_inactive(self):
        self.client.ending = "block"
        identity = []
        task = asyncio.create_task(self.runner.run(self.session, "cancel long task", on_started=identity.append))
        await asyncio.wait_for(self.client.blocked.wait(), 5)
        self.assertIsNotNone(await self.summaries.latest(self.session, current_run=identity[0]))
        self.runner.cancel(identity[0])
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(await self.summaries.list(self.session))
        self.assertIsNone(await self.summaries.latest(self.session))

    async def test_large_tool_output_is_saved_and_read_by_model_in_pages(self):
        text = "甲😀Z\n" * 3000 + "TAIL-TARGET"
        self.runner.tools.registry.get("emit").text = text
        artifacts = ArtifactRepository(self.database)
        owner = self
        class ArtifactClient(FakeModelClient):
            def __init__(self):
                super().__init__(); self.step = 0
            async def stream(self, request):
                if request.purpose == "summary":
                    async for event in super().stream(request):
                        yield event
                    return
                self.step += 1
                message = AssistantMessage([], request.model.api, request.model.provider, request.model.id, 1, "pending")
                yield StartEvent(partial=message)
                message = deepcopy(message)
                message.stop_reason = "toolUse" if self.step <= 2 else "stop"
                if self.step == 1:
                    call = ToolCall("large", "emit", {})
                elif self.step == 2:
                    records = await owner.runner.conversations.messages(owner.session)
                    reference = records[-1].message.artifact
                    self.reference = reference
                    owner.assertIn(reference.id, request.messages[-1].content[0].text)
                    owner.assertNotIn("甲😀Z\n" * 500, request.messages[-1].content[0].text)
                    call = ToolCall("read", "read_artifact", {"artifact_id": reference.id, "offset": len(text) - 11, "limit": 100})
                else:
                    owner.assertIn("TAIL-TARGET", request.messages[-1].content[0].text)
                message.content = [call] if self.step <= 2 else [TextContent("artifact read")]
                yield DoneEvent(reason=message.stop_reason, message=message)
        client = ArtifactClient()
        self.runner.client = client
        result = await self.runner.run(self.session, "read a large tool output")
        records = await self.runner.conversations.messages(self.session)
        self.assertEqual(records[2].message.content[0].text, text)
        self.assertEqual(records[2].message.artifact.sha256, sha256(text.encode()).hexdigest())
        projected = await self.runner.conversations.messages(self.session, project_artifacts=True)
        self.assertEqual(projected[2].message.content, [])
        self.assertIsNone(projected[2].message.details)
        self.assertEqual(projected[2].message.artifact.id, client.reference.id)
        offset, pieces = 0, []
        while offset is not None:
            page = await artifacts.read(self.session, UUID(client.reference.id), offset=offset)
            pieces.append(page["text"]); offset = page["next_offset"]
        self.assertEqual("".join(pieces), text)
        second_workspace = Path(self.temporary.name) / "other"
        second_workspace.mkdir()
        other = await self.runner.conversations.create_session(second_workspace)
        with self.assertRaises(LookupError):
            await artifacts.read(other, UUID(client.reference.id))
        denied = await self.runner.tools.execute(ToolCall("deny", "read_artifact", {"artifact_id": client.reference.id}),
                                                 context=ToolContext(second_workspace, other))
        self.assertTrue(denied.is_error)
        self.assertTrue(any(event["payload"].get("artifact_id") == client.reference.id for event in await self.runner.runs.events(result.run_id)))

    async def test_incomplete_multi_tool_batch_cannot_be_checkpointed(self):
        from chagent.context.segments import exchanges
        from chagent.domain.runs import StoredMessage
        from chagent.domain.messages import ToolResultMessage
        records = [StoredMessage(uuid4(), 1, AssistantMessage([ToolCall("one", "emit", {}), ToolCall("two", "emit", {})],
                                                             "fake", "test", "test", 1, "toolUse")),
                   StoredMessage(uuid4(), 2, ToolResultMessage("one", "emit", [TextContent("one")], False, 2))]
        with self.assertRaises(ValueError):
            exchanges(records)
        records.append(StoredMessage(uuid4(), 3, ToolResultMessage("two", "emit", [TextContent("two")], False, 3)))
        self.assertEqual(len(exchanges(records)), 1)
