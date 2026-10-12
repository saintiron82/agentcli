"""Resolve supported Windows shims without running cmd.exe with user arguments."""

import os
import re
import shutil
from functools import lru_cache
from pathlib import Path


class UnsafeLauncherError(ValueError):
    pass


@lru_cache(maxsize=128)
def _resolve_batch(path: str, mtime_ns: int, size: int, node: str | None):
    launcher = Path(path)
    if size > 65536:
        raise UnsafeLauncherError("Oversized Windows launcher; use a native executable")
    text = launcher.read_text(encoding="utf-8-sig", errors="replace")
    # Supported direct interpreter/script wrappers (also used by offline tests).
    lines = [line.strip() for line in text.splitlines() if line.strip()
             and line.strip().lower() not in ("@echo off", "echo off")]
    if len(lines) == 1:
        match = re.fullmatch(r'@?"([^"\r\n]+\.exe)"\s+"([^"\r\n]+)"\s+%\*', lines[0], re.IGNORECASE)
        if match:
            executable, script = map(Path, match.groups())
            if executable.is_absolute() and executable.is_file() and script.is_absolute() and script.is_file():
                return (str(executable), str(script))
    # Standard npm shim: bypass its cmd control flow and launch Node directly.
    match = re.search(
        r'(?:"%_prog%"|"(?:%dp0%|%~dp0)[\\/]node\.exe"|node(?:\.exe)?)'
        r'\s+"(?:%dp0%|%~dp0)([\\/][^"\r\n]+\.(?:m?js|cjs))"\s+%\*',
        text, re.IGNORECASE)
    if match and "node" in text.lower():
        entry = (launcher.parent / match.group(1).lstrip("\\/").replace("\\", os.sep)).resolve()
        local_node = launcher.parent / "node.exe"
        executable = str(local_node) if local_node.is_file() else node
        if executable and entry.is_file():
            return (executable, str(entry))
    raise UnsafeLauncherError("Unsupported Windows batch launcher; use a native executable or standard npm shim")


def prepare_subprocess_command(cmd: list[str], *, windows: bool | None = None) -> list[str]:
    if windows is None:
        windows = os.name == "nt"
    if not windows or not cmd or Path(cmd[0]).suffix.lower() not in (".cmd", ".bat"):
        return cmd
    launcher = Path(cmd[0]).resolve()
    info = launcher.stat()
    prefix = _resolve_batch(str(launcher), info.st_mtime_ns, info.st_size, shutil.which("node.exe"))
    return [*prefix, *cmd[1:]]
