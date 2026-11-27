"""Confined reads of employee files and reviewed edits to the employee's own project."""
from contextlib import ExitStack, contextmanager
import fnmatch
import hashlib
import math
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import tempfile
import time

from app.services import feature_files, workspace_files


def _own_path(project, name):
    parts = PureWindowsPath(name).parts
    if not parts or workspace_files.is_secret(parts):
        raise ValueError("请指定岗位内的普通文件。")
    if parts[0] == "任务":
        if (not getattr(project, "local_workstation", False) or len(parts) < 3
                or parts[1] != project.task_dir.name):
            raise ValueError("只能访问当前工单的文件。")
    return feature_files.checked_relative(project.root, name)


def _target(project, name):
    if not isinstance(name, str) or not name or len(name) > 400:
        raise ValueError("请指定岗位目录里的一个文件。")
    parts = PureWindowsPath(name).parts
    if not parts or workspace_files.is_secret(parts):
        raise ValueError("该文件不可用于岗位文本操作。")
    target = _own_path(project, name)
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


FIND_MAX_ENTRIES = 4096
FIND_MAX_SECONDS = 4


def _private_search_path(path):
    return (workspace_files.is_secret(path.parts) or any(part.casefold() in {
        ".codex", ".agents", ".config", ".git", "credentials", "secrets", "auth.json", "login data", "cookies",
        "keychains", "credential manager", "user data", "passwords", "vault", "protect", "systemcertificates",
    } for part in path.parts))


def _find_local(pin_root, target, args, visible):
    pattern = args.get("pattern", "*")
    limit = args.get("limit", 20)
    depth = args.get("max_depth", 6)
    since = args.get("modified_since", 0)
    if (not isinstance(pattern, str) or not 1 <= len(pattern) <= 128
            or any(c in "/\\:" or ord(c) < 32 for c in pattern)
            or type(limit) is not int or not 1 <= limit <= 100 or type(depth) is not int or not 0 <= depth <= 8
            or type(since) not in (int, float) or not 0 <= since <= 253402300799
            or not math.isfinite(since) or _private_search_path(target)):
        raise ValueError("请使用文件名通配符、0 至 8 层目录和 1 至 100 条结果。修改时间采用 Unix 秒数。")
    matched = []
    visited = skipped = 0
    stopped = False
    depth_limited = False
    deadline = time.monotonic() + FIND_MAX_SECONDS
    def walk(directory, remaining):
        nonlocal visited, skipped, stopped, depth_limited
        # All ancestor handles are held by the caller. lstat, then verify the
        # same directory identity under a new pinned handle before descending.
        with os.scandir(directory) as children:
            for entry in children:
                if visited >= FIND_MAX_ENTRIES or time.monotonic() >= deadline:
                    stopped = True
                    return
                visited += 1
                path = directory / entry.name
                if not visible(path) or _private_search_path(path):
                    continue
                try:
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if remaining:
                            with _directory(path):
                                if not os.path.samestat(info, feature_files.checked(path).stat()):
                                    skipped += 1
                                    continue
                                walk(path, remaining - 1)
                            if stopped:
                                return
                        else:
                            depth_limited = True
                    elif (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_mtime >= since
                          and fnmatch.fnmatchcase(entry.name.casefold(), pattern.casefold())):
                        matched.append({"path":str(path), "bytes":info.st_size, "modified":info.st_mtime})
                except (OSError, ValueError):
                    skipped += 1

    with pinned_directory(pin_root, target):
        walk(target, depth)
    matched.sort(key=lambda row: (-row["modified"], row["path"].casefold()))
    partial = stopped or depth_limited or bool(skipped)
    return {"path":str(target), "entries":matched[:limit], "matched":len(matched), "visited":visited,
            "truncated":len(matched) > limit or partial, "scan_complete":not partial, "skipped":skipped}


