"""Immutable text artifacts stored atomically with tool results."""

from hashlib import sha256
from uuid import UUID, uuid4

from chagent.domain.artifacts import ArtifactReference
from chagent.storage.database import Database


#: A tool result larger than this becomes a paginated artifact. The threshold
#: scales with the run's input budget instead of being pinned at one size: a
#: 64k-token window can afford to inline a whole source file, while an 8k window
#: cannot. Reading a 15k-character file used to cost fifteen turns at a 1000
#: character page size; both numbers moved together because a threshold below
#: the page size guarantees at least two round trips for every artifact.
INLINE_MIN_BYTES = 8000
INLINE_MAX_BYTES = 32000
ARTIFACT_PAGE_CHARS = 12000
ARTIFACT_PAGE_MAX = 16000


def inline_bytes_for(input_budget: int) -> int:
    """Half the input budget in bytes, clamped to the range above.

    Two bytes per budget token is deliberately generous (English prose is
    closer to four), because the cost of inlining slightly too much is one
    larger request, while the cost of paging is a whole extra model turn.
    """
    if input_budget < 1:
        raise ValueError("Input budget must be positive")
    return max(INLINE_MIN_BYTES, min(INLINE_MAX_BYTES, input_budget // 2))


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

    async def read(self, session_id: UUID, artifact_id: UUID, *, offset: int = 0, limit: int = ARTIFACT_PAGE_CHARS):
        if not 0 <= offset < 2_147_483_647 or not 1 <= limit <= ARTIFACT_PAGE_MAX:
            raise ValueError(f"Artifact page requires offset >= 0 and limit between 1 and {ARTIFACT_PAGE_MAX}")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT id, char_count, byte_count, sha256, source_truncated, substring(content FROM %s FOR %s) AS text "
                "FROM tool_artifacts WHERE id = %s AND session_id = %s", (offset + 1, limit, artifact_id, session_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("Artifact does not exist in this session")
        return {**row, "offset": offset, "next_offset": offset + len(row["text"]) if offset + len(row["text"]) < row["char_count"] else None}
