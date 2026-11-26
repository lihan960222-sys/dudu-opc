"""Private, bounded device-code login. CLI output is never logged or returned."""

import asyncio
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from app.core import data_lease, engine_processes
from app.services import cli_probe
from app.services.local_claude_runner import child_env
from app.core import host_exec

DEVICE_URL = "https://auth.openai.com/codex/device"
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CODE = re.compile(r"(?<![A-Z0-9-])([A-Z0-9]{4,8}-[A-Z0-9]{4,8})(?![A-Z0-9-])")


class LoginError(ValueError):
    pass


def employee_credentials_exist(home: Path) -> bool:
    """The CLI may only open regular, unshared files at the service-selected location."""
    if not home.is_absolute() or home.resolve() != home:
        raise LoginError("无法核验员工的独立登录目录。")
    credential = False
    for name in ("auth.json", "config.toml", ".suparagent-login.lock"):
        target = home / name
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 1024):
            raise LoginError("员工登录目录中存在链接或无效文件。")
        if name == "auth.json":
            credential = True
    return credential


def _command(executable: Path, employee: bool) -> list[str]:
    return [str(executable), *(["-c", 'cli_auth_credentials_store="file"'] if employee else [])]


async def account_signed_in(executable: Path, env: dict, *, employee: bool = False) -> bool:
    if employee:
        employee_credentials_exist(Path(env["CODEX_HOME"]))
    private = {"cwd": env["CODEX_HOME"], "employee_project": True} if employee else {}
    status = await cli_probe.run([*_command(executable, employee), "login", "status"],
                                 timeout=8, env=env, **private)
    said = status.stdout + "\n" + status.stderr
    if status.ok and re.search(r"^\s*logged in\b", said, re.I | re.M):
        if employee and (not re.search(r"^\s*logged in using chatgpt\b", said, re.I | re.M)
                         or not employee_credentials_exist(Path(env["CODEX_HOME"]))):
            raise LoginError("Codex 未确认这名员工的独立 ChatGPT 登录。")
        return True
    if status.failed_to_start or status.timed_out or not re.search(r"^\s*not logged in\b", said, re.I | re.M):
        raise LoginError("Codex could not confirm its account status. Check its installation on the server.")
    return False


