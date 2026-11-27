"""Employee roots, private Codex profiles and real Windows filesystem enforcement."""
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings


def employee_project(tmp_path, monkeypatch, *, mode="accept_edits"):
    from app.services import employee_runtime

    service = tmp_path / "service"
    service.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "SQLITE_PATH", str(service / "app.db"))
    root = tmp_path / "employee project"
    peer = tmp_path / "teammate"
    for folder in (root, peer):
        folder.mkdir(exist_ok=True)
        (folder / "AGENTS.md").write_text("Role instructions", encoding="utf-8")
    owner = "employee-test-owner"
    monkeypatch.setenv("SUPARAGENT_EMPLOYEE_NATIVE_IDS", f"{owner}/7")
    agent = SimpleNamespace(id=7, owner_id=owner, role_path=str(root), approval_mode=mode,
                            team_workspaces_readable=True)
    item = SimpleNamespace(id=4, owner_id=owner, agent_id=agent.id)
    return employee_runtime.resolve_project_runtime(agent, item, approval_mode=mode,
        readable_roots=(peer,), model_vendor="openai", assignment_epoch=1)


def test_role_root_and_private_profile_are_bound_to_ids(tmp_path, monkeypatch):
    from app.services import employee_runtime

    runtime = employee_project(tmp_path, monkeypatch)
    assert runtime.root == tmp_path / "employee project"
    assert runtime.writable and runtime.readable_roots == (tmp_path / "teammate",)
    assert runtime.task_dir == runtime.root / "任务" / "4"
    assert runtime.profile == employee_runtime.profile_dir(runtime.owner_id, 7, "openai")
    assert runtime.profile != employee_runtime.profile_dir(runtime.owner_id, 8, "openai")
    assert runtime.profile != employee_runtime.profile_dir(runtime.owner_id, 7, "deepseek")
    assert runtime.profile.is_relative_to(tmp_path / "service")
    assert not runtime.profile.is_relative_to(runtime.root)
    assert employee_runtime.native_enabled(runtime.owner_id, 7)
    assert not employee_runtime.native_enabled("another-owner", 7)
    assert not employee_runtime.native_enabled(runtime.owner_id, 8)
    env = runtime.process_environment({"PATH": "tools", "CODEX_HOME": "personal",
        "CODEX_SQLITE_HOME": "personal-index", "OPENAI_API_KEY": "private", "GITHUB_TOKEN": "private",
        "MY_SETTING": "private", "HOME": "personal-home", "XDG_CONFIG_HOME": "personal-config"})
    assert Path(env["CODEX_HOME"]) == Path(env["CODEX_SQLITE_HOME"]) == runtime.profile
    assert "private" not in env.values() and not any("personal" in v for v in env.values())
    login_env = employee_runtime.profile_environment(runtime.owner_id, 7, "openai", {
        "CODEX_HOME": "personal", "OPENAI_API_KEY": "private", "PATH": "personal"})
    assert Path(login_env["CODEX_HOME"]) == runtime.profile and "OPENAI_API_KEY" not in login_env
    assert "personal" not in login_env.values() and login_env["CODEX_SQLITE_HOME"] == env["CODEX_SQLITE_HOME"]
    with pytest.raises(ValueError):
        employee_runtime.profile_dir(runtime.owner_id, 7, "../other")


def test_readonly_scope_and_project_writer_lock(tmp_path, monkeypatch):
    from app.services import employee_runtime

    writer = employee_project(tmp_path, monkeypatch)
    reader = employee_project(tmp_path, monkeypatch, mode="plan")
    assert not reader.writable
    with writer.writer_lock(), reader.writer_lock():
        with pytest.raises(employee_runtime.ProjectBusy):
            with writer.writer_lock():
                pytest.fail("two writers acquired one project")
    with writer.writer_lock():
        pass


def test_binding_and_tool_scope_cannot_outlive_project_grants(tmp_path, monkeypatch):
    from dataclasses import replace

    folder = tmp_path / "references"
    native = replace(employee_project(tmp_path, monkeypatch), external_reads=(folder,), outbound_network=True)
    assert native.has_terminal_access([str(folder)], True)
    assert not native.has_terminal_access([], True)
    assert not native.has_terminal_access([str(folder)], False)
    assert replace(native, outbound_network=False).has_terminal_access([folder], True)


