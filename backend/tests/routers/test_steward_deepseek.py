from contextlib import contextmanager
import os

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from app.auth.middleware import set_principal
from app.auth.policy import Principal
from app.core.config import settings
from app.routers.staff import router
from app.services import steward_deepseek as service


def test_only_deepseek_named_environment_is_a_fallback(monkeypatch):
    monkeypatch.delenv("FEISHU_CONFIG", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-other-provider")
    assert service.api_key() == ""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-deepseek")
    assert service.api_key() == "synthetic-deepseek"


def test_employee_credentials_are_scoped_and_reflect_account_models(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "service.db"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "steward-only")
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(service, "dpapi", lambda data, *, unlock: cipher.decrypt(data) if unlock else cipher.encrypt(data))
    monkeypatch.setattr(service, "_verify_models", lambda key: ["deepseek-flash"])
    assert service.employee_api_key("owner", 5) == ""
    service.save_employee_key("owner", 5, "synthetic-employee-key")
    assert service.employee_api_key("owner", 5) == "synthetic-employee-key"
    assert service.employee_models("owner", 5) == ["deepseek-flash"]
    assert service.employee_api_key("other", 5) == service.employee_api_key("owner", 6) == ""
    assert all(b"synthetic-employee-key" not in path.read_bytes() for path in tmp_path.rglob("*.dpapi"))


def test_verify_fixed_origin_encrypt_replace_and_preserve_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "steward-deepseek.dpapi"
    monkeypatch.setattr(service, "_credential_path", lambda: path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-fallback")
    if os.name != "nt":
        cipher = Fernet(Fernet.generate_key())
        monkeypatch.setattr(service, "dpapi", lambda data, *, unlock: cipher.decrypt(data) if unlock else cipher.encrypt(data))
    attempts = []
    responses = [(200, {"data":[{"id":"deepseek-flash"}]}),
                 (401, {"message":"synthetic-sensitive-server-diagnostic"}),
                 (200, {"data":[{"id":"some-other-model"}]}),
                 (302, {"location":"https://elsewhere.invalid"})]

    def check(req):
        attempts.append(req)
        assert req.method == "GET" and str(req.url) == "https://api.deepseek.com/models"
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    original_client = httpx.Client
    @contextmanager
    def mock_client(**kwargs):
        assert not kwargs["follow_redirects"] and not kwargs["trust_env"]
        with original_client(transport=httpx.MockTransport(check), **kwargs) as cli:
            yield cli
    monkeypatch.setattr(service.httpx, "Client", mock_client)
    service.verify_and_save("test-synthetic-key")
    assert service.api_key() == "test-synthetic-key"
    ciphertext = path.read_bytes()
    assert b"test-synthetic-key" not in ciphertext
    assert attempts[0].headers["Authorization"] == "Bearer test-synthetic-key"
    for _ in range(3):
        with pytest.raises(ValueError) as err:
            service.verify_and_save("test-replacement")
        assert "synthetic-sensitive" not in str(err.value) and "test-replacement" not in str(err.value)
        assert path.read_bytes() == ciphertext


@pytest.mark.asyncio
async def test_only_owner_browser_can_save_or_query_and_no_secret_is_returned(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_EXECUTION_ENABLED", True)
    monkeypatch.setattr(service, "api_key", lambda: "test-private-key")
    calls = []
    monkeypatch.setattr(service, "verify_and_save", calls.append)
    app = FastAPI()
    active = None
    @app.middleware("http")
    async def session(request, next_handler):
        if active:
            set_principal(request.scope, active)
        return await next_handler(request)
    app.include_router(router)
    wire = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=wire, base_url="http://localhost") as client:
        url = "/staff/deepseek-key"
        for actor in (None, Principal("device"), Principal("desktop"), Principal("loopback"), Principal("grant"),
                      Principal("browser", session_id=4, role="admin", person_id=9),
                      Principal("browser", session_id=4, role="owner", tenant_id="other")):
            active = actor
            assert (await client.get(url)).status_code == 403
            assert (await client.put(url, json={"api_key":"test-private-key"})).status_code == 403
        assert calls == []
        active = Principal("browser", session_id=4, role="owner", person_id=1)
        response = await client.get(url)
        assert response.json() == {"configured": True} and response.headers["cache-control"] == "no-store"
        response = await client.put(url, json={"api_key":"test-private-key"})
        assert response.json() == {"configured": True} and calls == ["test-private-key"]
        for bad in ({"api_key":["test-private-key"]}, {"api_key":"test-private-key","extra":True}, {"api_key":"x"*3000}):
            denied = await client.put(url, json=bad)
            assert denied.status_code in (413, 422) and "test-private-key" not in denied.text
        assert calls == ["test-private-key"]
