"""One switch that stops this server from starting engine, tool or helper processes on its own host.

A hosted deployment signs many people in. Their agents must run in the user's own sandbox, never as
a process of this server, so the server sets HOST_EXECUTION_DISABLED and every place that would
start a process asks here first. The error is a PermissionError, so callers that already treat
"could not start" (an unreadable or missing program) as a normal failure handle it the same way.

This module must stay import-light: the computer tool server (computers/mcp_server.py) is a child
process with a minimal environment and imports docker_cli, which imports this. Loading the app
settings there would fail, so the settings are read only when the server already loaded them, and
the environment variable answers otherwise.
"""

import os
import sys
from functools import lru_cache


class HostExecutionDisabled(PermissionError):
    pass


def disabled() -> bool:
    config = sys.modules.get("app.core.config")
    if config is not None:
        return bool(config.settings.HOST_EXECUTION_DISABLED)
    return os.environ.get("HOST_EXECUTION_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def ensure_allowed(what: str) -> None:
    if disabled():
        raise HostExecutionDisabled(f"This server does not run {what} on its own machine.")


@lru_cache(maxsize=1)
def _suppress_windows_error_dialogs() -> None:
    """Set only this backend process's inherited loader and error-reporting flags."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    mode = 0x0001 | 0x0002 | 0x8000  # No critical-error, general-fault, or open-file dialog.
    kernel.GetErrorMode.restype = wintypes.UINT
    kernel.SetErrorMode.argtypes = [wintypes.UINT]
    kernel.SetErrorMode(kernel.GetErrorMode() | mode)
    if kernel.GetErrorMode() & mode != mode:
        raise OSError("Windows did not enable silent background error handling.")
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.WerGetFlags.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.WerGetFlags.restype = ctypes.c_long
    kernel.WerSetFlags.argtypes = [wintypes.DWORD]
    kernel.WerSetFlags.restype = ctypes.c_long
    flags = wintypes.DWORD()
    if (kernel.WerGetFlags(kernel.GetCurrentProcess(), ctypes.byref(flags)) < 0
            or kernel.WerSetFlags((flags.value & ~0x10) | 0x20) < 0):  # ALWAYS_SHOW_UI / NO_UI
        raise OSError("Windows did not enable silent error reporting for this backend.")


def background_process_options() -> dict:
    if os.name != "nt":
        return {}
    import subprocess
    _suppress_windows_error_dialogs()
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}


def noninteractive_window_station() -> bool:
    """A private window station can be used by a scheduled task under the same account."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class UserObjectFlags(ctypes.Structure):
        _fields_ = [("inherit", wintypes.BOOL), ("reserved", wintypes.BOOL), ("flags", wintypes.DWORD)]

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetProcessWindowStation.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    user.GetUserObjectInformationW.restype = wintypes.BOOL
    station = user.GetProcessWindowStation()
    info = UserObjectFlags()
    returned = wintypes.DWORD()
    read = station and user.GetUserObjectInformationW(
        station, 1, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned))  # UOI_FLAGS
    return bool(read and returned.value >= ctypes.sizeof(info) and not (info.flags & 1))  # WSF_VISIBLE


def windows_worker_token_status() -> str:
    """Check the process token inherited by children and any effective thread token."""
    if os.name != "nt":
        return "unknown"
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = kernel.GetCurrentThread.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle_pointer = ctypes.POINTER(wintypes.HANDLE)
    dword_pointer = ctypes.POINTER(wintypes.DWORD)
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, handle_pointer]
    security.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, handle_pointer]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, dword_pointer]
    security.CreateWellKnownSid.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, dword_pointer]
    security.DuplicateToken.argtypes = [wintypes.HANDLE, ctypes.c_int, handle_pointer]
    security.CheckTokenMembership.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]

    sid = ctypes.create_string_buffer(68)  # SECURITY_MAX_SID_SIZE
    size = wintypes.DWORD(ctypes.sizeof(sid))
    if not security.CreateWellKnownSid(26, None, sid, ctypes.byref(size)):  # WinBuiltinAdministratorsSid
        return "unknown"
    process, thread = wintypes.HANDLE(), wintypes.HANDLE()
    access = 0x0008 | 0x0002  # TOKEN_QUERY | TOKEN_DUPLICATE
    try:
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), access, ctypes.byref(process)):
            return "unknown"
        if not security.OpenThreadToken(kernel.GetCurrentThread(), access, True, ctypes.byref(thread)):
            if ctypes.get_last_error() != 1008:  # ERROR_NO_TOKEN means the process token is effective.
                return "unknown"
        for token in (process, thread):
            if not token:
                continue
            elevated, read = wintypes.DWORD(), wintypes.DWORD()
            if not security.GetTokenInformation(token, 20, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(read)):
                return "unknown"
            if read.value != ctypes.sizeof(elevated):
                return "unknown"
            if elevated.value:
                return "elevated"
            duplicate, member = wintypes.HANDLE(), wintypes.BOOL()
            if not security.DuplicateToken(token, 1, ctypes.byref(duplicate)):  # SecurityIdentification
                return "unknown"
            try:
                if not security.CheckTokenMembership(duplicate, sid, ctypes.byref(member)):
                    return "unknown"
            finally:
                kernel.CloseHandle(duplicate)
            if member.value:
                return "administrator"
        return "standard"
    finally:
        if thread:
            kernel.CloseHandle(thread)
        if process:
            kernel.CloseHandle(process)


def employee_host_blocker(*, acceptance: bool = False) -> str | None:
    try:
        if acceptance and not noninteractive_window_station():
            return "Windows 隔离对照测试需要非可见窗口站。当前桌面可见，或系统未能确认。"
        status = windows_worker_token_status()
    except (OSError, AttributeError):
        status = "unknown"
    if status == "standard":
        return None
    if status == "elevated":
        return "员工后台进程的 Windows 令牌已提升，请使用普通权限重新启动后台。"
    if status == "administrator":
        return "员工后台进程的 Windows 令牌已启用管理员组，请使用普通权限重新启动后台。"
    return "无法核实员工后台进程的 Windows 权限，未启动员工终端。"


def employee_process_lifetime(pid: int):
    """The private App Server and its descendants die when the backend loses its handle."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
                    ("memory_limits", ctypes.c_size_t * 4)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    proc = kernel.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x0400  # KILL_ON_JOB_CLOSE | DIE_ON_UNHANDLED_EXCEPTION
        if not (proc and kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                and kernel.AssignProcessToJobObject(job, proc)):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(job)
        raise
    finally:
        if proc:
            kernel.CloseHandle(proc)
    return lambda: kernel.CloseHandle(job)