@pytest.mark.skipif(os.name != "nt", reason="Windows Store runtime tools")
def test_rg_comes_from_codex_package_even_with_project_controlled_path(tmp_path, monkeypatch):
    root = tmp_path / "employee project"
    root.mkdir()
    (root / "rg.exe").touch()
    (root / "codex.exe").touch()
    install = tmp_path / "program-files"
    bundled = install / "WindowsApps" / "OpenAI.Codex_1.0_x64__2p2nqsd0c76g0" / "app" / "resources"
    bundled.mkdir(parents=True)
    rg = bundled / "rg.exe"
    rg.touch()
    (bundled / "codex.exe").touch()
    monkeypatch.setenv("ProgramFiles", str(install))
    monkeypatch.setenv("PATH", str(root))
    monkeypatch.setenv("SUPARAGENT_PACKAGED_CODEX_PATH", str(bundled / "codex.exe"))
    monkeypatch.setattr(settings, "LOCAL_CODEX_CLI_PATH", str(root / "codex.exe"))
    runtime = employee_project(tmp_path, monkeypatch)
    env = runtime.process_environment()
    assert ("rg", rg, rg) in runtime.installed_tools
    assert env["PATH"].split(os.pathsep)[0] == str(bundled)
    assert env["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert 'shell_environment_policy.set.NoDefaultCurrentDirectoryInExePath="1"' in runtime.cli_options()
    rg.unlink()
    assert not any(name == "rg" for name, *_ in employee_project(tmp_path, monkeypatch).installed_tools)


def test_short_private_storage_keeps_existing_profiles_and_rejects_unsafe_paths(tmp_path, monkeypatch):
    from app.services import employee_runtime

    existing = employee_project(tmp_path, monkeypatch)
    old = existing.profile
    existing.process_environment()
    (old / "sentinel").write_text("old", encoding="utf-8")
    chosen = tmp_path / "s"
    monkeypatch.setenv("SUPARAGENT_EMPLOYEE_RUNTIME_ROOT", str(chosen))
    assert employee_runtime.profile_dir(existing.owner_id, 7, "openai") == old
    fresh = employee_runtime.profile_dir(existing.owner_id, 8, "openai")
    assert fresh.is_relative_to(chosen) and not fresh.exists()
    assert (old / "sentinel").read_text(encoding="utf-8") == "old"
    from dataclasses import replace
    short_home = replace(existing, employee_id=8, profile_path=fresh)
    saved = short_home.stamp
    short_home.check_previous_storage(saved)
    monkeypatch.setenv("SUPARAGENT_EMPLOYEE_RUNTIME_ROOT", str(tmp_path / "elsewhere"))
    moved = replace(short_home, profile_path=employee_runtime.profile_dir(existing.owner_id, 8, "openai"))
    with pytest.raises(employee_runtime.BoundaryUnavailable, match="另一服务目录"):
        moved.check_previous_storage(saved)
    for unsafe in ("relative-path", str(existing.root), str(tmp_path / "service")):
        monkeypatch.setenv("SUPARAGENT_EMPLOYEE_RUNTIME_ROOT", unsafe)
        with pytest.raises(ValueError):
            employee_project(tmp_path, monkeypatch)


async def test_visible_station_refuses_before_any_probe(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from app.core import host_exec
    from app.services import employee_runtime

    runtime = employee_project(tmp_path, monkeypatch)
    monkeypatch.setattr(host_exec, "noninteractive_window_station", lambda: False)
    probe = AsyncMock(side_effect=AssertionError("a visible desktop must not start a probe"))
    monkeypatch.setattr(employee_runtime.cli_probe, "run", probe)
    with pytest.raises(employee_runtime.BoundaryUnavailable):
        await runtime.check_host_boundary(Path("codex.exe"), {})
    probe.assert_not_awaited()


@pytest.mark.parametrize(("token", "message"), [("elevated", "提升"), ("administrator", "管理员组"), ("unknown", "无法核实")])
@pytest.mark.skipif(sys.platform != "win32", reason="Windows employee launch policy")
async def test_noninteractive_worker_rejects_privileged_or_unknown_token(tmp_path, monkeypatch, token, message):
    from unittest.mock import AsyncMock
    from app.core import host_exec
    from app.services import employee_runtime
    from app.services.stdio_rpc import StdioRpc

    runtime = employee_project(tmp_path, monkeypatch)
    monkeypatch.setattr(host_exec, "noninteractive_window_station", lambda: True)
    monkeypatch.setattr(host_exec, "windows_worker_token_status", lambda: token)
    spawn = AsyncMock(side_effect=AssertionError("a rejected worker must never touch a child process"))
    monkeypatch.setattr(employee_runtime.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(employee_runtime.BoundaryUnavailable, match=message):
        await runtime.check_host_boundary(Path("codex.exe"), {})
    login = await employee_runtime.cli_probe.run(["codex.exe"], timeout=1, employee_project=True)
    assert not login.ok and login.failed_to_start
    with pytest.raises(PermissionError, match=message):
        await StdioRpc.spawn(["codex.exe"], cwd=str(runtime.root), env={}, max_bytes=1, employee_project=True)
    spawn.assert_not_awaited()


def test_standard_noninteractive_token_can_pass_preflight_without_launch(monkeypatch):
    from app.core import host_exec

    monkeypatch.setattr(host_exec, "noninteractive_window_station", lambda: True)
    monkeypatch.setattr(host_exec, "windows_worker_token_status", lambda: "standard")
    assert host_exec.employee_host_blocker() is None
    assert host_exec.employee_host_blocker(acceptance=True) is None


def test_uncertain_native_start_preserves_one_reservation(tmp_path, monkeypatch):
    from uuid import uuid4
    from app.services import employee_runtime

    runtime = employee_project(tmp_path, monkeypatch)
    assert runtime.reserve_native_start(31) is None
    with pytest.raises(employee_runtime.BoundaryUnavailable):
        runtime.reserve_native_start(31)
    native = str(uuid4())
    runtime.record_native_start(31, native)
    assert runtime.reserve_native_start(31) == native
    assert runtime.reserve_native_start(32) is None
    assert runtime.bound_native_id(31) == native
    assert runtime.bound_native_id(32) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows MXC acceptance")
async def test_shell_uses_role_root_and_respects_peer_grant(tmp_path, monkeypatch):
    from app.services import employee_runtime
    from app.core import host_exec

    if os.environ.get("SUPARAGENT_RUN_WINDOWS_MXC_ACCEPTANCE") != "1" or host_exec.employee_host_blocker(acceptance=True):
        pytest.skip("Real MXC acceptance requires a noninteractive ordinary Windows token and an explicit run.")

    exe = shutil.which("codex.exe")
    if not exe:
        pytest.skip("The Windows Codex CLI is not installed.")
    runtime = employee_project(tmp_path, monkeypatch)
    env = runtime.process_environment(os.environ)
    try:
        await runtime.check_host_boundary(Path(exe), env)
    except employee_runtime.BoundaryUnavailable as exc:
        if exc.unsupported:
            pytest.skip(str(exc))
        raise
    # Use normal Windows shell children; a second ordinary process reads the artifact.
    marker = runtime.root / "sample.txt"
    peer = runtime.readable_roots[0] / "AGENTS.md"
    system = Path(os.environ["SYSTEMROOT"]) / "System32"
    shell = [str(system / "cmd.exe"), "/d", "/c"]
    def run(argv, selected=runtime):
        return subprocess.run(selected.sandbox_command(Path(exe), argv), cwd=selected.root,
                              env=env, timeout=45, capture_output=True, **host_exec.background_process_options())
    assert run([*shell, "echo from-child>sample.txt"]).returncode == 0
    assert run([str(system / "findstr.exe"), "/m", "instructions", str(peer)]).returncode == 0
    assert run([*shell, r"echo changed>..\teammate\AGENTS.md"]).returncode != 0
    denied = tmp_path / "ungranted.txt"
    denied.write_text("private-marker", encoding="utf-8")
    assert run([str(system / "findstr.exe"), "/m", "private-marker", str(denied)]).returncode != 0
    reader = employee_project(tmp_path, monkeypatch, mode="plan")
    assert run([*shell, "echo changed>sample.txt"], reader).returncode != 0
    check = subprocess.check_output([sys.executable, "-I", "-c",
        "from pathlib import Path;print(Path('sample.txt').read_text())"], cwd=runtime.root, text=True,
        **host_exec.background_process_options())
    assert check.strip() == "from-child" and marker.is_file()
    assert peer.read_text(encoding="utf-8") == "Role instructions"
    python = Path(sys.base_prefix) / "python.exe"
    if python.is_file():
        # The installed interpreter runs the project's actual test and can repair the fixture.
        (runtime.root / "answer.py").write_text("value = 0\n", encoding="utf-8")
        (runtime.root / "check.py").write_text("from answer import value\nassert value == 1\n", encoding="utf-8")
        assert run([str(python), "-B", "-E", "-s", "check.py"]).returncode == 1
        repair = "from pathlib import Path; Path('answer.py').write_text('value = 1\\n')"
        assert run([str(python), "-I", "-c", repair]).returncode == 0
        assert run([str(python), "-B", "-E", "-s", "check.py"]).returncode == 0
        subprocess.run([str(python), "-B", "-E", "-s", "check.py"], cwd=runtime.root, check=True,
                       **host_exec.background_process_options())

def test_workstation_profiles_keep_host_and_project_writes_separate(tmp_path, monkeypatch):
    import tomllib
    from dataclasses import replace
    from app.services import employee_runtime

    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", "employee-test-owner")
    project = employee_project(tmp_path, monkeypatch)
    foreign = tmp_path / "another owner"
    pinned = settings.SQLITE_FILE.parent / "employee-settings" / "library" / "tool.py"
    project = replace(project, employee_folders=(project.root, foreign), pinned_files=(pinned,))
    assert employee_runtime.native_enabled(project.owner_id, 8)
    assert not employee_runtime.native_enabled("another owner", 8)
    fs = tomllib.loads("fs=" + project._filesystem())["fs"]
    assert fs[":root"] == "read" and fs[str(project.root)] == fs[str(project.task_dir)] == "write"
    assert fs[str(pinned)] == "read" and fs[str(foreign)] == "deny"
    assert fs[str(settings.SQLITE_FILE.parent)] == "deny"
    assert fs[str(project.scratch)] == "write"
    assert str(project.scratch) != project.process_environment()["TEMP"]
    assert project.scratch.is_dir()
    reviewed = tomllib.loads("fs=" + project._filesystem(reviewed=True))["fs"]
    assert reviewed[str(project.root)] == reviewed[str(project.task_dir)] == "write"
    assert reviewed[str(project.root / "AGENTS.md")] == reviewed[str(project.root / "任务")] == "read"
    assert "write" not in tomllib.loads("fs=" + project._filesystem(check=True))["fs"].values()
    reader = replace(project, writable=False)
    assert tomllib.loads("fs=" + reader._filesystem())["fs"][str(reader.root)] == "read"
    assert reader.reviewed_permission_name != project.reviewed_permission_name
    assert not any(reader.reviewed_permission_name in opt for opt in reader.cli_options())
    assert not any("network.enabled=true" in opt for opt in project.cli_options())
    employee_runtime.save_native_enabled(project.owner_id, 8, False)
    assert not employee_runtime.native_enabled(project.owner_id, 8)


@pytest.mark.skipif(os.name != "nt", reason="Windows workstation shell settings")
def test_employee_desktop_shell_uses_own_appdata_and_machine_installations(tmp_path, monkeypatch):
    import tomllib
    from dataclasses import replace

    project = employee_project(tmp_path, monkeypatch)
    desktop = replace(project, local_workstation=True)
    user = tmp_path / "windows-user"
    tools = tmp_path / "Program Files" / "installed-tools"
    tools.mkdir(parents=True)
    monkeypatch.setenv("PROGRAMFILES", str(tools.parent))
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setenv("APPDATA", str(user / "AppData" / "Roaming"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(tools), ".", str(project.root)]))
    monkeypatch.setenv("CODEX_HOME", str(user / ".codex"))
    monkeypatch.setenv("OPENAI_API_KEY", "never-pass-to-shell")

    def shell_settings(runtime):
        options = runtime.cli_options()[1::2]
        return tomllib.loads("\n".join(options))["shell_environment_policy"]["set"]

    shell = shell_settings(desktop)
    assert shell["HOME"] == shell["USERPROFILE"] == str(desktop.desktop_home)
    assert Path(shell["APPDATA"]).is_relative_to(desktop.desktop_home)
    assert Path(shell["LOCALAPPDATA"]).is_dir()
    fs = tomllib.loads("fs=" + desktop._filesystem())["fs"]
    assert fs[str(desktop.desktop_home)] == "write"
    assert fs[str(desktop.profile.parents[3])] == "deny"
    assert desktop.desktop_home == replace(desktop, task_id=42).desktop_home
    assert desktop.desktop_home != replace(desktop, employee_id=8, profile_path=None).desktop_home
    readonly = replace(desktop, writable=False)
    assert tomllib.loads("fs=" + readonly._filesystem())["fs"][str(desktop.desktop_home)] == "read"
    assert shell["CODEX_HOME"] == shell["CODEX_SQLITE_HOME"] == str(desktop.profile)
    assert shell["TEMP"] == str(desktop.scratch)
    assert str(tools) in shell["PATH"].split(os.pathsep)
    assert str(project.root) not in shell["PATH"].split(os.pathsep) and "." not in shell["PATH"].split(os.pathsep)
    assert "OPENAI_API_KEY" not in shell
    assert "USERPROFILE" not in shell_settings(project)
    broker = desktop.process_environment()
    assert broker["USERPROFILE"] == broker["CODEX_HOME"] == str(desktop.profile)
    assert str(tools) not in broker["PATH"].split(os.pathsep)
