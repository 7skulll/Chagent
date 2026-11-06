"""Source-linked excerpts and a bounded workspace history index."""

import re
from dataclasses import asdict
from uuid import UUID

from psycopg.types.json import Jsonb

from chagent.domain.messages import AssistantMessage, TextContent
from chagent.storage.database import Database


def excerpt(value: str, limit: int) -> str:
    # The transcript remains the source. This short index must not copy obvious
    # access values into every later task prompt or tool result.
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", value)
    value = re.sub(r"(?i)(api[_ -]?key|password|secret|access[_ -]?token|密码|密钥)\s*[:=：]\s*\S+",
                   r"\1=[redacted]", value)
    return " ".join(value.split())[:limit]


class TaskHistoryRepository:
    def __init__(self, database: Database):
        self.database = database

    async def record(self, connection, state) -> None:
        """Save only a completed answer, in the run completion transaction."""
        source = state.current_input
        answer = next((item for item in reversed(state.history)
                       if item.run_id == state.run_id and isinstance(item.message, AssistantMessage)), None)
        if answer is None or not isinstance(source.message.content, str):
            return
        text = "\n".join(block.text for block in answer.message.content if isinstance(block, TextContent))
        if not text.strip():
            return
        await connection.execute(
            "INSERT INTO task_recaps (run_id, session_id, workspace_id, user_message_id, answer_message_id, request_excerpt, answer_excerpt, task_state) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
            (state.run_id, state.session.id, state.session.workspace_id, source.id, answer.id,
             excerpt(source.message.content, 260), excerpt(text, 600), Jsonb(asdict(state.task_state))),
        )

    async def search(self, workspace_id: UUID, query: str = "", limit: int = 12) -> list[dict]:
        if not 1 <= limit <= 20 or len(query) > 300:
            raise ValueError("Invalid task history search")
        terms = [item.lower() for item in re.findall(r"[a-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", query)][:12]
        if any(len(item) > 2 and re.search(r"[\u4e00-\u9fff]", item) for item in terms):
            terms = [part for item in terms for part in
                     ([item[i:i+2] for i in range(len(item)-1)] if re.search(r"[\u4e00-\u9fff]", item) and len(item)>2 else [item])][:12]
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT t.run_id, t.session_id, t.request_excerpt, t.created_at "
                "FROM task_recaps t JOIN workspaces w ON w.id = t.workspace_id AND w.binding_status = 'bound' "
                "JOIN messages a ON a.id = t.answer_message_id AND a.superseded_by IS NULL "
                "WHERE t.workspace_id = %s AND (%s::text[] = '{}' OR EXISTS "
                "(SELECT 1 FROM unnest(%s::text[]) term WHERE strpos(lower(t.request_excerpt || ' ' || t.answer_excerpt), term) > 0)) "
                "ORDER BY t.created_at DESC, t.run_id DESC LIMIT %s",
                (workspace_id, terms, terms, limit),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get(self, workspace_id: UUID, run_id: UUID) -> dict:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT t.*, u.payload AS user_payload, a.payload AS answer_payload "
                "FROM task_recaps t JOIN workspaces w ON w.id = t.workspace_id AND w.binding_status = 'bound' "
                "JOIN messages u ON u.id = t.user_message_id "
                "JOIN messages a ON a.id = t.answer_message_id AND a.superseded_by IS NULL "
                "WHERE t.workspace_id = %s AND t.run_id = %s", (workspace_id, run_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise LookupError("Task recap does not exist in this workspace")
            result = dict(row)
            result["run_outcome"] = "answer_recorded"
            result["task_completion"] = "unknown_without_current_verification"
            user = result.pop("user_payload")
            answer = result.pop("answer_payload")
            result["request_detail"] = excerpt(user.get("content", ""), 2000) if isinstance(user.get("content"), str) else ""
            blocks = answer.get("content", [])
            result["answer_detail"] = excerpt("\n".join(block.get("text", "") for block in blocks
                                                          if isinstance(block, dict) and block.get("type") == "text"), 3000)
            return result