def inspect_local(project, args):
    """Read an ordinary local file through the service without granting a child process its parent tree."""
    from app.services.agent_memory import redact_secrets
    if (not project.local_workstation or not isinstance(args, dict) or not {"path", "operation"} <= args.keys()
            or args.keys() - ({"path", "operation", "pattern", "limit", "max_depth", "modified_since"}
                              if args["operation"] == "find" else {"path", "operation"})
            or not isinstance(args["path"], str) or not 0 < len(args["path"]) <= 1024
            or args["operation"] not in ("metadata", "list", "read", "find")):
        raise ValueError("请指定本地绝对路径，并选择查看属性、列目录、查找文件或读文本。")
    target = Path(args["path"])
    anchor = Path(target.anchor)
    if (not target.is_absolute() or target.drive.startswith("\\\\") or any(
            part in (".", "..") or part.endswith((" ", ".")) or feature_files.ntpath.isreserved(part)
            or any(ord(ch) < 32 or ch in ':*?"<>|' for ch in part) for part in target.parts[1:])):
        raise ValueError("只支持本地磁盘上的普通路径。")
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(anchor)) not in (2, 3, 5, 6):
            raise ValueError("只支持本地磁盘。")
    barriers = (*project.private_read_roots(), *(p for p in project.employee_folders
                 if p != project.root and p not in project.readable_roots))
    application_data = project.application_data_roots()
    peer_controls = project.peer_control_paths()

    def visible(path):
        if any(path.is_relative_to(p) for p in peer_controls):
            return False
        # A validated registered project can live under the service data directory.
        if any(path.is_relative_to(root) for root in (project.root, *project.readable_roots)):
            return True
        return not any(path.is_relative_to(root) for root in barriers)

    if not visible(target):
        raise ValueError("该目录包含后台私有数据。")
    # Pin from an explicitly registered directory where possible. Ancestors
    # such as C:\\Users may refuse even attribute handles to a standard user.
    candidates = [project.root, *project.readable_roots, *application_data, Path.home()]
    pin_root = max((p for p in candidates if target.is_relative_to(p)), key=lambda p:len(p.parts), default=None)
    if pin_root is None:
        pin_root = anchor / target.parts[1] if os.name == "nt" and len(target.parts) > 1 else anchor
    operation = args["operation"]
    if operation == "find":
        return _find_local(pin_root, target, args, visible)
    if operation == "list":
        names = []
        with pinned_directory(pin_root, target):
            for child in target.iterdir():
                if not visible(child):
                    continue
                try:
                    feature_files.checked(child)
                    names.append({"name": child.name, "folder": child.is_dir()})
                except (OSError, ValueError):
                    continue
                if len(names) > 100:
                    break
        return {"path": str(target), "entries": names[:100], "truncated": len(names) > 100}
    with pinned_parent(pin_root, target) if target != pin_root else pinned_directory(pin_root, target):
        info = feature_files.checked(target).stat()
        metadata = {"path": str(target), "bytes": info.st_size, "modified": info.st_mtime,
                    "folder": stat.S_ISDIR(info.st_mode)}
        if operation == "metadata":
            return metadata
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("只能读取普通文本文件。")
        # AppData mixes application logs with browser profiles, credential vaults
        # and sessions. Read logging files only; directory discovery stays available.
        relative = next((target.relative_to(p) for p in application_data if target.is_relative_to(p)), None)
        if relative is not None:
            logs = any(part.casefold() in ("log","logs","logging","日志") for part in relative.parts[:-1])
            if not (target.suffix.lower() == ".log" or (logs and target.suffix.lower() in (".txt",".json",".jsonl"))):
                return {**metadata, "contents_hidden": True}
        if (workspace_files.is_secret(target.parts) or any(p.casefold() in
                ("credentials", "secrets", "auth.json", "login data", "cookies") for p in target.parts)):
            return {**metadata, "contents_hidden": True}
        fd, opened, final = workspace_files._open_confined(anchor, target)
        with os.fdopen(fd, "rb") as stream:
            if os.name == "nt":
                import msvcrt
                from ctypes import wintypes
                resolved = ctypes.create_unicode_buffer(32768)
                size = ctypes.windll.kernel32.GetFinalPathNameByHandleW(wintypes.HANDLE(msvcrt.get_osfhandle(fd)),
                                                                         resolved, len(resolved), 0)
                if not 0 < size < len(resolved) or Path(resolved.value.removeprefix("\\\\?\\")) != target:
                    raise ValueError("文件指向了其他位置。")
            if final != target or opened.st_nlink != 1 or not os.path.samestat(info, opened):
                raise ValueError("文件已被替换。")
            oversized = opened.st_size > feature_files.MAX_TEXT
            if oversized:
                stream.seek(-feature_files.MAX_TEXT, os.SEEK_END)
                stream.readline(feature_files.MAX_TEXT)
            data = stream.read(feature_files.MAX_TEXT)
        if b"\x00" in data:
            raise ValueError("请选择 UTF-8 文本。")
        text, count = redact_secrets(data.decode("utf-8-sig"))
        return {**metadata, "text":text, "redacted": bool(count), "truncated":oversized}


def create(project, args):
    """Publish a complete UTF-8 file without overwriting any existing directory entry."""
    if (not getattr(project, "local_workstation", False) or not project.writable or not isinstance(args, dict)
            or set(args) != {"path", "text"} or not isinstance(args["path"], str) or not 0 < len(args["path"]) <= 400
            or not isinstance(args["text"], str) or "\x00" in args["text"]):
        raise ValueError("请提供新文件的相对路径和 UTF-8 文本。")
    data = args["text"].encode("utf-8")
    if len(data) > feature_files.MAX_TEXT:
        raise ValueError("新文件超过 64 KiB。")
    path = _own_path(project, args["path"])
    # The runtime is Windows-only. Pin each parent against deletion or junction
    # substitution while creating nested directories and publishing the final name.
    with ExitStack() as stack:
        current = project.root
        stack.enter_context(_directory(current))
        for part in path.parent.relative_to(current).parts:
            current = feature_files.checked(current / part)
            current.mkdir(exist_ok=True)
            stack.enter_context(_directory(current))
        with tempfile.NamedTemporaryFile(dir=current, delete=False) as stream:
            staged = Path(stream.name)
            try:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.close()
                staged.unlink(missing_ok=True)
                raise
        try:
            os.link(staged, path)
        finally:
            staged.unlink(missing_ok=True)
    return {"path":path.relative_to(project.root).as_posix(),"sha256":hashlib.sha256(data).hexdigest()}


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
