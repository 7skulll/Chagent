"""Model-facing path projection and a final textual request guard."""

import re
from pathlib import Path

from chagent.boundaries import protected_source


VIRTUAL_ROOT = "/workspace"
#: What may follow a path and still count as "the path ends here". The last
#: alternative is a literal backslash, because the compactor serialises turns with
#: ``json.dumps``: inside that string a line break after a path is the two
#: characters ``\\n``, so the path is followed by a backslash rather than by
#: whitespace. Without it, a tool result like ``Exit code: 0\\n/tmp/ws\\n`` was
#: never projected, the guard then (correctly) refused to send it, and every
#: compaction whose tool output ended with the workspace path failed.
PATH_END = r"(?=$|/|[\s\"'`:;,)}\]!?<>|&=]|\\|\.(?=\s|$))"


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
            raise ProtectedPathError(
                "Model request contains an unmapped workspace path: "
                + _excerpt(projected, str(workspace.resolve())))
        source = str(protected_source())
        if source in projected:
            raise ProtectedPathError(
                "Model request contains a protected source path: " + _excerpt(projected, source))
        return projected
    if isinstance(value, list):
        return [protect_payload(item, workspace) for item in value]
    if isinstance(value, dict):
        return {key: protect_payload(item, workspace) for key, item in value.items()}
    return value


def _excerpt(text: str, needle: str, span: int = 24) -> str:
    """The hit and a little of what follows, bounded.

    The old message named the check but not the text, so diagnosing a refusal
    meant patching this file to print the payload. Twenty-four characters after
    the path show whether it was a JSON escape, a quote or a sentence — and are
    short enough not to spill a document into a log.
    """
    index = text.index(needle)
    return repr(text[index:index + len(needle) + span])


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
