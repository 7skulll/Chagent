"""Accept one file from the browser and put it where the file tools can read it.

The upload lands inside the session workspace on purpose: ``read_file``,
``grep`` and ``exec_command`` already have a bounded, symlink-safe view of that
directory, and a second storage location would need its own set of those rules.
"""

import asyncio
from hashlib import sha256
from pathlib import Path
import re

from chagent.files import FileService
from chagent.tools.workspace import WorkspaceFS


#: Directory under the workspace that receives uploads.
UPLOAD_DIRECTORY = "uploads"

#: One file at a time. Large enough for a scanned report, small enough that the
#: request cannot exhaust memory in a single-process local service.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

#: Name length after sanitising; most filesystems cap a component at 255 bytes.
MAX_NAME_CHARS = 120

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def safe_name(raw: str) -> str:
    """Reduce a client-supplied name to one harmless path component.

    Browsers send only the basename, but the query string is caller-controlled,
    so separators, traversal and control characters are removed rather than
    validated: a rejected upload is a worse outcome than a renamed one.
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL.sub("", name).strip().strip(".")
    if not name:
        return "upload"
    if len(name) > MAX_NAME_CHARS:
        stem, dot, suffix = name.rpartition(".")
        if dot and len(suffix) <= 10:
            stem = stem[: MAX_NAME_CHARS - len(suffix) - 1]
            name = f"{stem}.{suffix}"
        else:
            name = name[:MAX_NAME_CHARS]
    return name


def _candidate(name: str, attempt: int) -> str:
    if attempt == 0:
        return name
    stem, dot, suffix = name.rpartition(".")
    if dot and stem and len(suffix) <= 10:
        return f"{stem}-{attempt}.{suffix}"
    return f"{name}-{attempt}"


async def save(workspace: Path, raw_name: str, read, *, outputs=None, workspace_id=None, request_id=None) -> dict:
    """Stream one upload to ``<workspace>/uploads`` and return its descriptor.

    ``read`` is an async iterator of byte chunks; the size limit is enforced
    while reading so an oversized body is refused before it reaches disk.
    """
    filesystem = FileService(workspace)
    with filesystem.directory(()) as descriptor:
        import os
        try:
            os.mkdir(UPLOAD_DIRECTORY, mode=0o700, dir_fd=descriptor)
        except FileExistsError:
            pass
    with filesystem.directory((UPLOAD_DIRECTORY,)):
        pass

    received = bytearray()
    async for chunk in read:
        received.extend(chunk)
        if len(received) > MAX_UPLOAD_BYTES:
            raise ValueError(f"Upload exceeds the {MAX_UPLOAD_BYTES} byte limit")
    if not received:
        raise ValueError("Upload is empty")

    data = bytes(received)
    digest = sha256(data).hexdigest()
    name = safe_name(raw_name)
    if outputs is not None:
        relative = f"{UPLOAD_DIRECTORY}/{name}"
        output = await outputs.submit(workspace_id, workspace, relative, data, source='upload', request_id=request_id)
        return {"path": output['output_path'] or relative, "name": name, "size": len(data), "sha256": digest,
                "workspace_id": workspace_id, "output_id": output['id'], "status": output['status']}
    for attempt in range(100):
        candidate = _candidate(name, attempt)
        relative = f"{UPLOAD_DIRECTORY}/{candidate}"
        try:
            await asyncio.to_thread(filesystem.write, relative, data, limit=MAX_UPLOAD_BYTES)
        except FileExistsError:
            continue
        return {"path": relative, "name": candidate, "size": len(data), "sha256": digest}
    raise ValueError("Too many files with the same name in uploads/")
