"""One declared Windows project check, executed read-only with an immutable script."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import PureWindowsPath
import re
import stat

from sqlalchemy import select

from app.db.models import AgentJobEvent
from app.services import employee_project_files, feature_files, workspace_files
from app.team import work_verification


EVENT = "steward_project_checks"
SOURCE = "steward_project_check"
SCRIPT = re.compile(r"cmd[.]exe /d /c ((?:verify|tests?|check)(?:[-_][a-z0-9_-]{1,40})?[.](?:cmd|bat))", re.ASCII)


@contextmanager
def _read_handle(path):
    """Windows refuses concurrent writes, renames and link substitution until exit."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        open_file = kernel.CreateFileW
        open_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        open_file.restype = wintypes.HANDLE
        # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, OPEN_REPARSE_POINT.
        handle = open_file(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(wintypes.HANDLE(handle))
            raise
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > feature_files.MAX_TEXT
                or not os.path.samestat(info, feature_files.checked(path).stat())):
            raise ValueError("验收脚本已变更、被链接或超过大小限制。")
        yield stream


@contextmanager
def _script(root, cwd, name):
    location = PureWindowsPath(cwd)
    if (location.anchor or workspace_files.is_secret(location.parts) or any(
            part.startswith(".") or part.casefold() in feature_files.PRIVATE or part == "任务" for part in location.parts)):
        raise ValueError("验收目录不在本岗位可用范围。")
    directory, relative = work_verification._project_path(cwd, root)
    path = feature_files.checked(directory / name)
    with employee_project_files.pinned_parent(root, path), _read_handle(path) as stream:
        yield relative, hashlib.file_digest(stream, "sha256").hexdigest()


def capture(job, root, contract):
    """Snapshot only the exact checks chosen in this authenticated direct assignment."""
    scopes = []
    for check in contract.get("commands", ()):
        match = SCRIPT.fullmatch(check["command"])
        if not match:
            continue
        name = match.group(1)
        try:
            with _script(root, check["cwd"], name) as (cwd, digest):
                scopes.append({"owner": job.owner_id, "employee": job.assignee_agent_id, "task": job.task_id,
                    "epoch": job.assignment_epoch, "job": job.id, "root": str(root), "cwd": cwd, "sha256": digest,
                    "argv": ["cmd.exe", "/d", "/c", name]})
        except (OSError, ValueError):
            continue
    return scopes


@contextmanager
def pinned(project, job_id, args, scope):
    if not isinstance(args, dict) or set(args) - {"argv", "cwd"} or not isinstance(scope, dict):
        raise ValueError("项目验收的参数不匹配。")
    argv = args.get("argv")
    if (not isinstance(argv, list) or len(argv) != 4 or not all(isinstance(a, str) for a in argv)
            or not SCRIPT.fullmatch(" ".join(argv)) or scope.get("argv") != argv
            or scope.get("cwd") != args.get("cwd", ".") or scope.get("owner") != project.owner_id
            or scope.get("employee") != project.employee_id or scope.get("job") != job_id
            or scope.get("task") != project.task_id or scope.get("epoch") != project.assignment_epoch
            or scope.get("root") != str(project.root)):
        raise ValueError("员工、工单、命令或目录已变更。")
    with _script(project.root, scope["cwd"], argv[-1]) as (cwd, digest):
        if scope.get("sha256") != digest or scope["cwd"] != cwd:
            raise ValueError("验收脚本已变更，需要重新核对。")
        yield


async def current(db, run, args):
    from app.services import employee_runtime, employee_terminal
    from app.team.steward_review import current_native_job, effective_job_mode
    if (not getattr(run.ctx, "project_runtime", None) or run.ctx.approval_mode != "accept_edits"
            or not employee_terminal.available(run.ctx, live=True)):
        return None
    project = await employee_runtime.checked_active_project(db, run)
    job = await current_native_job(db, run, write=True, lock=True)
    if not job or job.origin_kind != "person" or await effective_job_mode(db, job) != "accept_edits":
        return None
    event = await db.scalar(select(AgentJobEvent.detail).where(AgentJobEvent.job_id == job.id,
        AgentJobEvent.kind == EVENT).order_by(AgentJobEvent.id.desc()).limit(1))
    for scope in (event or {}).get("checks", ()):
        try:
            with pinned(project, job.id, args, scope):
                return scope
        except (OSError, ValueError):
            continue
    return None
