"""Workspace selection guard for the local web interface.

This is an application boundary, not protection from the machine's OS user.
"""
from pathlib import Path


def protected_source() -> Path:
    package = Path(__file__).resolve().parents[1]
    # Editable checkout: protect the entire project, not just the package.
    if package.parent.name == "src" and (package.parent.parent / "pyproject.toml").is_file():
        return package.parent.parent
    return package


def validate_workspace(path: Path) -> Path:
    target = path.expanduser().resolve(strict=True)
    if not target.is_dir():
        raise ValueError("工作区必须是目录")
    protected = protected_source()
    if target == protected or target.is_relative_to(protected) or protected.is_relative_to(target):
        raise ValueError("工作区不能是 Chagent 程序目录、其子目录或包含程序的父目录")
    return target


def default_workspace() -> Path:
    return Path.home() / "Chagent Workspace"
