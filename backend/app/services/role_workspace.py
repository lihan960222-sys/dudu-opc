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
               *(base / n for n in ("key.dpapi", "steward-deepseek.dpapi", "employee-credentials", "employee-runtime", "employee-settings",
                                    "owner-oauth.sqlite3", "deepseek-codex", "feishu.json", "feishu-state", "decisions",
                                    "attachments", "engine-processes", "auth-limits"))]
    private.extend(Path(v) for v in (settings.ATTACHMENTS_DIR, settings.DECISION_LOG_DIR,
                                      settings.ENGINE_PROCESS_DIR, settings.AUTH_LIMITS_DIR) if v)
    configured_store = os.environ.get("SUPARAGENT_EMPLOYEE_RUNTIME_ROOT", "").strip()
    if configured_store and Path(configured_store).is_absolute():
        private.append(Path(configured_store))
    validate_location(folder, private)


def initialize(root: Path, model: str, instructions: str) -> None:
    validate_configured_location(root)
    path = checked_root(root) / "AGENTS.md"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(f"---\ndefault_model: {model}\n---\n\n{instructions.strip()}\n")
    except FileExistsError:
        pass
    load(root, model)


def load(root: Path, fallback_model: str | None = None) -> RoleConfig:
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
        if fallback_model and not re.match(r"^---[ \t]*(?:\n|$)", content):
            return RoleConfig(fallback_model, content.strip(), hashlib.sha256(raw).hexdigest())
        raise ValueError("AGENTS.md 的模型配置格式有误。普通 Markdown 可直接使用岗位默认模型；如使用配置头，请只填写 default_model。")
    return RoleConfig(match[1], (match[2] or "").strip(), hashlib.sha256(raw).hexdigest())


def sync_model_file(db, agent, fields):
    """Preserve the instruction bytes when a profile API or a reviewed setup changes a role's default."""
    if not agent.role_path or "model" not in fields or fields["model"] == agent.model:
        return
    from sqlalchemy import event
    from tempfile import NamedTemporaryFile
    from app.team.service import TeamError

    root = Path(agent.role_path)
    model = fields["model"]
    if not isinstance(model, str) or not re.fullmatch(r"[\w./:@+\[\]-]+", model):
        raise TeamError("role_model", "岗位目录需要明确的默认模型。")
    try:
        role = load(root, agent.model)
        if role.model == model:
            return
        if role.model != agent.model:
            raise TeamError("role_model_changed", "岗位文件已有不同的默认模型，请重新读取员工档案。")
        path = root / "AGENTS.md"
        raw = path.read_bytes()
        if path.stat().st_nlink != 1 or hashlib.sha256(raw).hexdigest() != role.digest:
            raise TeamError("role_model_changed", "岗位文件已变更，请重新读取员工档案。")
        encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
        text = raw.decode(encoding)
        newline = "\r\n" if "\r\n" in text else "\n"
        if text.startswith("---" + newline):
            text = re.sub(r"(?m)^(\s*default_model:[ \t]+)\S+", lambda m: m[1] + model, text, count=1)
        else:
            text = f"---{newline}default_model: {model}{newline}---{newline}" + text
        updated = text.encode(encoding)
        if len(updated) > 65536:
            raise TeamError("role_model", "岗位文件改后会超过 64 KiB。")

        def replace(expected, content):
            if linked(path) or path.stat().st_nlink != 1 or path.read_bytes() != expected:
                return False
            with NamedTemporaryFile(dir=root, delete=False) as tmp:
                temporary = Path(tmp.name)
                tmp.write(content)
            try:
                os.chmod(temporary, path.stat().st_mode)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return True

        if not replace(raw, updated):
            raise TeamError("role_model_changed", "岗位文件已变更，请重新读取员工档案。")
        session = db.sync_session
        pending = session.info.get("role_model_rewrites")
        if pending is None:
            pending = session.info["role_model_rewrites"] = {}

            def committed(s):
                pending.pop(s.get_transaction(), None)

            def ended(_session, transaction):
                for restore in reversed(pending.pop(transaction, [])):
                    restore()

            event.listen(session, "after_commit", committed)
            event.listen(session, "after_transaction_end", ended)
        pending.setdefault(session.get_transaction(), []).append(lambda: replace(updated, raw))
    except OSError as exc:
        raise TeamError("role_model_changed", "当前无法读写岗位文件，请核对员工目录。") from exc


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


async def validate_model(runtime, provider: str, model: str, model_vendor: str | None = None,
                         effort: str | None = None, *, private_project: bool = False) -> None:
    from app.services.engine_registry import resolve_model_profile
    profile = resolve_model_profile(provider, model_vendor, model, effort)
    runtime.check_engine_model(provider, model)
    if provider == "codex" and profile.model_vendor != "deepseek" and not private_project:
        entries = await runtime.engines.describe(only=provider)
        models = {m["id"] for entry in entries for m in entry["models"]}
        if model not in models and not any(entry.get("custom_models") for entry in entries):
            raise ValueError(f"Model {model!r} is unavailable in the Codex model list")
        option = next((m for entry in entries for m in entry["models"] if m["id"] == model), None)
        if effort and option and effort not in option.get("supported_reasoning_efforts", []):
            raise ValueError(f"Model {model!r} does not support reasoning effort {effort!r}.")
