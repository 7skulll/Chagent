"""Immutable text artifacts stored atomically with tool results."""

from hashlib import sha256
from uuid import UUID, uuid4

from chagent.domain.artifacts import ArtifactReference
from chagent.storage.database import Database


INLINE_BYTES = 8000


class ArtifactRepository:
    def __init__(self, database: Database):
        self.database = database

    async def create(self, connection, *, session_id: UUID, run_id: UUID, tool_record_id: UUID,
                     text: str, source_truncated: bool = False) -> ArtifactReference:
        data, identity = text.encode("utf-8"), uuid4()
        reference = ArtifactReference(str(identity), len(text), len(data), sha256(data).hexdigest(),
                                      text[:800] + "\n[…]\n" + text[-200:], source_truncated)
        await connection.execute(
            "INSERT INTO tool_artifacts (id, session_id, run_id, tool_record_id, content, char_count, byte_count, sha256, source_truncated) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (identity, session_id, run_id, tool_record_id, text, len(text), len(data), reference.sha256, source_truncated),
        )
        return reference

    async def read(self, session_id: UUID, artifact_id: UUID, *, offset: int = 0, limit: int = 1000):
        if not 0 <= offset < 2_147_483_647 or not 1 <= limit <= 1000:
            raise ValueError("Artifact page requires offset >= 0 and limit between 1 and 1000")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT id, char_count, byte_count, sha256, source_truncated, substring(content FROM %s FOR %s) AS text "
                "FROM tool_artifacts WHERE id = %s AND session_id = %s", (offset + 1, limit, artifact_id, session_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("Artifact does not exist in this session")
        return {**row, "offset": offset, "next_offset": offset + len(row["text"]) if offset + len(row["text"]) < row["char_count"] else None}
