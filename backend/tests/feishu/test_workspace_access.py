import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.feishu import workspace_access as access


def profile(tmp_path):
    path = tmp_path / "feishu.json"
    cfg = {"owner_id":"local-owner","state_dir":"channel",
           "steward":{"app_id":"cli_fixture","open_id":"ou_owner","tenant_key":"test-tenant"}}
    path.write_text(json.dumps(cfg))
    return path, {**cfg, "_access_config_path": str(path)}


def test_owner_permission_settings_are_live_and_cannot_cross_accounts(tmp_path):
    path, cfg = profile(tmp_path)
    before = access.settings_for(cfg)
    contact = {"chat_id":"oc_private","peer_open_id":"ou_colleague","agent_id":None}
    target = {"folder_token":"Folder123", "tenant_origin":"https://example.feishu.cn"}
    result = access.save_settings(path, history_sources={"同事":contact}, document_target=target,
                                  expected_revision=before["revision"])
    assert access.settings_for(cfg) == result and result["history_sources"]["同事"] == contact
    assert access.settings_for({**cfg, "steward":{**cfg["steward"],"open_id":"ou_someoneelse"}})["history_sources"] == {}
    assert "history_sources" not in json.loads(path.read_text())
    with pytest.raises(access.SettingsChanged):
        access.save_settings(path, document_target={}, expected_revision=before["revision"])


def test_only_one_of_two_competing_settings_writes_succeeds(tmp_path):
    path, cfg = profile(tmp_path)
    rev = access.settings_for(cfg)["revision"]
    def write(suffix):
        try:
            return access.save_settings(path, document_target={"folder_token":"Folder"+suffix,
                "tenant_origin":"https://example.feishu.cn"}, expected_revision=rev)["revision"]
        except access.SettingsChanged:
            return None
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sum(bool(result) for result in workers.map(write, ["A","B"])) == 1


@pytest.mark.parametrize("field,value", [
    ("history_sources", {"某群":"oc_group"}),
    ("history_sources", {"甲":{"chat_id":"oc_chat","peer_open_id":"ou_owner","agent_id":False}}),
    ("document_target", {"folder_token":"Folder", "tenant_origin":"https://other.test"}),
    ("document_target", {"folder_token":"Folder", "tenant_origin":"https://t.feishu.cn@other.test"}),
])
def test_settings_validate_identifiers_and_destination(tmp_path, field, value):
    path, cfg = profile(tmp_path)
    with pytest.raises(ValueError):
        access.save_settings(path, expected_revision=access.settings_for(cfg)["revision"], **{field:value})


def test_management_ui_requires_an_owner_browser_and_verifies_selected_chat(tmp_path, monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from app.auth.policy import Principal
    from app.feishu import history
    from app.routers import feishu_access as portal
    app = FastAPI()
    current = [Principal(kind="device", scopes=("admin","chat"))]
    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.auth = current[0]
        return await call_next(request)
    app.include_router(portal.router)
    app.dependency_overrides[portal.get_runtime] = lambda: None
    path,cfg=profile(tmp_path)
    monkeypatch.setenv("FEISHU_CONFIG",str(path))
    seen = []
    def resolve(profile, chat):
        seen.append(chat)
        return "ou_colleague"
    monkeypatch.setattr(history,"resolve_peer",resolve)
    with TestClient(app) as client:
        assert client.get("/feishu/access").status_code == 403
        current[0] = Principal(kind="browser", scopes=("admin",), person_id=2, role="member")
        assert client.get("/feishu/access").status_code == 403
        current[0] = Principal(kind="browser", scopes=("admin",), person_id=1, role="owner", session_id=7, tenant_id="foreign")
        assert client.get("/feishu/access").status_code == 403
        current[0] = Principal(kind="browser", scopes=("admin",), person_id=1, role="owner", session_id=7)
        before=client.get("/feishu/access").json()
        assert before["history_sources"] == {} and before["allow_codex_summary"] is False
        enabled=client.put("/feishu/analysis",json={"revision":before["revision"],"enabled":True}).json()
        ref = {"revision":enabled["revision"],"alias":"同事","chat":"https://applink.feishu.cn/client/chat/open?openChatId=oc_example"}
        stored=client.post("/feishu/contacts",json=ref)
        assert stored.status_code == 200 and seen == ["oc_example"]
        assert stored.json()["history_sources"]["同事"]["peer_open_id"] == "ou_colleague"
        assert client.post("/feishu/contacts",json=ref).status_code == 409 and seen == ["oc_example"]
        folder=client.put("/feishu/document-target", json={"revision":stored.json()["revision"],
                          "folder_url":"https://example.feishu.cn/drive/folder/ABC?from=share"}).json()
        assert folder["document_target"] == {"folder_token":"ABC","tenant_origin":"https://example.feishu.cn"}
        done=client.request("DELETE","/feishu/contacts/同事",json={"revision":folder["revision"]})
        assert done.json()["history_sources"] == {} and done.json()["allow_codex_summary"]
