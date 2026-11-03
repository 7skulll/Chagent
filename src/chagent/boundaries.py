"""Shared workspace and file identity boundary for all application entry points.

This is an application boundary, not protection from the machine's OS user.
"""
from pathlib import Path
import os
import sys


def protected_source() -> Path:
    package = Path(__file__).resolve().parent
    # Editable checkout: protect the entire project, not just the package.
    if package.parent.name == "src" and (package.parent.parent / "pyproject.toml").is_file():
        return package.parent.parent
    return package


def validate_workspace(path: Path) -> Path:
    target = path.expanduser().resolve(strict=True)
    if not target.is_dir():
        raise ValueError("工作区必须是目录")
    if not os.access(target, os.R_OK | os.X_OK):
        raise PermissionError("工作区目录不可读取或遍历")
    protected = protected_source()
    if target == protected or target.is_relative_to(protected) or protected.is_relative_to(target):
        raise ValueError("工作区不能是 Chagent 程序目录、其子目录或包含程序的父目录")
    for item in runtime_roots():
        if target == item or target.is_relative_to(item) or item.is_relative_to(target):
            raise ValueError("Workspace cannot overlap the runtime allowlist")
    return target


def default_workspace() -> Path:
    return Path.home() / "Chagent Workspace"


def validate_file_identity(info) -> None:
    import stat
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Only regular files are supported")
    if info.st_nlink != 1:
        raise PermissionError("Workspace files cannot be hard-linked")

def runtime_roots() -> tuple[Path, ...]:
    """Explicit read-only runtime grants, never the user's home or all /opt."""
    roots = [Path(p) for p in ("/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/share",
                               "/System/Library", "/System/iOSSupport", "/Library/Apple", "/Library/Developer/CommandLineTools", "/private/var/select")]
    prefix = Path(sys.base_prefix).resolve()
    roots += [prefix / name for name in ("bin", "lib", "include", "share")]
    return tuple(dict.fromkeys(p.resolve() for p in roots if p.exists()))

