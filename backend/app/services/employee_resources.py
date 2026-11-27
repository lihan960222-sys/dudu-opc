"""Owner-scoped public references and local deliveries for a live employee job."""
import asyncio
import hashlib
import os
from pathlib import Path
import re
import tempfile

from sqlalchemy import select

from app.db.models import AgentJobEvent
from app.feishu import document_access, public_reference
from app.services import agent_memory, employee_project_files, feature_files, workspace_files

READ = "read_task_reference"
DOWNLOAD = "download_task_reference"
DELIVER = "deliver_project_file"
MAX_BYTES = 32 * 1024 * 1024


def tool_names(reviewed, ctx):
    if "list_project_files" not in reviewed:
        return ()
    if ctx.project_runtime.writable and ctx.approval_mode != "plan":
        return READ, DOWNLOAD, DELIVER
    return (READ,)


def project_integrations(ctx):
    """Only explicitly granted integrations are added beside native Codex project tools."""
    from app.services import employee_terminal
    reviewed = employee_terminal.allowed_tools(ctx)
    names = tool_names(reviewed, ctx)
    if DELIVER in names:
        return (*names, "use_project_release")
    return names


def model_result(value):
    """Redact only on the model-facing copy, leaving local files and digests intact."""
    if isinstance(value, dict):
        return {k: ("[redacted]" if str(k).lower() in {
            "password", "passwd", "passphrase", "secret", "api_key", "apikey", "access_token",
            "refresh_token", "client_secret", "private_key", "authorization", "cookie", "set-cookie",
        } else model_result(v)) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [model_result(v) for v in value]
    return agent_memory.redact_secrets(value)[0] if isinstance(value, str) else value


def source_url(proof, url):
    messages = [proof.get("text", ""), (proof.get("quoted_owner") or {}).get("text", "")]
    if not isinstance(url, str) or not any(url in document_access.urls(text) for text in messages):
        raise ValueError("该网址没有出现在本工单的老板原话或引用中。可用模型的公开搜索查资料。")
    return url


def _name(name):
    from pathlib import PureWindowsPath
    if (not isinstance(name, str) or not 0 < len(name) <= 200 or name.startswith(".")
            or name.endswith((" ", ".")) or PureWindowsPath(name).name != name
            or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
            or feature_files.ntpath.isreserved(name) or workspace_files.is_secret((name,))):
        raise ValueError("请指定普通文件名。")
    return name


def _pin_folder(folder):
    # Use the closest stable user root. A standard Windows account can open an
    # authorized destination even when it cannot open the volume or C:\Users.
    bases = (Path(tempfile.gettempdir()), *(Path.home() / name for name in ("Documents", "Downloads", "Desktop")))
    base = next((p for p in bases if folder.is_relative_to(p)), folder)
    return employee_project_files.pinned_directory(base, folder)


def save_new(folder, name, data):
    """Publish complete bytes without clobbering a concurrent delivery; replay reuses its copy."""
    name = _name(name)
    digest = hashlib.sha256(data).hexdigest()
    with _pin_folder(folder):
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".dudu-delivery-", delete=False) as output:
            staged = Path(output.name)
            try:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                output.close()
                staged.unlink(missing_ok=True)
                raise
        try:
            target = folder / name
            for index in range(100):
                if index:
                    suffix = digest[:12] if index == 1 else f"{digest[:12]}-{index}"
                    target = folder / f"{Path(name).stem}-{suffix}{Path(name).suffix}"
                try:
                    if os.name == "nt":
                        os.rename(staged, target)  # Windows rename fails if the destination exists.
                    else:
                        os.link(staged, target)
                    return target
                except FileExistsError:
                    if target.is_file() and not target.is_symlink() and target.stat().st_size == len(data):
                        fd, info, final = workspace_files._open_confined(folder, target)
                        with os.fdopen(fd, "rb") as existing:
                            if info.st_nlink == 1 and final == target and existing.read(len(data) + 1) == data:
                                return target
            raise ValueError("目标目录同名版本过多，请指定新名称。")
        finally:
            staged.unlink(missing_ok=True)


def external_folder(project, value, filename):
    from app.core.config import settings
    from app.services import employee_runtime, role_workspace
    if not isinstance(value, str):
        raise ValueError("请指定老板选择的本地交付目录。")
    folder = role_workspace.checked_root(Path(value))
    target = folder / _name(filename)
    role_workspace.validate_configured_location(target)
    role_workspace.validate_location(target, (settings.SQLITE_FILE.parent.resolve(),
        employee_runtime._service_root(), *(p for p in project.employee_folders if p != project.root),
        *(Path(os.environ[k]) for k in ("SYSTEMROOT", "ProgramFiles", "ProgramFiles(x86)", "APPDATA", "LOCALAPPDATA")
          if os.environ.get(k))))
    return folder


