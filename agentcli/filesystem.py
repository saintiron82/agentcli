"""Conservative workspace path checks (not an OS sandbox)."""

import os
import stat
from pathlib import Path


def safe_workspace_path(root: Path, target: Path) -> Path:
    lexical_root = Path(os.path.abspath(root))
    root = lexical_root.resolve()
    target = Path(os.path.abspath(target))
    try:
        parts = target.relative_to(lexical_root).parts
    except ValueError:
        try:
            parts = target.relative_to(root).parts
        except ValueError:
            raise PermissionError("Path escapes the workspace") from None
    # Map only the trusted root alias. Resolving the whole target first would
    # hide forbidden links inside the workspace from the lstat checks below.
    target = root.joinpath(*parts)
    current = root
    for part in parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(info.st_mode) or reparse:
            raise PermissionError("Workspace links/reparse points are not allowed")
    try:
        target.resolve().relative_to(root)
    except (ValueError, RuntimeError):
        raise PermissionError("Path escapes the workspace") from None
    return target


def reject_tree_links(root: Path):
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise PermissionError("Skill source links/reparse points are not allowed")
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            safe_workspace_path(root, Path(parent) / name)
