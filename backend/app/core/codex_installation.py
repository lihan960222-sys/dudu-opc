"""Identify an executable belonging to the installed Windows Codex Store package."""
import os
from pathlib import Path
import re


def store_codex_executable(raw: str | Path | None) -> Path | None:
    if os.name != "nt" or not raw or not os.getenv("ProgramFiles"):
        return None
    path = Path(raw)
    root = Path(os.environ["ProgramFiles"]) / "WindowsApps"
    if not path.is_absolute() or not path.is_relative_to(root):
        return None
    parts = path.relative_to(root).parts
    if (len(parts) != 4 or not re.fullmatch(r"OpenAI\.Codex_[\w.\-]+__2p2nqsd0c76g0", parts[0])
            or tuple(p.lower() for p in parts[1:]) != ("app", "resources", "codex.exe")):
        return None
    return path if path.is_file() and path.resolve() == path else None
