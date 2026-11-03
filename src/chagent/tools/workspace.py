"""Small POSIX filesystem accessor using directory-relative, no-follow operations.

This is a file-tool boundary, not an OS sandbox for arbitrary processes.
"""

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import stat
from threading import RLock
from functools import wraps
from uuid import uuid4

from chagent.boundaries import validate_workspace, validate_file_identity

_LOCKS = {}
_LOCK_GUARD = RLock()

def workspace_lock(root):
    with _LOCK_GUARD:
        return _LOCKS.setdefault(str(root), RLock())

def coordinated(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with workspace_lock(self.root):
            return method(self, *args, **kwargs)
    return call


MAX_FILE_BYTES = 1_048_576


class WorkspaceFS:
    def __init__(self, root: Path, *, expected_identity: tuple[int, int] | None = None):
        if os.name != "posix":
            raise RuntimeError("Workspace file tools currently require POSIX directory descriptors")
        self.logical_root = root.expanduser().absolute()
        self.root = validate_workspace(self.logical_root)
        self.identity = (self.root.stat().st_dev, self.root.stat().st_ino)
        if expected_identity is not None and self.identity != expected_identity:
            raise PermissionError("Workspace directory was replaced")
        if not self.root.is_dir():
            raise NotADirectoryError("Workspace must be a directory")

    def parts(self, path: str) -> tuple[str, ...]:
        value = Path(path)
        if value.is_absolute():
            try:
                value = value.relative_to(self.logical_root)
            except ValueError:
                try:
                    value = value.relative_to(self.root)
                except ValueError:
                    raise PermissionError("Path is outside the session workspace") from None
        if ".." in value.parts:
            raise PermissionError("Parent traversal is not allowed")
        if value.parts[:2] == (".chagent", "trash"):
            raise PermissionError("Workspace trash is reserved for file recovery")
        return value.parts

    @contextmanager
    def directory(self, parts: tuple[str, ...]):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if validate_workspace(self.logical_root) != self.root:
            raise PermissionError("Workspace path changed")
        descriptor = os.open(self.root, flags)
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != self.identity:
                raise PermissionError("Workspace directory was replaced")
            for part in parts:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def parent(self, path: str):
        parts = self.parts(path)
        if not parts:
            raise ValueError("A file path is required")
        with self.directory(parts[:-1]) as descriptor:
            yield descriptor, parts[-1]

    def _read_at(self, parent: int, name: str, limit: int = MAX_FILE_BYTES) -> tuple[bytes, int]:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            validate_file_identity(info)
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError(f"File exceeds the {limit} byte file-tool limit")
            return data, stat.S_IMODE(info.st_mode) & 0o777

    def read(self, path: str, *, limit: int = MAX_FILE_BYTES) -> bytes:
        """Read one file; ``limit`` is raised only for formats that get parsed."""
        with self.parent(path) as (parent, name):
            return self._read_at(parent, name, limit)[0]

    @coordinated
    def write(
        self, path: str, data: bytes, *, overwrite: bool = False, expected_hash: str | None = None,
        limit: int = MAX_FILE_BYTES,
    ) -> str:
        if len(data) > limit:
            raise ValueError(f"Content exceeds the {limit} byte limit")
        with self.parent(path) as (parent, name):
            mode = 0o600
            try:
                # Same limit as the write, so an oversized existing file is
                # reported as "exists" rather than as a size violation.
                current, mode = self._read_at(parent, name, limit)
            except FileNotFoundError:
                if expected_hash is not None:
                    raise ValueError("Expected file no longer exists") from None
            else:
                if not overwrite:
                    raise FileExistsError("File exists; explicit overwrite is required")
                if expected_hash is not None and sha256(current).hexdigest() != expected_hash:
                    raise ValueError("File changed since it was read; read it again before editing")
            temporary = ".chagent-" + uuid4().hex
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), mode)
                    os.fsync(stream.fileno())
                if overwrite:
                    # Replacement never writes through the destination inode or a hard link.
                    if expected_hash is not None:
                        latest, _ = self._read_at(parent, name, limit)
                        if sha256(latest).hexdigest() != expected_hash:
                            raise ValueError("File changed while preparing the edit")
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return sha256(data).hexdigest()

    @coordinated
    def remove(self, path: str, *, expected_hash: str | None = None) -> None:
        """Unlink one regular file; symlinks and directories are never followed."""
        with self.parent(path) as (parent, name):
            current, _ = self._read_at(parent, name)
            if expected_hash is not None and sha256(current).hexdigest() != expected_hash:
                raise ValueError("File changed since it was read")
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)

    def walk(self, parts=(), *, skipped=frozenset(), max_paths=50_000, max_depth=None):
        pending = [parts]
        visited = 0
        while pending:
            current = pending.pop()
            with self.directory(current) as descriptor:
                with os.scandir(descriptor) as entries:
                    for entry in sorted(entries, key=lambda e: e.name):
                        visited += 1
                        if visited > max_paths:
                            raise ValueError("Workspace search path limit exceeded; narrow the base")
                        info = entry.stat(follow_symlinks=False)
                        path = (*current, entry.name)
                        if stat.S_ISLNK(info.st_mode):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if entry.name not in skipped and (max_depth is None or len(path) < max_depth):
                                pending.append(path)
                            yield path, True
                        elif stat.S_ISREG(info.st_mode):
                            if info.st_nlink == 1:
                                yield path, False
