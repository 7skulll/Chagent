"""Resolve a runtime directory to an independently persisted host identity."""

import os
from pathlib import Path
from uuid import uuid4


def host_path_for(workspace: Path) -> str:
    root = workspace.expanduser().resolve(strict=False)
    mounted = os.environ.get("CHAGENT_WORKSPACE_ROOT")
    if not mounted:
        return str(root)
    runtime_root = Path(mounted).expanduser().resolve(strict=False)
    if not root.is_relative_to(runtime_root):
        raise ValueError("Workspace is outside the configured runtime mount")
    declared = os.environ.get("CHAGENT_HOST_WORKSPACE_PATH", "").strip()
    if not declared:
        raise ValueError("Host workspace path is required for a mounted workspace")
    host_root = Path(declared).expanduser()
    if not host_root.is_absolute() or ".." in host_root.parts:
        raise ValueError("Host workspace path must be an absolute canonical path")
    # The host directory is not accessible from the container. Its canonical
    # spelling is supplied by the host launcher, never by a model or web request.
    return str(host_root / root.relative_to(runtime_root))


async def ensure_workspace(connection, workspace: Path):
    runtime = str(workspace.expanduser().resolve(strict=False))
    host = host_path_for(workspace)
    cursor = await connection.execute(
        "INSERT INTO workspaces (id, root_path, host_path, binding_status) "
        "VALUES (%s, %s, %s, 'bound') ON CONFLICT (host_path) "
        "WHERE binding_status = 'bound' DO UPDATE SET root_path = EXCLUDED.root_path "
        "RETURNING id", (uuid4(), runtime, host),
    )
    return (await cursor.fetchone())["id"]
