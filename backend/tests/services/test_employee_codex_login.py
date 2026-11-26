import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services import cli_probe, codex_login


@pytest.mark.asyncio
async def test_employee_device_code_both_status_checks_and_login_share_only_its_file_store(tmp_path, monkeypatch):
    home = tmp_path / "employee"
    home.mkdir()
    env = {"HOME": str(home), "CODEX_HOME": str(home), "USERPROFILE": str(home)}
    fake_exe = tmp_path / "codex.exe"
    calls = []
    async def probe(args, **options):
        calls.append((args, options))
        if (home / "auth.json").exists():
            return cli_probe.ProbeRun(0, "", "Logged in using ChatGPT")
        return cli_probe.ProbeRun(1, "", "Not logged in")

    output = asyncio.StreamReader()
    output.feed_data(b"https://auth.openai.com/codex/device\nAB12-CD34\n")
    finished = asyncio.Event()
    class InMemoryProcess:
        pid = 42
        stdout = output
        returncode = None
        async def wait(self):
            await finished.wait()
            return 0

    async def spawn(*args, **options):
        calls.append((list(args), options))
        return InMemoryProcess()

    monkeypatch.setattr(codex_login.cli_probe, "run", probe)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(codex_login, "child_env", lambda: pytest.fail("global credentials accessed"))
    monkeypatch.setattr(codex_login.host_exec, "employee_host_blocker", lambda: None)
    monkeypatch.setattr(codex_login.host_exec, "background_process_options", lambda: {})
    monkeypatch.setattr(codex_login.host_exec, "employee_process_lifetime", lambda _: lambda: None)
    monkeypatch.setattr(codex_login.engine_processes, "record", lambda *_: None)
    monkeypatch.setattr(codex_login.engine_processes, "forget", lambda *_: None)
    monkeypatch.setattr(codex_login.cli_probe, "kill_group", lambda _: None)
    manager = codex_login.CodexLogin()
    try:
        flow = await manager.start("browser:1", fake_exe, employee_env=env)
        async with asyncio.timeout(2):
            while "user_code" not in manager.view("browser:1", flow["id"]):
                await asyncio.sleep(.001)
        assert manager.view("browser:1", flow["id"])["user_code"] == "AB12-CD34"
        (home / "auth.json").write_text("session-secret")
        output.feed_eof()
        finished.set()
        await manager.task
        assert manager.view("browser:1", flow["id"]) == {"id":flow["id"], "phase":"succeeded"}
        for args, opts in calls:
            assert args[:3] == [str(fake_exe.resolve()), "-c", 'cli_auth_credentials_store="file"']
            assert opts["env"] == env and Path(opts["cwd"]) == home
        assert [args[3:] for args, _ in calls] == [["login","status"], ["login","--device-auth"], ["login","status"]]
    finally:
        finished.set()
        output.feed_eof()
        await manager.close()


@pytest.mark.asyncio
async def test_foreign_or_missing_employee_login_file_cannot_be_reported_as_connected(tmp_path, monkeypatch):
    env = {"HOME":str(tmp_path), "CODEX_HOME":str(tmp_path)}
    probe = AsyncMock(return_value=cli_probe.ProbeRun(0, "", "Logged in using ChatGPT"))
    monkeypatch.setattr(cli_probe, "run", probe)
    with pytest.raises(codex_login.LoginError):
        await codex_login.account_signed_in(tmp_path / "codex.exe", env, employee=True)
    (tmp_path / "auth.json").write_text("saved-token")
    probe.return_value = cli_probe.ProbeRun(0, "", "Logged in using an API key: private-secret")
    with pytest.raises(codex_login.LoginError) as failure:
        await codex_login.account_signed_in(tmp_path / "codex.exe", env, employee=True)
    assert "private-secret" not in str(failure.value)
