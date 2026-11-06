"""JSON-compatible values shared by messages and tool schemas."""

from typing import TypeAlias, Union


JsonValue: TypeAlias = Union[
    None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]
]
JsonObject: TypeAlias = dict[str, JsonValue]


def postgres_json_safe(value):
    """PostgreSQL jsonb cannot represent NUL, in either of the two shapes it takes.

    A real ``\\x00`` byte is one. The other is the six characters ``\\u0000``:
    valid JSON, and jsonb decodes it into the byte it cannot store, so the whole
    write is refused with "unsupported Unicode escape sequence". Tool output is
    the usual source — a command that prints ``tr '\\u0000'`` records its own
    invocation — and the write it kills is the one recording the run, which is
    how a single character in stdout ended a 24-turn build as ``storage_error``.

    Escaping the escape (``\\u0000`` becomes ``\\\\u0000``) keeps the text
    readable and storable; nothing else about the string changes.
    """
    if isinstance(value, str):
        return value.replace("\x00", "\\0").replace("\\u0000", "\\\\u0000")
    if isinstance(value, list):
        return [postgres_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [postgres_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {postgres_json_safe(key): postgres_json_safe(item) for key, item in value.items()}
    return value
