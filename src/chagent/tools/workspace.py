"""Small POSIX filesystem accessor using directory-relative, no-follow operations.

This is a file-tool boundary, not an OS sandbox for arbitrary processes.
"""

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import stat
from uuid import uuid4


MAX_FILE_BYTES = 1_048_576


class WorkspaceFS:
    def __init__(self, root: Path):
        if os.name != "posix":
            raise RuntimeError("Workspace file tools currently require POSIX directory descriptors")
        self.logical_root = root.expanduser().absolute()
        self.root = self.logical_root.resolve(strict=True)
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
        return value.parts

    @contextmanager
    def directory(self, parts: tuple[str, ...]):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(self.root, flags)
        try:
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
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Only regular files are supported")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError(f"File exceeds the {limit} byte file-tool limit")
            return data, stat.S_IMODE(info.st_mode) & 0o777

    def read(self, path: str, *, limit: int = MAX_FILE_BYTES) -> bytes:
        """Read one file; ``limit`` is raised only for formats that get parsed."""
        with self.parent(path) as (parent, name):
            return self._read_at(parent, name, limit)[0]

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
                        latest, _ = self._read_at(parent, name)
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

    def remove(self, path: str, *, expected_hash: str | None = None) -> None:
        """Unlink one regular file; symlinks and directories are never followed."""
        with self.parent(path) as (parent, name):
            current, _ = self._read_at(parent, name)
            if expected_hash is not None and sha256(current).hexdigest() != expected_hash:
                raise ValueError("File changed since it was read")
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
