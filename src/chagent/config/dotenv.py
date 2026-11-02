"""Read a ``.env`` file so a silently shadowed value can be named.

``uv run --env-file`` does not replace variables that already exist, so a stale
shell export wins without any message. The consequence has already shown up
for example, a shell ``CHAGENT_MODEL`` can make reports disagree with the file.
Comparing the file with the process environment turns both into one visible
line. This module only reads and compares; it never writes or exports anything.
"""

from collections.abc import Mapping
from pathlib import Path

# Mentioned only when present in the environment but absent from the file:
# these override a preset or a file value from outside the file's control.
_AMBIENT_KEYS = ("CHAGENT_API_KEY", "CHAGENT_PROVIDER", "CHAGENT_MODEL", "CHAGENT_BASE_URL")


def read(path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; comments, blanks and one export prefix are skipped."""
    values: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def is_secret(key: str) -> bool:
    return key.endswith("_KEY") or key.endswith("_TOKEN") or "DATABASE_URL" in key


def shadowed(process_env: Mapping[str, str], file_env: Mapping[str, str]) -> list[dict]:
    """Values in the environment that win over the file, or exist outside it."""
    entries = []
    for key in sorted(file_env):
        if key not in process_env:
            continue
        if process_env[key].strip() == file_env[key].strip():
            continue
        secret = is_secret(key)
        entries.append({"key": key, "kind": "overridden", "redacted": secret,
                        "file": "<set>" if secret else file_env[key],
                        "environment": "<set>" if secret else process_env[key]})
    for key in _AMBIENT_KEYS:
        if key in process_env and key not in file_env:
            entries.append({"key": key, "kind": "shell-only", "redacted": is_secret(key),
                            "file": None, "environment": "<set>" if is_secret(key) else process_env[key]})
    return entries


def describe(entries: list[dict]) -> list[str]:
    """One readable line per shadowed value; never includes a secret."""
    lines = []
    for entry in entries:
        if entry["kind"] == "overridden":
            lines.append(f"{entry['key']} from .env {entry['file']!r} is overridden by environment "
                         f"{entry['environment']!r}")
        else:
            lines.append(f"{entry['key']} is set in the environment but not in .env"
                         + ("" if entry["redacted"] else f" (value {entry['environment']!r})"))
    return lines
