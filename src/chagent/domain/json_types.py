"""JSON-compatible values shared by messages and tool schemas."""

from typing import TypeAlias, Union


JsonValue: TypeAlias = Union[
    None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]
]
JsonObject: TypeAlias = dict[str, JsonValue]


def postgres_json_safe(value):
    """PostgreSQL jsonb cannot represent NUL, even as a JSON Unicode escape."""
    if isinstance(value, str):
        return value.replace("\x00", "\\0")
    if isinstance(value, list):
        return [postgres_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [postgres_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {postgres_json_safe(key): postgres_json_safe(item) for key, item in value.items()}
    return value
