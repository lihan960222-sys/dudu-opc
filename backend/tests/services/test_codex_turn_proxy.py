"""The Codex-only proxy is explicit and scoped to an app-server process."""
import pytest

from app.services import codex_adapter
from tests.services.test_codex_adapter import codex, crt, codex_thread, finished, owner, sessions  # noqa: F401


def env(monkeypatch, **values):
    monkeypatch.setattr(codex_adapter.local_claude_runner, "child_env", lambda: dict(values))
    monkeypatch.setenv("SUPARAGENT_CODEX_HTTPS_PROXY", "http://127.0.0.1:7890")


def test_local_proxy_preserves_bypasses_and_does_not_modify_host(monkeypatch):
    env(monkeypatch, NO_PROXY="company.example", PATH="engine")
    configured = codex_adapter._codex_turn_env(platform="win32")
    assert configured["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert "HTTP_PROXY" not in configured and "ALL_PROXY" not in configured
    assert {"localhost", "127.0.0.1", "::1", "company.example"} <= set(configured["NO_PROXY"].split(","))
    assert "HTTPS_PROXY" not in codex_adapter.local_claude_runner.child_env()


@pytest.mark.parametrize("var", ["https_proxy", "HTTP_PROXY", "ALL_PROXY"])
def test_existing_proxy_wins_entirely(monkeypatch, var):
    env(monkeypatch, **{var: "http://operator.example:7000"})
    assert codex_adapter._codex_turn_env(platform="win32") is None


@pytest.mark.parametrize("url", ["https://127.0.0.1:80","http://a:b@127.0.0.1:90",
    "http://remote.example:80","http://127.0.0.1:80/path","http://127.0.0.1:65536",
    "http://127.0.0.1:0","http://127.0.0.1:80?","http://127.0.0.1:80#private"])
def test_malformed_or_nonlocal_proxy_is_rejected_without_echoing_value(monkeypatch, url):
    env(monkeypatch)
    monkeypatch.setenv("SUPARAGENT_CODEX_HTTPS_PROXY", url)
    with pytest.raises(ValueError, match="loopback") as exc:
        codex_adapter._codex_turn_env(platform="win32")
    assert url not in str(exc.value)


def test_opt_in_is_off_by_default_and_windows_only(monkeypatch):
    env(monkeypatch)
    assert codex_adapter._codex_turn_env(platform="linux") is None
    monkeypatch.delenv("SUPARAGENT_CODEX_HTTPS_PROXY")
    assert codex_adapter._codex_turn_env(platform="win32") is None


async def test_opt_in_reaches_turn_process_without_changing_thread_mcp_config(monkeypatch, codex, crt, sessions, owner):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SUPARAGENT_CODEX_HTTPS_PROXY", "http://127.0.0.1:7890")
    create_env = codex_adapter._codex_turn_env
    monkeypatch.setattr(codex_adapter, "_codex_turn_env", lambda: create_env(platform="win32"))
    captured = []
    original = codex_adapter.StdioRpc.spawn

    async def capture(*args, **kwargs):
        if kwargs.get("sandbox"):
            captured.append(kwargs.get("env"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(codex_adapter.StdioRpc, "spawn", staticmethod(capture))
    thread = await codex_thread(crt, sessions, owner)
    await crt.send_turn(thread.id, "OK")
    await finished(sessions, thread.id)
    assert captured and captured[0]["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert "SUPARAGENT_CODEX_HTTPS_PROXY" not in captured[0]
    settings = codex.received("thread/start")[0]["params"].get("config", {})
    assert not any(not item.get("enabled", True) or "startup_timeout_sec" in item
                   for item in settings.get("mcp_servers", {}).values())
