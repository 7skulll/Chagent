"""A compact reference to an immutable, session-owned tool output."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    id: str
    char_count: int
    byte_count: int
    sha256: str
    preview: str
    source_truncated: bool = False


def artifact_text(reference: ArtifactReference) -> str:
    return (f"Tool output saved as artifact {reference.id}; {reference.char_count} characters, "
            f"{reference.byte_count} bytes; SHA256 {reference.sha256}. "
            f"Source truncated: {reference.source_truncated}. "
            "Use read_artifact with artifact_id, offset (0-based characters), limit (up to 1000) to read more.\n"
            "[Excerpt; omitted text remains in the artifact]\n" + reference.preview)
