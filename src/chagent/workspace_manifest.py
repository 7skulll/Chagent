"""Bounded, symlink-safe evidence of regular files in a workspace."""

import hashlib
import errno
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

MAX_FILES = 50_000
MAX_HASH_BYTES = 2 * 1024 * 1024 * 1024

#: How many digests one workspace may remember. A scan never holds more than
#: MAX_FILES files anyway, so this is a memory ceiling, not a tuning knob.
CACHE_ENTRIES = 50_000

#: How many slices of the hash list run at once. Measured on a local SSD with
#: 2 014 small files: 1 worker 106 ms, 2 workers 102 ms, 4 workers 143 ms,
#: 8 workers 180 ms — per-file hand-offs cost more than the reads they overlap,
#: so the default is serial. Raise it for a workspace on a high-latency volume
#: (network mount, endpoint-scanned directory), where one open can cost
#: milliseconds and overlapping is the only way to hide it.
HASH_WORKERS = 1


class ManifestCache:
    """Reuse a file's digest while its identity and metadata are unchanged.

    One run inventories the workspace twice, and consecutive runs usually see
    the same thousands of unchanged files. Re-hashing all of them every turn was
    the largest fixed cost of a chat turn, so an unchanged file now yields its
    previous digest instead of being read again.

    The key is the tuple the filesystem itself uses to mean "the same file,
    untouched": device, inode, size, mtime and ctime. A rewrite that preserved
    every one of those would be missed, which is why this is an in-process
    optimisation and never the evidence itself — the diff still comes from the
    digests, and a scan that cannot prove it is complete still says so.
    """

    def __init__(self, limit: int | None = None) -> None:
        self._entries: dict[str, tuple[tuple, str]] = {}
        # Not MAX_FILES: that constant is the scan's ceiling and is patched by
        # tests, while this is how much memory the process is willing to hold.
        self._limit = CACHE_ENTRIES if limit is None else limit
        self._lock = Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def lookup(self, path: str, metadata: tuple) -> str | None:
        with self._lock:
            entry = self._entries.get(path)
        return entry[1] if entry is not None and entry[0] == metadata else None

    def remember(self, path: str, metadata: tuple, digest: str) -> None:
        with self._lock:
            self._entries[path] = (metadata, digest)
            if len(self._entries) > self._limit:
                # A long-lived process may see many workspaces. Dropping the
                # older half costs one re-hash on the next scan and keeps the
                # map bounded without deciding which workspace deserves it.
                for stale in list(self._entries)[: self._limit // 2]:
                    del self._entries[stale]

    def retain(self, seen: set[str]) -> None:
        """Forget every path the last complete scan did not see again."""
        with self._lock:
            for path in [known for known in self._entries if known not in seen]:
                del self._entries[path]


class _NotRegular(Exception):
    """A path stopped being a regular file between listing and opening."""


def _metadata(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _reason(error: OSError) -> str:
    if isinstance(error, FileNotFoundError):
        return "disappeared_during_scan"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if error.errno == errno.ELOOP:
        return "symlink"
    return type(error).__name__


def digest_file(path: Path) -> tuple[str, os.stat_result, os.stat_result]:
    """Hash one file without following a link, reporting the stats it saw."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise _NotRegular
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest(), before, os.fstat(fd)
    finally:
        os.close(fd)


def _hash_one(path: Path) -> tuple[str | None, str | None]:
    """(digest, reason). Exactly one of the two is set."""
    try:
        digest, before, after = digest_file(path)
    except _NotRegular:
        return None, None
    except OSError as error:
        # A link that replaced a regular file is a skip, not a broken scan: the
        # listing already refused to follow links, so this only closes the race.
        return None, _reason(error)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        return None, "changed_during_scan"
    try:
        current = path.lstat()
    except OSError as error:
        return None, _reason(error)
    if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
        return None, "changed_during_scan"
    return digest, None


def _hash_chunk(items: list[tuple[str, Path]]) -> list[tuple[str | None, str | None]]:
    return [_hash_one(path) for _, path in items]


def snapshot(root: Path, cache: ManifestCache | None = None) -> dict:
    """Return a relative-path inventory; report partial scans explicitly."""
    root = Path(root)
    issues: list[dict] = []
    skipped_symlinks = 0
    total_bytes = 0
    candidates: list[tuple[str, Path, os.stat_result]] = []

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
        over_limit = False
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if len(candidates) >= MAX_FILES or total_bytes >= MAX_HASH_BYTES:
                issues.append({"path": relative, "reason": "scan_limit"})
                over_limit = True
                break
            try:
                # Listing first is what makes the cache possible: an unchanged
                # file is settled by one stat, and only a changed one is opened.
                info = path.lstat()
            except OSError as error:
                issues.append({"path": relative, "reason": _reason(error)})
                continue
            if stat.S_ISLNK(info.st_mode):
                skipped_symlinks += 1
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if total_bytes + info.st_size > MAX_HASH_BYTES:
                issues.append({"path": relative, "reason": "scan_limit"})
                over_limit = True
                break
            candidates.append((relative, path, info))
            total_bytes += info.st_size
        if over_limit:
            break

    digests: dict[str, str] = {}
    reused: set[str] = set()
    misses: list[tuple[str, Path]] = []
    for relative, path, info in candidates:
        remembered = cache.lookup(relative, _metadata(info)) if cache is not None else None
        if remembered is None:
            misses.append((relative, path))
        else:
            digests[relative] = remembered
            reused.add(relative)

    if misses:
        # One future per file costs more than the read it schedules on a fast
        # local disk, so the work is split into one contiguous slice per worker
        # instead: few hand-offs, same ordering, same per-file handling.
        size = max(1, -(-len(misses) // min(HASH_WORKERS, len(misses))))
        chunks = [misses[start:start + size] for start in range(0, len(misses), size)]
        if len(chunks) == 1:
            outcomes = [_hash_chunk(misses)]
        else:
            with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
                outcomes = list(pool.map(_hash_chunk, chunks))
        for (relative, _), (digest, problem) in zip(misses, [item for chunk in outcomes for item in chunk]):
            if digest is not None:
                digests[relative] = digest
            elif problem == "symlink":
                skipped_symlinks += 1
            elif problem is not None:
                issues.append({"path": relative, "reason": problem})

    entries: list[dict] = []
    seen: set[str] = set()
    for relative, path, info in candidates:
        digest = digests.get(relative)
        if digest is None:
            continue
        metadata = _metadata(info)
        if relative in reused:
            # A reused digest has no open/read of its own, so the file is
            # re-stat'ed once the scan is over: a change that landed while the
            # rest of the tree was being read is still reported, not blessed.
            try:
                if _metadata(path.lstat()) != metadata:
                    issues.append({"path": relative, "reason": "changed_during_scan"})
                    continue
            except OSError as error:
                issues.append({"path": relative, "reason": _reason(error)})
                continue
        entries.append({"path": relative, "size": info.st_size, "sha256": digest})
        seen.add(relative)
        if cache is not None:
            cache.remember(relative, metadata, digest)

    complete = not issues
    if cache is not None and complete:
        # Only a scan that saw the whole tree may declare the rest gone.
        cache.retain(seen)
    return {"files": entries, "complete": complete, "issues": issues,
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
