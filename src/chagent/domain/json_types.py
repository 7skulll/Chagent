"""JSON-compatible values shared by messages and tool schemas."""

from typing import TypeAlias, Union


JsonValue: TypeAlias = Union[
    None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]
]
JsonObject: TypeAlias = dict[str, JsonValue]
