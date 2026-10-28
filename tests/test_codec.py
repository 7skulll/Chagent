from dataclasses import asdict
import json
import unittest

from chagent.domain.codec import message_from_dict, message_to_dict
from chagent.domain.messages import AssistantMessage, SystemMessage, ThinkingContent, ToolCall, ToolResultMessage, TextContent
from chagent.domain.tools import Tool, ToolReference
from chagent.domain.usage import Usage, UsageCost


class CodecTests(unittest.TestCase):
    def test_provider_metadata_and_tool_declarations_survive_json_round_trip(self):
        messages = [
            SystemMessage("Instructions", 1, sections={"rules": "x"}, tools_added=[Tool("echo", "Echo", {"type": "object"})], tools_removed=[ToolReference("old")]),
            AssistantMessage(
                [ThinkingContent("", "opaque-thinking", redacted=True), ToolCall("a", "echo", {"nested": [None, True, {"x": 1}]}, thought_signature="opaque-tool", namespace="local")],
                "test-api", "test-provider", "requested-model", 2, "toolUse",
                usage=Usage(10, 5, 2, 1, 18, cost=UsageCost(1, 2, 3, 4, 10), reasoning=3),
                response_model="actual-model", response_id="response-1",
            ),
            ToolResultMessage("a", "echo", [TextContent("result")], False, 3, details={"ui": "not model text"}),
        ]
        for message in messages:
            restored = message_from_dict(json.loads(json.dumps(message_to_dict(message))))
            self.assertIs(type(restored), type(message))
            self.assertEqual(asdict(restored), asdict(message))

    def test_reject_unknown_roles_and_wrong_content(self):
        for payload in (
            {"role": "unknown", "content": "x", "timestamp": 1},
            {"role": "user", "content": [{"type": "toolCall", "id": "a", "name": "x", "arguments": {}}], "timestamp": 1},
        ):
            with self.assertRaises(ValueError):
                message_from_dict(payload)
