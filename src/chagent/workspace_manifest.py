"""Bounded, symlink-safe evidence of regular files in a workspace."""

import hashlib
import errno
import os
import stat
from pathlib import Path

MAX_FILES = 50_000
MAX_HASH_BYTES = 2 * 1024 * 1024 * 1024


def snapshot(root: Path) -> dict:
    """Return a relative-path inventory; report partial scans explicitly."""
    root = Path(root)
    entries: list[dict] = []
    issues: list[dict] = []
    total_bytes = 0
    skipped_symlinks = 0

    def walk_error(error: OSError) -> None:
        issues.append({"path": str(Path(error.filename).relative_to(root)) if error.filename else ".",
                       "reason": type(error).__name__})

    for directory, names, files in os.walk(root, topdown=True, followlinks=False, onerror=walk_error):
        names.sort()
        files.sort()
        for name in list(names):
            path = Path(directory) / name
            try:
                if path.is_symlink():
                    names.remove(name)
                    skipped_symlinks += 1
            except OSError as error:
                names.remove(name)
                issues.append({"path": path.relative_to(root).as_posix(), "reason": type(error).__name__})
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            try:
                if len(entries) >= MAX_FILES or total_bytes >= MAX_HASH_BYTES:
                    issues.append({"path": relative, "reason": "scan_limit"})
                    return {"files": entries, "complete": False, "issues": issues,
                            "skipped_symlinks": skipped_symlinks}
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode):
                        continue
                    if total_bytes + before.st_size > MAX_HASH_BYTES:
                        issues.append({"path": relative, "reason": "scan_limit"})
                        return {"files": entries, "complete": False, "issues": issues,
                                "skipped_symlinks": skipped_symlinks}
                    digest = hashlib.sha256()
                    with os.fdopen(fd, "rb", closefd=False) as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    after = os.fstat(fd)
                    current = path.lstat()
                    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
                            before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                        issues.append({"path": relative, "reason": "changed_during_scan"})
                        continue
                    entries.append({"path": relative, "size": after.st_size, "sha256": digest.hexdigest()})
                    total_bytes += after.st_size
                finally:
                    os.close(fd)
            except OSError as error:
                if isinstance(error, FileNotFoundError):
                    reason = "disappeared_during_scan"
                elif isinstance(error, PermissionError):
                    reason = "permission_denied"
                elif error.errno == errno.ELOOP:
                    skipped_symlinks += 1
                    continue
                else:
                    reason = type(error).__name__
                issues.append({"path": relative, "reason": reason})
    return {"files": entries, "complete": not issues, "issues": issues,
            "skipped_symlinks": skipped_symlinks}


def diff(before: dict, after: dict) -> dict:
    old = {item["path"]: item for item in before["files"]}
    new = {item["path"]: item for item in after["files"]}
    added = [new[path] for path in sorted(new.keys() - old.keys())]
    deleted = [old[path] for path in sorted(old.keys() - new.keys())]
    modified = [{"path": path, "before": old[path], "after": new[path]}
                for path in sorted(old.keys() & new.keys()) if old[path]["sha256"] != new[path]["sha256"]]
    return {"complete": before["complete"] and after["complete"],
            "added": added, "modified": modified, "deleted": deleted,
            "counts": {"added": len(added), "modified": len(modified), "deleted": len(deleted)}}