class CodexLogin:
    def __init__(self):
        self.flow = None
        self.task = None
        self.lifetime = 300
        self.prompt_timeout = 30

    async def start(self, owner: str, executable: Path, *, employee_env: dict[str, str] | None = None):
        if self.task and not self.task.done():
            raise LoginError("A Codex sign-in is already running. Finish or cancel it first.")
        employee = employee_env is not None
        env = dict(employee_env) if employee else child_env()
        if employee:
            configured_home = Path(env["CODEX_HOME"])
            employee_credentials_exist(configured_home)
        else:
            configured_home = Path(env.get("CODEX_HOME") or str(Path.home() / ".codex"))
        if not configured_home.is_absolute():
            raise LoginError("Use an absolute CODEX_HOME path before signing in.")
        home = configured_home.resolve()
        home.mkdir(parents=True, exist_ok=True)
        fd = os.open(home / ".suparagent-login.lock", os.O_RDWR | os.O_CREAT, 0o600)
        if not data_lease._try_lock(fd):
            os.close(fd)
            raise LoginError("A Codex sign-in is already running on this server.")
        self.flow = {"id": uuid4().hex, "owner": owner, "phase": "running"}
        flow = self.flow
        self.task = asyncio.create_task(self._run(flow, executable.resolve(), env, employee=employee))
        def release(task):
            if task.cancelled():
                flow["phase"] = "cancelled"
            flow.pop("user_code", None)
            flow.pop("authorization_url", None)
            data_lease._unlock(fd)
            os.close(fd)
        self.task.add_done_callback(release)
        return self.view(owner, flow["id"])

    def view(self, owner: str, flow_id: str):
        if not self.flow or self.flow["owner"] != owner or self.flow["id"] != flow_id:
            raise KeyError("Sign-in not found")
        return {key: self.flow[key] for key in ("id", "phase", "user_code", "authorization_url", "error")
                if key in self.flow}

    def pending(self, owner: str):
        if self.flow and self.flow["owner"] == owner and self.flow["phase"] == "running":
            return self.view(owner, self.flow["id"])
        return None

    async def cancel(self, owner: str, flow_id: str):
        self.view(owner, flow_id)
        await self.close()

    async def close(self):
        if self.task and not self.task.done():
            if not self.task.cancelling():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _run(self, flow, executable, env, *, employee=False):
        proc = None
        close_job = None
        result = {"phase": "failed"}
        try:
            # An existing working account is never silently replaced.
            if await account_signed_in(executable, env, employee=employee):
                result = {"phase": "succeeded"}
                return
            host_exec.ensure_allowed("an engine sign-in")
            if employee and (reason := host_exec.employee_host_blocker()):
                raise LoginError(reason)
            starting = asyncio.create_task(asyncio.create_subprocess_exec(
                *_command(executable, employee), "login", "--device-auth", stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=env, cwd=env["CODEX_HOME"] if employee else str(Path.home()), start_new_session=os.name != "nt",
                **host_exec.background_process_options(),
            ))
            # If cancellation arrives during OS process creation, retain the
            # resulting child so the finally block can kill and reap it.
            try:
                proc = await asyncio.shield(starting)
            except asyncio.CancelledError:
                proc = await starting
                raise
            if employee:
                close_job = host_exec.employee_process_lifetime(proc.pid)
            await asyncio.to_thread(engine_processes.record, proc.pid, str(executable))
            async def receive():
                output = b""
                async with asyncio.timeout(self.prompt_timeout):
                    while "user_code" not in flow:
                        chunk = await proc.stdout.read(1024)
                        if not chunk:
                            raise LoginError("Codex did not provide a device code. Update the CLI and enable device-code login in your ChatGPT security settings.")
                        output += chunk
                        if len(output) > 16384:
                            raise LoginError("Codex returned too much sign-in output.")
                        text = _ANSI.sub("", output.decode("utf-8", "replace"))
                        code = next((_CODE.search(line) for line in text.split("\n")[:-1] if _CODE.search(line)), None)
                        if re.search(r"https://auth\.openai\.com/codex/device(?=[\s\x1b]|$)", text) and code:
                            flow.update(authorization_url=DEVICE_URL, user_code=code[1])
                # Keep draining without retaining arbitrary provider output.
                total = len(output)
                while chunk := await proc.stdout.read(1024):
                    total += len(chunk)
                    if total > 32768:
                        raise LoginError("Codex returned too much sign-in output.")
                if await proc.wait() != 0:
                    raise LoginError("Codex sign-in did not finish. Enable device-code login in ChatGPT, then try again.")
                if not await account_signed_in(executable, env, employee=employee):
                    raise LoginError("Codex did not confirm the new account. Check its status on the server.")
            await asyncio.wait_for(receive(), timeout=self.lifetime)
            result = {"phase": "succeeded"}
        except asyncio.CancelledError:
            result = {"phase": "cancelled"}
        except TimeoutError:
            result = {"phase": "expired", "error": "Codex sign-in timed out. Start again for a new code."}
        except LoginError as exc:
            result = {"phase": "failed", "error": str(exc)}
        except Exception:
            result = {"phase": "failed", "error": "Could not start Codex sign-in. Check the CLI installation on the server."}
        finally:
            if proc:
                if close_job is not None:
                    close_job()
                else:
                    cli_probe.kill_group(proc)
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except TimeoutError:
                    pass
                engine_processes.forget(proc.pid)
            flow.pop("user_code", None)
            flow.pop("authorization_url", None)
            flow.update(result)
