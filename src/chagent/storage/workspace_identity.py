"""Register the workspace a session belongs to.

Identity is ``workspaces.root_path``, which on the host controller is the real
host directory and inside a container is the mounted path. The filesystem
identity that decides whether a directory is still the same directory lives in
``workspace_bindings`` (see ``docs/数据库配置.md``), not here: this module only
answers "which workspace row does this path have".
"""

from pathlib import Path
from uuid import uuid4


async def ensure_workspace(connection, workspace: Path):
    cursor = await connection.execute(
        "INSERT INTO workspaces (id, root_path) VALUES (%s, %s) "
        "ON CONFLICT (root_path) DO UPDATE SET root_path = EXCLUDED.root_path RETURNING id",
        (uuid4(), str(workspace.expanduser().resolve(strict=False))),
    )
    return (await cursor.fetchone())["id"]