def deliver(project, args):
    if set(args) - {"source", "folder", "sha256", "name", "previous_sha256"}:
        raise ValueError("交付参数无效。")
    parts = workspace_files.clean_relative(args.get("source"))
    if not parts or workspace_files.is_secret(parts):
        raise ValueError("只可交付当前项目中的普通产物。")
    path = feature_files.checked_relative(project.root, "/".join(parts))
    name = _name(args.get("name", path.name))
    folder = external_folder(project, args.get("folder"), name)
    with employee_project_files.pinned_parent(project.root, path):
        fd, info, final = workspace_files._open_confined(project.root, path)
        with os.fdopen(fd, "rb") as stream:
            if info.st_size > MAX_BYTES or info.st_nlink != 1 or final != path:
                raise ValueError("产物过大、被链接或发生了变化。")
            data = stream.read(MAX_BYTES + 1)
    digest = hashlib.sha256(data).hexdigest()
    if len(data) > MAX_BYTES or digest != args.get("sha256"):
        raise ValueError("产物已变化，请重新核对后交付。")
    previous = args.get("previous_sha256")
    if previous is None:
        target = save_new(folder, name, data)
    else:
        if not isinstance(previous, str) or not re.fullmatch("[a-f0-9]{64}", previous):
            raise ValueError("更新指定文件需要原版本的摘要。")
        target = folder / name
        with _pin_folder(folder):
            fd, info, final = workspace_files._open_confined(folder, target)
            with os.fdopen(fd, "rb") as stream:
                if info.st_nlink != 1 or final != target or info.st_size > MAX_BYTES:
                    raise ValueError("目标文件不可更新。")
                actual = hashlib.sha256(stream.read(MAX_BYTES + 1)).hexdigest()
            if actual != digest:
                if actual != previous:
                    raise ValueError("目标已有新版本，原样保留，请重新核对。")
                with tempfile.NamedTemporaryFile(dir=folder, delete=False) as output:
                    staged = Path(output.name)
                    try:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                        output.close()
                        current = feature_files.checked(target).stat()
                        if (not os.path.samestat(current, info) or current.st_mtime_ns != info.st_mtime_ns
                                or current.st_size != info.st_size):
                            raise ValueError("发布时目标版本已变化。")
                        if os.name == "nt":
                            import ctypes
                            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                            if not kernel.ReplaceFileW(str(target), str(staged), None, 0, None, None):
                                raise ctypes.WinError(ctypes.get_last_error())
                        else:
                            staged.chmod(info.st_mode & 0o777)
                            os.replace(staged, target)
                    finally:
                        output.close()
                        staged.unlink(missing_ok=True)
    return {"path": str(target), "sha256": digest, "bytes": len(data)}


async def execute(rt, run, name, args):
    from app.services import employee_runtime, employee_terminal
    from app.team import service
    from app.team.steward_review import current_native_job

    async def live(db):
        if (name not in tool_names(employee_terminal.allowed_tools(run.ctx, live=True), run.ctx)
                or not getattr(run, "recorder", None) or run.recorder.lost):
            raise service.TeamError("outside_scope", "这次派工的工具授权已结束。")
        job = await current_native_job(db, run, write=name != READ, lock=True)
        if not job:
            raise service.TeamError("outside_scope", "原工单的员工或任务范围已变更。")
        project = await employee_runtime.checked_active_project(db, run)
        return job, project

    try:
        async with rt.session() as db:
            if name == DELIVER:
                from app.db.transactions import begin_write
                await begin_write(db)
            job, project = await live(db)
            if name == DELIVER:
                return await asyncio.to_thread(deliver, project, args)
            proof = await db.scalar(select(AgentJobEvent.detail).where(
                AgentJobEvent.job_id == job.id, AgentJobEvent.kind == "owner_request").order_by(AgentJobEvent.id.desc()).limit(1))
            url = source_url(proof or {}, args.get("url"))
        if name == READ and not args.keys() - {"url", "offset"}:
            result = await public_reference.read(url, args.get("offset", 0))
            async with rt.session() as db:
                await live(db)
            return result
        if name == DOWNLOAD and set(args) == {"url", "name"}:
            filename = _name(args["name"])
            _, data, _, _ = await public_reference.fetch(url, download=True)
            async with rt.session() as db:
                _, project = await live(db)
                target = await asyncio.to_thread(save_new, project.task_dir, filename, data)
            return {"path": str(target), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        raise ValueError("公开资料工具参数无效。")
    except (ValueError, OSError, workspace_files.FileAccessError) as exc:
        raise service.TeamError("employee_resource_unavailable", str(exc)) from None
