"""Model-facing path projection and a final textual request guard."""

import re
from pathlib import Path

from chagent.boundaries import protected_source


VIRTUAL_ROOT = "/workspace"
PATH_END = r"(?=$|/|[\s\"'`:;,)}\]!?<>|&=]|\.(?=\s|$))"


class ProtectedPathError(ValueError):
    pass


def to_model(text: str, workspace: Path | None) -> str:
    if workspace is None:
        return text
    root = str(workspace.resolve())
    return re.sub(re.escape(root) + PATH_END, VIRTUAL_ROOT, text)


def to_host(text: str, workspace: Path) -> str:
    root = str(workspace.resolve())
    return re.sub(r"(?<![\w/])" + re.escape(VIRTUAL_ROOT) +
                  PATH_END, lambda _: root, text)


def protect_payload(value, workspace: Path | None):
    """Project strings, then reject protected source paths before HTTP I/O."""
    if isinstance(value, str):
        projected = to_model(value, workspace)
        if workspace is not None and str(workspace.resolve()) in projected:
            raise ProtectedPathError("Model request contains an unmapped workspace path")
        source = str(protected_source())
        if source in projected:
            raise ProtectedPathError("Model request contains a protected source path")
        return projected
    if isinstance(value, list):
        return [protect_payload(item, workspace) for item in value]
    if isinstance(value, dict):
        return {key: protect_payload(item, workspace) for key, item in value.items()}
    return value


def invocation_to_host(arguments: dict, workspace: Path) -> dict:
    """Resolve virtual roots in path-bearing tool arguments before execution."""
    def relative(value: str) -> str:
        if value == VIRTUAL_ROOT:
            return "."
        if value.startswith(VIRTUAL_ROOT + "/"):
            return value[len(VIRTUAL_ROOT) + 1:]
        return value

    def patch_headers(value: str) -> str:
        lines = value.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith(("--- ", "+++ ")):
                lines[index] = line[:4] + relative(line[4:])
        return "".join(lines)

    converted = dict(arguments)
    for key in ("path", "filename", "target"):
        if isinstance(converted.get(key), str):
            converted[key] = relative(converted[key])
    if isinstance(converted.get("patch"), str):
        converted["patch"] = patch_headers(converted["patch"])
    for key in ("workdir", "cmd", "command"):
        if isinstance(converted.get(key), str):
            converted[key] = to_host(converted[key], workspace)
    return converted
