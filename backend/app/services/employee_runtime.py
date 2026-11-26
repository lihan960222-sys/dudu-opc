"""Execution identity, project grants and private state for an opted-in employee."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.core import host_exec
from app.core.config import settings
from app.services import cli_probe
from app.services.role_workspace import checked_root, validate_configured_location, validate_location


class ProjectBusy(Exception):
    pass


class BoundaryUnavailable(ValueError):
    def __init__(self, reason: str, *, unsupported: bool = False):
        super().__init__(reason)
        self.unsupported = unsupported


def native_enabled(owner_id: str, employee_id: int) -> bool:
    entries = os.environ.get("SUPARAGENT_EMPLOYEE_NATIVE_IDS", "").split(",")
    return f"{owner_id}/{employee_id}" in {entry.strip() for entry in entries}


def _default_service_root() -> Path:
    return settings.SQLITE_FILE.resolve().parent / "employee-runtime"


def _service_root() -> Path:
    default = _default_service_root()
    configured = os.environ.get("SUPARAGENT_EMPLOYEE_RUNTIME_ROOT", "").strip()
    if not configured:
        return default
    path = Path(configured)
    if (not path.is_absolute() or path.drive.startswith("\\\\") or path.resolve() != path
            or (path.exists() and not path.is_dir())):
        raise BoundaryUnavailable("员工会话存储需要独立的本地绝对目录，不能使用链接。")
    if path == default:
        return default
    data = default.parent
    protected = [default, settings.SQLITE_FILE, Path(__file__).resolve().parents[3],
        *(data / name for name in ("employee-credentials", "employee-settings", "key.dpapi", "steward-deepseek.dpapi",
                                   "owner-oauth.sqlite3", "feishu.json", "deepseek-codex", "attachments", "decisions",
                                   "engine-processes", "auth-limits", "feishu-state")),
        *(Path(p) for p in (settings.ATTACHMENTS_DIR, settings.DECISION_LOG_DIR, settings.ENGINE_PROCESS_DIR,
                             settings.AUTH_LIMITS_DIR) if p)]
    validate_location(path, protected)
    return path


def _private_directory(path: Path) -> Path:
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("The employee service directory must not be redirected.")
    if not path.exists():
        _private_directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
    return checked_root(path)


def profile_dir(owner_id: str, employee_id: int, vendor: str) -> Path:
    if (not isinstance(owner_id, str) or not owner_id or type(employee_id) is not int or employee_id < 1
            or not isinstance(vendor, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", vendor)):
        raise ValueError("Invalid employee profile identity.")
    owner_key = hashlib.sha256(owner_id.encode()).hexdigest()
    relative = Path("profiles") / owner_key / str(employee_id) / vendor
    selected = _service_root()
    original = _default_service_root() / relative
    if selected != _default_service_root() and original.exists():
        return checked_root(original)
    return selected / relative


def profile_environment(owner_id: str, employee_id: int, vendor: str, source=None) -> dict[str, str]:
    return _profile_environment(profile_dir(owner_id, employee_id, vendor), source, ())


def check_service_executable(exe: Path, employee_folders=()) -> None:
    protected = (*employee_folders, _default_service_root(), _service_root())
    if (exe.suffix.casefold() != ".exe" or not exe.is_absolute() or exe.resolve() != exe or not exe.is_file()
            or any(exe.is_relative_to(Path(path)) for path in protected)):
        raise BoundaryUnavailable("请为员工选择项目目录以外的本机 Codex 可执行文件。")


def _profile_environment(home: Path, source, tool_directories) -> dict[str, str]:
    source = os.environ if source is None else source
    # Start from a positive list. In particular, never inherit provider keys, Codex
    # config, a personal keyring, shell startup scripts, or optional engine env names.
    allowed = {"PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC",
               "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
               "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "LANG", "LC_ALL",
               "NO_PROXY", "PYTHONUTF8", "PYTHONIOENCODING"}
    env = {key.upper(): value for key, value in source.items() if key.upper() in allowed}
    locations = list(tool_directories)
    if os.name == "nt" and env.get("SYSTEMROOT"):
        system = Path(env["SYSTEMROOT"]) / "System32"
        locations.extend((str(system), env["SYSTEMROOT"]))
        env["COMSPEC"] = str(system / "cmd.exe")
    env["PATH"] = os.pathsep.join(dict.fromkeys(locations)) if os.name == "nt" else os.defpath
    home = _private_directory(home)
    for name in ("CODEX_HOME", "CODEX_SQLITE_HOME", "HOME", "USERPROFILE"):
        env[name] = str(home)
    for name, folder in (("APPDATA", "roaming"), ("LOCALAPPDATA", "local"),
                         ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                         ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"),
                         ("TEMP", "tmp"), ("TMP", "tmp"), ("TMPDIR", "tmp")):
        env[name] = str(_private_directory(home / folder))
    proxy = os.environ.get("SUPARAGENT_CODEX_HTTPS_PROXY", "").strip()
    if proxy:
        from app.services.codex_adapter import validated_loopback_proxy
        env["HTTPS_PROXY"] = validated_loopback_proxy(proxy)
    return env


def _python_installation(base: Path, version: str | None = None):
    base = checked_root(base)
    validate_configured_location(base)
    directories = (os.environ.get(key) for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "SYSTEMROOT", "LOCALAPPDATA", "APPDATA"))
    blocked = {Path(value).resolve() for value in directories if value and Path(value).is_absolute()}
    if base in blocked or base.is_relative_to(_service_root()):
        raise BoundaryUnavailable("Python 安装路径没有独立的运行库目录。")
    if version:
        match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?(?:[\w+-]*)", version)
        if not match:
            raise BoundaryUnavailable("无法辨认项目 Python 的版本。")
        digits = "".join(match.groups())
        dlls = [base / f"python{digits}{suffix}.dll" for suffix in ("", "t")]
    else:
        dlls = list(base.glob("python3[0-9]*.dll"))
    libraries = [base / "Lib" / "encodings" / "__init__.py", *(dll.with_suffix(".zip") for dll in dlls)]
    if not ((base / "python.exe").is_file() and any(p.is_file() for p in dlls) and any(p.is_file() for p in libraries)):
        raise BoundaryUnavailable("Python 的解释器、对应版本的 DLL 或标准库不完整，未启动员工终端。")
    return base


def _installed_tools(project: Path, reserved_roots) -> tuple[tuple[str, Path, Path], ...]:
    if os.name != "nt":
        return ()
    # Never expose the service virtualenv (it may contain application configuration).
    # The standalone base installation supplies python.exe, the exact Python DLL and stdlib.
    base = _python_installation(Path(sys.base_prefix).resolve(), f"{sys.version_info.major}.{sys.version_info.minor}")
    tools = [("python", base / "python.exe", base)]
    protected = tuple(Path(value).resolve() for key in ("PROGRAMFILES", "PROGRAMFILES(X86)")
                      if (value := os.environ.get(key)))
    for name in ("pwsh", "git", "python"):
        located = shutil.which(name + ".exe")
        if not located:
            continue
        exe = Path(located).resolve()
        install = exe.parent.parent if name == "git" and exe.parent.name.lower() == "cmd" else exe.parent
        if not any(install.is_relative_to(folder) and install != folder for folder in protected):
            continue
        if name == "git" and not (install / "cmd" / "git.exe").is_file():
            continue
        if name == "pwsh" and not (install / "System.Management.Automation.dll").is_file():
            continue
        if name == "python":
            _python_installation(install)
        if (name, exe, install) not in tools:
            tools.append((name, exe, checked_root(install)))
    for _, _, root in tools:
        if any(root.is_relative_to(p) or p.is_relative_to(root) for p in (*reserved_roots, project, _service_root())):
            raise BoundaryUnavailable("工具安装目录与员工目录或后台私有目录重叠。")
    # A project-controlled pyvenv.cfg is a dependency declaration, never a grant.
    for name in (".venv", "venv"):
        cfg = project / name / "pyvenv.cfg"
        if not cfg.exists():
            continue
        if cfg.resolve(strict=True) != cfg or cfg.stat().st_nlink != 1 or cfg.stat().st_size > 16384:
            raise BoundaryUnavailable("项目虚拟环境的配置不是可核实的普通文件。")
        values = {}
        for line in cfg.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key.strip()] = value.strip()
        home = Path(values.get("home", ""))
        if not home.is_absolute() or home.resolve() not in {r for n, _, r in tools if n == "python"}:
            raise BoundaryUnavailable("项目虚拟环境引用了尚未获准的 Python 基础安装。")
        _python_installation(home.resolve(), values.get("version") or values.get("version_info"))
        if not (cfg.parent / "Scripts" / "python.exe").is_file():
            raise BoundaryUnavailable("项目虚拟环境缺少 Scripts/python.exe。")
    return tuple(tools)


@dataclass(frozen=True)
class ProjectRuntime:
    owner_id: str
    employee_id: int
    task_id: int
    assignment_epoch: int
    model_vendor: str
    root: Path
    writable: bool
    readable_roots: tuple[Path, ...]
    installed_tools: tuple[tuple[str, Path, Path], ...] = ()
    employee_folders: tuple[Path, ...] = ()
    profile_path: Path | None = None

    @property
    def profile(self) -> Path:
        return self.profile_path or profile_dir(self.owner_id, self.employee_id, self.model_vendor)

    @property
    def stamp(self) -> dict:
        home = self.profile
        storage = home.parents[3]
        return {"owner": home.parent.parent.name, "employee": self.employee_id,
                "task": self.task_id, "epoch": self.assignment_epoch, "vendor": self.model_vendor,
                "root": str(self.root), **({"storage": str(storage)} if storage != _default_service_root() else {})}

    def check_previous_storage(self, stamp) -> None:
        if not isinstance(stamp, dict):
            return
        current = self.stamp
        if (all(stamp.get(key) == current[key] for key in ("owner", "employee", "vendor"))
                and stamp.get("storage") != current.get("storage")):
            raise BoundaryUnavailable("此员工的会话保存在另一服务目录。请恢复原存储设置后续办，或为新任务建立独立档案。")

    @property
    def task_dir(self) -> Path:
        return self.root / "任务" / str(self.task_id)

    @property
    def permission_name(self) -> str:
        # A changed grant cannot reuse a stale process configuration.
        payload = json.dumps([str(self.root), self.writable, *map(str, self.readable_roots),
                              *(str(path) for _, _, path in self.installed_tools)])
        return "employee-" + hashlib.sha256(payload.encode()).hexdigest()[:24]

    def process_environment(self, source=None) -> dict[str, str]:
        directories = (str(executable.parent) for _, executable, _ in self.installed_tools)
        return _profile_environment(self.profile, source, directories)

    def _binding_paths(self, backend_thread_id: int) -> tuple[Path, Path]:
        if type(backend_thread_id) is not int or backend_thread_id < 1:
            raise ValueError("Invalid service thread ID.")
        folder = _private_directory(self.profile / "service-bindings")
        return folder / f"{backend_thread_id}.starting", folder / f"{backend_thread_id}.native"

    def bound_native_id(self, backend_thread_id: int) -> str | None:
        _, receipt = self._binding_paths(backend_thread_id)
        if not receipt.exists():
            return None
        if not receipt.is_file() or receipt.resolve() != receipt or receipt.stat().st_nlink != 1 or receipt.stat().st_size > 8192:
            raise BoundaryUnavailable("员工会话的后台绑定文件无效。")
        data = json.loads(receipt.read_text(encoding="utf-8"))
        native = data.get("native") if isinstance(data, dict) else None
        if isinstance(native, str) and data.get("identity") == self.stamp and str(UUID(native)) == native:
            return native
        raise BoundaryUnavailable("员工会话的后台绑定身份已变更。")

    def reserve_native_start(self, backend_thread_id: int) -> str | None:
        """An interrupted creation with no receipt must never silently create a second thread."""
        if native := self.bound_native_id(backend_thread_id):
            return native
        starting, _ = self._binding_paths(backend_thread_id)
        try:
            with starting.open("xb") as stream:
                stream.write(b"starting")
                stream.flush()
                os.fsync(stream.fileno())
            return None
        except FileExistsError:
            if native := self.bound_native_id(backend_thread_id):
                return native
            raise BoundaryUnavailable("此前已请求建立员工会话，但后台尚未取得可核实的编号。请核对原绑定。")

    def record_native_start(self, backend_thread_id: int, native: str) -> None:
        if not isinstance(native, str) or str(UUID(native)) != native:
            raise ValueError("Invalid native thread ID.")
        previous = self.bound_native_id(backend_thread_id)
        if previous is not None:
            if previous != native:
                raise BoundaryUnavailable("后台已保存另一原生会话编号。")
            return
        _, receipt = self._binding_paths(backend_thread_id)
        with tempfile.NamedTemporaryFile(dir=receipt.parent, delete=False) as stream:
            pending = Path(stream.name)
            try:
                stream.write(json.dumps({"native":native, "identity":self.stamp}).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.close()
                pending.unlink(missing_ok=True)
                raise
        try:
            os.replace(pending, receipt)
        finally:
            pending.unlink(missing_ok=True)

    def cli_options(self) -> list[str]:
        # Named profiles express restricted reads on Windows. Legacy workspace-write
        # grants broad reads and is never substituted for these options.
        fs = {":root": "deny", ":minimal": "read", **{str(p): "read" for _, _, p in self.installed_tools},
              str(self.root): "write" if self.writable else "read",
              **{str(path): "read" for path in self.readable_roots}, str(_default_service_root()): "deny",
              str(self.profile.parents[3]): "deny", str(_service_root()): "deny"}
        fields = ", ".join(f"{json.dumps(k, ensure_ascii=False)}={json.dumps(v)}" for k, v in fs.items())
        config = {
            "windows.sandbox": '"mxc"',
            "default_permissions": json.dumps(self.permission_name),
            f"permissions.{self.permission_name}.filesystem": "{" + fields + "}",
            f"permissions.{self.permission_name}.network.enabled": "false",
            "cli_auth_credentials_store": '"file"',
            "projects": "{" + json.dumps(str(self.root), ensure_ascii=False) + '={trust_level="untrusted"}}',
            "project_root_markers": "[]",
            "features.hooks": "false", "features.multi_agent": "false", "features.multi_agent_v2": "false",
            "features.plugins": "false", "features.apps": "false", "features.code_mode": "false",
            "shell_environment_policy.inherit": '"core"', "allow_login_shell": "false",
            "analytics.enabled": "false", "check_for_update_on_startup": "false",
        }
        return [part for key, value in config.items() for part in ("-c", f"{key}={value}")]

    def sandbox_command(self, exe: Path, argv: list[str]) -> list[str]:
        self.require_quiet_host()
        return [str(exe), *self.cli_options(), "sandbox", "--include-managed-config",
                "--permission-profile", self.permission_name, "--", *argv]

    @staticmethod
    def require_quiet_host():
        if sys.platform != "win32":
            raise BoundaryUnavailable("员工原生终端目前需要 Windows MXC。", unsupported=True)
        # MXC starts binaries via a separate broker, which may reset the parent's
        # loader-error mode. CREATE_NO_WINDOW by itself does not hide loader dialogs.
        # A background scheduled task may meet this condition in a logged-in home.
        if reason := host_exec.employee_host_blocker(acceptance=True):
            raise BoundaryUnavailable(reason, unsupported=True)
        if os.environ.get("SUPARAGENT_RUN_WINDOWS_MXC_ACCEPTANCE") != "1":
            raise BoundaryUnavailable("本次未明确启用 Windows 隔离对照测试。", unsupported=True)

    def check_executable(self, exe: Path) -> None:
        check_service_executable(exe, (self.root, *self.readable_roots, *self.employee_folders))

    def check_start(self, exe: Path) -> None:
        if sys.platform != "win32":
            raise BoundaryUnavailable("员工原生终端目前需要 Windows MXC。", unsupported=True)
        if reason := host_exec.employee_host_blocker():
            raise BoundaryUnavailable(reason, unsupported=True)
        self.check_executable(exe)

    async def check_host_boundary(self, exe: Path, env: dict) -> None:
        """Explicit acceptance only. Ordinary employee turns use check_start and native RPC."""
        self.require_quiet_host()
        self.check_executable(exe)
        # A CLI claiming it supports the profile is insufficient. Run an actual child
        # with the exact same profile and require a positive cwd check and denied read.
        # The canary contains only test data and is removed even if the CLI dies.
        with tempfile.NamedTemporaryFile(dir=self.profile, delete=False) as probe:
            probe.write(b"suparagent-boundary-check")
            marker = Path(probe.name)
        try:
            system = Path(os.environ["SYSTEMROOT"]) / "System32"
            cwd_probe = [str(system / "cmd.exe"), "/d", "/c", "chcp 65001>nul & type AGENTS.md >nul && cd"]
            denied_probe = [str(system / "findstr.exe"), "/m", "/l", "suparagent-boundary-check", str(marker)]
            read_probe = [*denied_probe[:-1], str(self.root / "AGENTS.md")]
            cwd_run, read_run, private_run = await asyncio.gather(*(
                cli_probe.run(self.sandbox_command(exe, argv), cwd=str(self.root), env=env, timeout=30,
                              employee_project=True, acceptance=True)
                for argv in (cwd_probe, read_probe, denied_probe)))
        finally:
            marker.unlink(missing_ok=True)
        if (cwd_run.ok and Path(cwd_run.stdout.strip()) == self.root
                and read_run.exit_code in (0, 1) and not read_run.stderr.strip()
                and private_run.exit_code in (1, 2) and not private_run.stdout.strip() and private_run.stderr.strip()):
            for name, path, _ in self.installed_tools:
                args = (["-I", "-S", "-c", "import sys;print(sys.version_info.major)"] if name == "python"
                        else ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "exit 0"] if name == "pwsh"
                        else ["--version"])
                check = await cli_probe.run(self.sandbox_command(exe, [str(path), *args]),
                                            cwd=str(self.root), env=env, timeout=15, employee_project=True, acceptance=True)
                if not check.ok:
                    raise BoundaryUnavailable(f"员工目录权限下无法启动 {name}（退出码 {check.exit_code}）。"
                                              "请检查该工具的本机安装及运行库。")
            return
        detail = (cwd_run.stderr + private_run.stderr).lower()
        unsupported = ("mxc" in detail and any(word in detail for word in
                       ("unavailable", "not supported", "unsupported", "not available", "not installed")))
        raise BoundaryUnavailable(
            f"Windows MXC 未通过员工目录隔离检查（目录 {cwd_run.exit_code}，私有文件 {private_run.exit_code}），未启动项目会话。",
            unsupported=unsupported,
        )

    @contextmanager
    def writer_lock(self):
        if not self.writable:
            yield
            return
        folder = _private_directory(_default_service_root() / "locks")
        key = hashlib.sha256(os.path.normcase(str(self.root)).encode()).hexdigest()
        with (folder / key).open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                if stream.tell() == 0:
                    stream.write(b"x")
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise ProjectBusy from exc
            else:
                import fcntl
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ProjectBusy from exc
            yield

    @asynccontextmanager
    async def turn_lock(self, ctx):
        # Do not hold a database write transaction while another project task runs.
        while not ctx.aborted:
            lock = self.writer_lock()
            try:
                lock.__enter__()
                break
            except ProjectBusy:
                await asyncio.sleep(0.2)
        else:
            raise BoundaryUnavailable("派工已停止。")
        try:
            yield
        finally:
            lock.__exit__(None, None, None)


def resolve_project_runtime(agent, task, *, approval_mode, readable_roots=(),
                            model_vendor: str, assignment_epoch: int, protected_roots=()) -> ProjectRuntime:
    if ((task.owner_id, task.agent_id) != (agent.owner_id, agent.id) or not agent.role_path
            or not native_enabled(agent.owner_id, agent.id)):
        raise ValueError("此员工未获准以原生项目会话执行这项派工。")
    root = checked_root(Path(agent.role_path))
    peers = tuple(sorted({checked_root(Path(p)) for p in readable_roots})) if agent.team_workspaces_readable else ()
    storage = _service_root()
    if any(storage.is_relative_to(p) or p.is_relative_to(storage) for p in protected_roots):
        raise BoundaryUnavailable("员工的会话存储不能与任何员工的项目目录重叠。")
    for path in (root, *peers):
        validate_configured_location(path)
        if any(path.is_relative_to(p) or p.is_relative_to(path) for p in (_default_service_root(), _service_root())):
            raise ValueError("员工项目目录与后台私有资料重叠。")
    if any(root.is_relative_to(peer) or peer.is_relative_to(root) for peer in peers):
        raise ValueError("员工项目目录相互重叠。")
    writable_modes = {"ask", "accept_edits", "approve_for_me", "full_access"}
    writable = approval_mode in writable_modes and agent.approval_mode in writable_modes
    profile = profile_dir(agent.owner_id, agent.id, model_vendor)
    return ProjectRuntime(agent.owner_id, agent.id, task.id, assignment_epoch,
                          model_vendor, root, writable, peers,
                          _installed_tools(root, protected_roots), tuple(protected_roots), profile)
