"""Confined reads of employee files and reviewed edits to the employee's own project."""
from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import tempfile

from app.services import feature_files, workspace_files


def _target(project, name):
    if not isinstance(name, str) or not name or len(name) > 400:
        raise ValueError("请指定岗位目录里的一个文件。")
    parts = PureWindowsPath(name).parts
    if not parts or workspace_files.is_secret(parts):
        raise ValueError("该文件不可用于岗位文本操作。")
    target = _inspection_path(project.root, name, own=True)
    if not target.is_file():
        raise ValueError("岗位文本操作只接受已有的普通文件。")
    return target


@contextmanager
def _directory(path):
    """Pin the parent against rename, junction substitution and symlink traversal."""
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if not os.path.samestat(os.fstat(descriptor), feature_files.checked(path).stat()):
                raise ValueError("文件目录已变更。")
            yield descriptor
        finally:
            os.close(descriptor)
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    # FILE_READ_ATTRIBUTES, share read/write (not delete), OPEN_EXISTING,
    # BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
    handle = create(str(path), 0x80, 3, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        final = ctypes.create_unicode_buffer(32768)
        size = kernel.GetFinalPathNameByHandleW(wintypes.HANDLE(handle), final, len(final), 0)
        if not 0 < size < len(final) or Path(final.value.removeprefix("\\\\?\\")) != feature_files.checked(path):
            raise ValueError("岗位文件目录已被重定向。")
        yield None
    finally:
        kernel.CloseHandle(wintypes.HANDLE(handle))


def pinned_parent(root, target):
    return pinned_directory(root, target.parent)


@contextmanager
def pinned_directory(root, directory):
    """Hold the trusted root and every directory down to the target during an operation."""
    with ExitStack() as stack:
        current = feature_files.checked(root)
        descriptor = stack.enter_context(_directory(current))
        for part in directory.relative_to(current).parts:
            current = feature_files.checked(current / part)
            descriptor = stack.enter_context(_directory(current))
        yield descriptor


def _contents(root, path):
    fd, info, final = workspace_files._open_confined(root, path)
    with os.fdopen(fd, "rb") as stream:
        if (info.st_nlink != 1 or final != path or info.st_size > feature_files.MAX_TEXT
                or not os.path.samestat(info, feature_files.checked(path).stat())):
            raise ValueError("岗位文件已变更、被链接或超过大小限制。")
        data = stream.read(feature_files.MAX_TEXT + 1)
        if len(data) > feature_files.MAX_TEXT or b"\x00" in data:
            raise ValueError("只支持 64 KiB 以内的 UTF-8 文本。")
        return data, info


def read(project, args):
    if not isinstance(args, dict) or set(args) != {"path"}:
        raise ValueError("读取操作只接受一个岗位内的相对文件名。")
    path = _target(project, args["path"])
    with pinned_parent(project.root, path):
        data, _ = _contents(project.root, path)
    return {"path":path.relative_to(project.root).as_posix(), "text":data.decode("utf-8-sig"),
            "sha256":hashlib.sha256(data).hexdigest()}


def _inspection_path(root, name, *, own):
    parts = PureWindowsPath(name).parts
    if own and parts and parts[0] == "任务":
        raise ValueError("员工的历史任务目录不可用于项目盘点。")
    return feature_files.checked_relative(root, name)


def inspect_workspace(root, name, *, listing, limit=workspace_files.LIST_MAX_ENTRIES, folders_only=False, own=False):
    if not isinstance(name, str) or len(name) > 400 or workspace_files.is_secret(PureWindowsPath(name).parts):
        raise ValueError("请使用岗位目录内的相对路径。")
    target = _inspection_path(root, name, own=own)
    relative = target.relative_to(root).as_posix()
    if not listing:
        with pinned_parent(root, target):
            data, _ = _contents(root, target)
        return {"path": relative, "text": data.decode("utf-8-sig"), "sha256": hashlib.sha256(data).hexdigest()}
    if type(limit) is not int or not 1 <= limit <= workspace_files.LIST_MAX_ENTRIES:
        raise ValueError(f"返回数量须为 1 到 {workspace_files.LIST_MAX_ENTRIES} 的整数。")
    if type(folders_only) is not bool:
        raise ValueError("folders_only 须为布尔值。")
    entries = []
    with pinned_directory(root, target):
        for child in sorted(target.iterdir()):
            if workspace_files.is_secret(child.relative_to(root).parts):
                continue
            try:
                _inspection_path(root, child.relative_to(root), own=own)
                folder = child.is_dir()
                if not folder and (folders_only or not child.is_file()):
                    continue
            except (OSError, ValueError):
                continue
            if len(entries) == limit:
                return {"path": relative, "entries": entries, "truncated": True}
            entries.append({"name": child.name, "folder": folder})
    return {"path": relative, "entries": entries, "truncated": False}


def edit(project, args, *, storage):
    if not project.writable or not isinstance(args, dict) or set(args) != {"path", "sha256", "old", "new"}:
        raise ValueError("修改须提供文件名、原文件摘要和一处明确的替换。")
    if (not isinstance(args["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", args["sha256"])
            or any(not isinstance(args[k], str) or "\x00" in args[k] or len(args[k]) > 4096 for k in ("old", "new"))
            or not args["old"] or args["old"] == args["new"]):
        raise ValueError("需要一处可以核对的文本替换。")
    files = feature_files.FeatureFiles(project.task_dir, writable=False)
    if files.role != project.root:
        raise ValueError("当前工单与岗位目录不匹配。")
    path = _target(project, args["path"])
    with pinned_parent(project.root, path) as parent_fd:
        data, info = _contents(project.root, path)
        data.decode("utf-8-sig")
        old, new = args["old"].encode(), args["new"].encode()
        if hashlib.sha256(data).hexdigest() != args["sha256"] or data.count(old) != 1:
            raise ValueError("原文件或匹配位置已变更，请重新读取并核对。")
        result = data.replace(old, new, 1)
        if len(result) > feature_files.MAX_TEXT:
            raise ValueError("修改后的文件超过 64 KiB。")
        # Prepare in private service storage. The project receives a single atomic
        # replacement, never a temporary project file or a partial source file.
        staging = feature_files.checked(storage)
        if staging.is_relative_to(project.root) or staging.stat().st_dev != info.st_dev:
            raise ValueError("安全修改需要与项目同盘的员工专用存储。")
        with tempfile.NamedTemporaryFile(dir=staging, delete=False) as temporary:
            staged = Path(temporary.name)
        try:
            with staged.open("wb") as stream:
                stream.write(result)
                stream.flush()
                os.fsync(stream.fileno())
            current, latest = _contents(project.root, path)
            if not os.path.samestat(latest, info) or current != data:
                raise ValueError("文件刚被其他程序修改，请重新读取。")
            if os.name == "nt":
                import ctypes
                # ReplaceFile preserves the original file's ACL and attributes.
                win = ctypes.WinDLL("kernel32", use_last_error=True)
                if not win.ReplaceFileW(str(path), str(staged), None, 0, None, None):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                staged.chmod(stat.S_IMODE(info.st_mode))
                os.replace(staged, path.name, dst_dir_fd=parent_fd)
        finally:
            staged.unlink(missing_ok=True)
    return {"path":path.relative_to(project.root).as_posix(),"sha256":hashlib.sha256(result).hexdigest()}
