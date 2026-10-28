"""Model-visible tool declarations, independent of executable implementations."""

from dataclasses import dataclass

# JsonValue is also needed to resolve JsonObject's recursive type hints.
from chagent.domain.json_types import JsonObject, JsonValue


@dataclass(slots=True)
class Tool:
    """A tool's name, description and JSON Schema parameter declaration."""

    name: str
    description: str
    parameters: JsonObject


@dataclass(slots=True)
class ToolReference:
    name: str
