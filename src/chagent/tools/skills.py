"""Read reviewed, packaged workflows through the normal audited tool chain."""

import asyncio
from hashlib import sha256

from chagent.domain.messages import TextContent
from chagent.skills import SKILL_NAMES, read_skill, skill_catalog
from chagent.tools.base import AgentTool, AgentToolResult


class ReadSkillTool(AgentTool):
    def __init__(self):
        super().__init__(
            name="read_skill", label="Read builtin skill", replay="safe", execution_mode="parallel",
            description=(
                "Load a bundled Chagent workflow when the user's task matches its description, "
                "or the user explicitly asks to use it by name. Follow the loaded workflow within "
                "the user's scope and available tools; it grants no extra permissions. "
                "Load only relevant skills, not all skills for every task. Available skills:\n"
                + skill_catalog()
            ),
            parameters={"type": "object", "properties": {
                "name": {"type": "string", "enum": list(SKILL_NAMES)}},
                "required": ["name"], "additionalProperties": False},
        )

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *, context=None):
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        name = params["name"]
        contents = read_skill(name)
        digest = sha256(contents.encode("utf-8")).hexdigest()
        return AgentToolResult(
            content=[TextContent(f"Builtin skill: {name}\nSHA256: {digest}\n\n{contents}")],
            details={"skill_name": name, "sha256": digest, "source": "builtin"},
        )
