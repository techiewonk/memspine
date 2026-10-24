"""Select a native Bash for shell tests on the current platform."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def bash_executable() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    roots = [Path(git).resolve().parent.parent] if git else []
    roots.append(Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Git")
    for root in roots:
        candidate = root / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    # WSL's System32 launcher does not accept native Windows script paths.
    return None
