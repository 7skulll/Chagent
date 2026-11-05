"""The final provider payload is the only authoritative export boundary."""

import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import httpx

from chagent.boundaries import protected_source
from chagent.domain.messages import SystemMessage, ToolCall, ToolResultMessage, TextContent, UserMessage
from chagent.models.base import ModelRequest
from chagent.models.factory import create_model_client
from chagent.models.outbound import invocation_to_host, protect_payload, ProtectedPathError, to_model
from chagent.models.settings import ModelSettings, PRESETS


class OutboundPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_provider_payloads_project_workspace_before_post(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for provider in ("openai", "anthropic"):
                with self.subTest(provider=provider):
                    settings = ModelSettings.from_env({"CHAGENT_PROVIDER": provider,
                        PRESETS[provider].key_env: "test-secret"})
                    sent = []
                    def capture(request):
                        sent.append(json.loads(request.content))
                        return httpx.Response(400)
                    request = ModelRequest(settings.model(), [
                        SystemMessage(f"Directory: {root}", 0),
                        UserMessage(f"Read {root}/notes.txt", 0),
                        ToolResultMessage("call", "read_file", [TextContent(f"File: {root}/notes.txt")], False, 0),
                    ], workspace=root)
                    async with create_model_client(settings, transport=httpx.MockTransport(capture)) as client:
                        _ = [event async for event in client.stream(request)]
                    self.assertEqual(len(sent), 1)
                    body = json.dumps(sent[0], ensure_ascii=False)
                    self.assertNotIn(str(root), body)
                    self.assertIn("/workspace/notes.txt", body)

    async def test_summary_and_memory_requests_project_workspace_before_post(self):
        """Those two purposes build their own request objects.

        A request whose ``workspace`` is left unset is projected nowhere and
        guarded nowhere, so the summarizer and the memory extractor would export
        real host paths while the task loop looks protected. This pins that both
        carry a workspace and that the transport projects them.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = ModelSettings.from_env({"CHAGENT_PROVIDER": "openai", "OPENAI_API_KEY": "test-secret"})
            for purpose in ("summary", "memory"):
                with self.subTest(purpose=purpose):
                    sent = []
                    def capture(request):
                        sent.append(json.loads(request.content))
                        return httpx.Response(400)
                    request = ModelRequest(settings.model(), [
                        SystemMessage("Summarize or extract; no tools.", 0),
                        UserMessage(f"File: {root}/notes.txt changed", 0),
                    ], purpose=purpose, workspace=root, run_id=uuid4())
                    async with create_model_client(settings, transport=httpx.MockTransport(capture)) as client:
                        _ = [event async for event in client.stream(request)]
                    self.assertEqual(len(sent), 1)
                    body = json.dumps(sent[0], ensure_ascii=False)
                    self.assertNotIn(str(root), body)
                    self.assertIn("/workspace/notes.txt", body)

    async def test_a_protected_path_in_a_summary_request_is_still_blocked(self):
        # The guard is keyed on the payload, not on the purpose: a poisoned
        # history must not reach the summarizer either.
        settings = ModelSettings.from_env({"CHAGENT_PROVIDER": "openai", "OPENAI_API_KEY": "test-secret"})
        sent, audited = [], []
        def capture(request):
            sent.append(request)
            return httpx.Response(200)
        request = ModelRequest(settings.model(), [UserMessage(str(protected_source()), 0)],
                              purpose="summary", run_id=uuid4())
        async with create_model_client(settings, transport=httpx.MockTransport(capture)) as client:
            async def audit(run_id, purpose):
                audited.append((run_id, purpose))
            client.outbound_audit = audit
            _ = [event async for event in client.stream(request)]
        self.assertEqual(sent, [])
        self.assertEqual(audited, [(request.run_id, "summary")])

    async def test_protected_source_blocks_post_and_records_audit_callback(self):
        settings = ModelSettings.from_env({"CHAGENT_PROVIDER": "openai", "OPENAI_API_KEY": "test-secret"})
        sent, audited = [], []
        def capture(request):
            sent.append(request)
            return httpx.Response(200)
        request = ModelRequest(settings.model(), [UserMessage(str(protected_source()), 0)], run_id=uuid4())
        async with create_model_client(settings, transport=httpx.MockTransport(capture)) as client:
            async def audit(run_id, purpose):
                audited.append((run_id, purpose))
            client.outbound_audit = audit
            events = [event async for event in client.stream(request)]
        self.assertEqual(sent, [])
        self.assertEqual(audited, [(request.run_id, "task")])
        self.assertEqual(events[-1].type, "error")

    def test_virtual_invocation_maps_only_path_bearing_fields(self):
        root = Path("/tmp/example-workspace")
        result = invocation_to_host({"path": "/workspace/notes.txt", "cmd": "cat /workspace/notes.txt",
                                     "content": "Keep /workspace literal"}, root)
        self.assertEqual(result["path"], "notes.txt")
        self.assertEqual(result["cmd"], "cat " + str(root.resolve()) + "/notes.txt")
        self.assertEqual(result["content"], "Keep /workspace literal")
        patch = "--- /workspace/notes.txt\n+++ /workspace/notes.txt\n@@\n-old\n+new\n"
        self.assertIn("--- notes.txt\n+++ notes.txt", invocation_to_host({"patch": patch}, root)["patch"])
        with self.assertRaises(ProtectedPathError):
            protect_payload({"text": str(protected_source())}, None)

    def test_workspace_root_before_sentence_punctuation_is_projected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(to_model(f"cwd: {root}. Next step!", root),
                             "cwd: /workspace. Next step!")
