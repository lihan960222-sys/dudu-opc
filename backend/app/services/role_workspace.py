"""Local role configuration. Only the root AGENTS.md is interpreted."""
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RoleConfig:
    model: str
    instructions: str
    digest: str


def linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 1024)


def checked_root(root: Path) -> Path:
    if (not root.is_absolute() or root.drive.startswith("\\\\") or linked(root)
            or root.resolve(strict=True) != root):
        raise ValueError(f"Role folder must be a local directory without links: {root}")
    if not root.is_dir():
        raise ValueError(f"Missing role folder: {root}")
    return root


def validate_location(folder: Path, protected=()) -> None:
    home = Path.home().resolve()
    root = folder.resolve()
    if (root == Path(root.anchor) or home.is_relative_to(root)
            or root in {home / name for name in ("Documents", "Desktop", "Downloads", "AppData")}):
        raise ValueError("Choose a dedicated folder for this role.")
    private = [home / name for name in (".codex", ".aws", ".ssh", ".config", ".gnupg")]
    private.extend(protected)
    for path in private:
        other = Path(path).resolve()
        if root.is_relative_to(other) or other.is_relative_to(root):
            raise ValueError("Role folder overlaps private application data.")


def validate_configured_location(folder: Path) -> None:
    from app.core.config import settings
    base = settings.SQLITE_FILE.parent
    private = [Path(__file__).resolve().parents[3], settings.SQLITE_FILE,
               *(base / n for n in ("key.dpapi", "feishu.json", "feishu-state", "decisions",
                                    "attachments", "engine-processes", "auth-limits"))]
    private.extend(Path(v) for v in (settings.ATTACHMENTS_DIR, settings.DECISION_LOG_DIR,
                                      settings.ENGINE_PROCESS_DIR, settings.AUTH_LIMITS_DIR) if v)
    validate_location(folder, private)


def initialize(root: Path, model: str, instructions: str) -> None:
    validate_configured_location(root)
    path = checked_root(root) / "AGENTS.md"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(f"---\ndefault_model: {model}\n---\n\n{instructions.strip()}\n")
    except FileExistsError:
        pass
    load(root)


def load(root: Path) -> RoleConfig:
    validate_configured_location(root)
    path = checked_root(root) / "AGENTS.md"
    if linked(path) or not path.is_file():
        raise ValueError(f"Expected a regular file: {path}")
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError(f"{path} exceeds 64 KiB")
    content = raw.decode("utf-8-sig").replace("\r\n", "\n")
    match = re.fullmatch(r"---\n[ \t]*default_model:[ \t]+([\w./:@+\[\]-]+)[ \t]*\n---(?:\n(.*))?", content, re.S)
    if not match:
        raise ValueError(f"{path} needs a front matter block containing only default_model: MODEL")
    return RoleConfig(match[1], (match[2] or "").strip(), hashlib.sha256(raw).hexdigest())


def inventory(root: Path, task_id: str | None = None) -> list[dict]:
    checked_root(root)
    rows = []
    for base, dirs, files in os.walk(root):
        folder = Path(base)
        dirs[:] = [d for d in dirs if not d.startswith(".") and not linked(folder / d)
                   and d not in {"node_modules", "__pycache__"}
                   and not (folder == root and d.casefold() in {"memory", "skills"})
                   and (folder != root / "任务" or d == task_id)]
        for name in files:
            path = folder / name
            if name.startswith(".") or linked(path) or not path.is_file():
                continue
            if folder == root / "任务" or (folder == root and name.casefold() in {"agents.md", "memory.md"}):
                continue
            info = path.stat()
            rows.append({"path": path.relative_to(root).as_posix(), "size": info.st_size,
                         "mtime_ns": info.st_mtime_ns})
    return sorted(rows, key=lambda r: r["path"])


async def validate_model(runtime, provider: str, model: str) -> None:
    runtime.check_engine_model(provider, model)
    if provider == "codex":
        entries = await runtime.engines.describe(only=provider)
        models = {m["id"] for entry in entries for m in entry["models"]}
        if not models or model not in models:
            raise ValueError(f"Model {model!r} is unavailable in the Codex model list")
