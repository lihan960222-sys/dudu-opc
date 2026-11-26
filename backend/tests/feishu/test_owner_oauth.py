from urllib.parse import parse_qs, urlsplit
from concurrent.futures import ThreadPoolExecutor
import hashlib
import base64
import httpx
import pytest


def test_owner_match_single_use_and_locked_refresh(tmp_path, monkeypatch):
    from app.feishu import owner_oauth as oauth
    config = {"_access_config_path":str(tmp_path/"feishu.json"), "owner_id":"local",
              "steward":{"app_id":"cli_test","open_id":"ou_owner","secret_env":"FEISHU_TEST_SECRET"}}
    monkeypatch.setenv("FEISHU_TEST_SECRET","demo-app-secret")
    calls=[]
    who=["ou_other"]
    issued=[]
    def api(method,url,**kwargs):
        calls.append((method,url,kwargs))
        if url==oauth.USER_INFO:
            return {"code":0,"data":{"open_id":who[0]}}
        form=kwargs.get("json") or kwargs["data"]
        if form["grant_type"]=="authorization_code":
            assert url=="https://open.feishu.cn/open-apis/authen/v2/oauth/token" and "json" in kwargs
            assert base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).decode().rstrip("=")==challenge
        else:
            assert form["refresh_token"]=="demo-refresh-1"
            issued.append(1)
        return {"access_token":"demo-access-"+str(len(issued)),"refresh_token":"demo-refresh-"+str(1+len(issued)),
                "expires_in":3600,"refresh_token_expires_in":86400,"scope":" ".join(oauth.ACTIONS["doc"] | {"offline_access"})}
    monkeypatch.setattr(oauth,"_request",api)
    uri="http://localhost:3000/api/v1/feishu/owner-oauth/callback"
    result=oauth.start(config,"doc",uri,"browser-A")
    query=parse_qs(urlsplit(result).query)
    challenge=query["code_challenge"][0]
    assert query["code_challenge_method"]==["S256"] and "im:" not in query["scope"][0]
    with pytest.raises(oauth.OwnerOAuthError):
        oauth.finish(config,query["state"][0],"browser-B",uri,"code")
    with pytest.raises(oauth.OwnerOAuthError):
        oauth.finish(config,query["state"][0],"browser-A",uri,"code")
    assert not oauth.status(config)["connected"]
    who[0]="ou_owner"
    with pytest.raises(oauth.OwnerOAuthError):
        oauth.finish(config,query["state"][0],"browser-A",uri,"code")
    query=parse_qs(urlsplit(oauth.start(config,"doc",uri,"browser-A")).query)
    challenge=query["code_challenge"][0]
    oauth.finish(config,query["state"][0],"browser-A",uri,"code")
    assert oauth.status(config)["actions"]=={"doc":True,"history":False,"delete":False,"folder":False,"wiki":False}
    with pytest.raises(oauth.OwnerOAuthError):
        oauth.user_headers(config,required=oauth.ACTIONS["history"])
    assert oauth.user_headers(config,required=oauth.ACTIONS["doc"])=={"Authorization":"Bearer demo-access-0"}
    now=oauth.time.time()
    monkeypatch.setattr(oauth.time,"time",lambda:now+3601)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _: oauth.user_headers(config,required=oauth.ACTIONS["doc"]),range(4)))
    assert all(r["Authorization"]=="Bearer demo-access-1" for r in results)
    assert len(issued)==1
    assert b"demo-refresh" not in (tmp_path/"owner-oauth.sqlite3").read_bytes()
    oauth.disconnect(config)
    assert not oauth.status(config)["connected"]


def test_owner_browser_and_callback_origin(monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.auth.policy import Principal
    from app.core.config import settings
    from app.routers import feishu_owner_oauth as routes
    def req(kind,role,host):
        return Request({"type":"http","scheme":"http","path":"/api/v1/feishu/owner-oauth/start","query_string":b"",
                        "headers":[(b"host",host.encode())],"state":{"auth":Principal(kind=kind,role=role)}})
    monkeypatch.setattr(settings,"SUPARAGENT_PUBLIC_URL","")
    assert routes.redirect_uri(req("loopback",None,"localhost:3000")).startswith("http://localhost:3000/")
    with pytest.raises(HTTPException):
        routes.redirect_uri(req("browser","owner","public.example"))
    for kind,role in [("device",None),("browser","member"),("loopback",None),("desktop",None),("browser","owner")]:
        with pytest.raises(HTTPException):
            routes.owner_browser(req(kind,role,"localhost:3000"))


def test_http_browser_roundtrip_and_dashboard(tmp_path,monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from app.auth.policy import Principal
    from app.feishu import owner_oauth as oauth
    from app.routers import feishu_owner_oauth as routes
    config={"_access_config_path":str(tmp_path/"feishu.json"),"owner_id":"local",
            "steward":{"open_id":"ou_me","app_id":"cli_test","secret_env":"FEISHU_TEST_SECRET"}}
    monkeypatch.setattr(routes,"config_from_env",lambda:config)
    monkeypatch.setenv("FEISHU_TEST_SECRET","demo")
    def fake(method,url,**kwargs):
        if url==oauth.USER_INFO:
            return {"data":{"open_id":"ou_me"}}
        return {"access_token":"u-demo","refresh_token":"r-demo","expires_in":800,
                "refresh_token_expires_in":9000,"scope":" ".join(oauth.ALL_SCOPES)}
    monkeypatch.setattr(oauth,"_request",fake)
    app=FastAPI()
    @app.middleware("http")
    async def identity(request: Request,call_next):
        request.state.auth=Principal(kind="browser",role="owner",session_id=1)
        return await call_next(request)
    app.include_router(routes.router,prefix="/api/v1")
    prefix="/api/v1/feishu/owner-oauth"
    with TestClient(app,base_url="http://localhost:8787") as owner, TestClient(app,base_url="http://localhost:8787") as stranger:
        unlinked=owner.get(prefix+"/status").json()
        assert not unlinked["connected"]
        assert unlinked["redirect_uri"]=="http://localhost:8787"+prefix+"/callback"
        link=owner.post(prefix+"/start",json={"action":"both"})
        assert link.status_code==200 and "httponly" in link.headers["set-cookie"].lower()
        fields=parse_qs(urlsplit(link.json()["authorize_url"]).query)
        assert set(fields["scope"][0].split())==(oauth.ACTIONS["history"] | oauth.ACTIONS["doc"] | {"offline_access"})
        args={"code":"fake","state":fields["state"][0]}
        assert stranger.get(prefix+"/callback",params=args,follow_redirects=False).headers["location"].endswith("=error")
        done=owner.get(prefix+"/callback",params=args,follow_redirects=False)
        assert done.status_code==303 and done.headers["location"]=="http://localhost:8787/?section=host&feishu_oauth=connected"
        assert owner.get(prefix+"/status").json()["actions"]=={"doc":True,"history":True,"delete":False,"folder":False,"wiki":False}
        assert owner.get(prefix+"/callback",params=args,follow_redirects=False).headers["location"].endswith("=error")
        assert not owner.delete(prefix+"/connection").json()["connected"]
        assert owner.post(prefix+"/start",json={"action":"doc","redirect_uri":"https://public.example"}).status_code==422

        config["steward"]["tenant_key"] = "test-tenant"
        config["roles"] = {"employee": {"app_id": "cli_testemployee", "agent_id": 4,
                                      "owner_open_id": "ou_me", "secret_env": "FEISHU_TEST_SECRET"}}
        start = owner.post(prefix + "/start?employee=4", json={"action": "folder"})
        state = parse_qs(urlsplit(start.json()["authorize_url"]).query)["state"][0]
        done = owner.get(prefix + "/callback", params={"state": state, "code": "fake"}, follow_redirects=False)
        assert done.status_code == 303
        assert parse_qs(urlsplit(done.headers["location"]).query) == {
            "section": ["people"], "capability": ["feishu"], "employee": ["4"], "feishu_oauth": ["connected"]}
        assert owner.get(prefix + "/status?employee=4").json()["actions"]["folder"]
        assert not owner.get(prefix + "/status").json()["actions"]["folder"]


def test_extending_consent_keeps_prior_capabilities_until_confirmed(tmp_path,monkeypatch):
    from app.feishu import owner_oauth as oauth
    cfg={"_access_config_path":str(tmp_path/"feishu.json"),"owner_id":"test",
         "steward":{"app_id":"cli_demo","open_id":"ou_me","secret_env":"FEISHU_DEMO_SECRET"}}
    monkeypatch.setenv("FEISHU_DEMO_SECRET","demo")
    actual=set(oauth.ACTIONS["history"])|{"offline_access"}
    def api(method,url,**kwargs):
        if url==oauth.USER_INFO:
            return {"data":{"open_id":"ou_me"}}
        return {"access_token":"demo","refresh_token":"demo","expires_in":3600,
                "refresh_token_expires_in":6200,"scope":" ".join(sorted(actual))}
    monkeypatch.setattr(oauth,"_request",api)
    def begin(action):
        return parse_qs(urlsplit(oauth.start(cfg,action,"http://localhost/cb","browser")).query)
    def finish(q):
        oauth.finish(cfg,q["state"][0],"browser","http://localhost/cb","code")
    finish(begin("history"))
    ext=begin("doc")
    assert set(ext["scope"][0].split())==(oauth.ACTIONS["history"] | oauth.ACTIONS["doc"] | {"offline_access"})
    actual.clear()
    actual.update(oauth.ACTIONS["doc"]|{"offline_access"})
    with pytest.raises(oauth.OwnerOAuthError):
        finish(ext)
    assert oauth.status(cfg)["actions"]=={"history":True,"doc":False,"delete":False,"folder":False,"wiki":False}
    actual.update(oauth.ACTIONS["history"])
    finish(begin("doc"))
    assert oauth.status(cfg)["actions"]=={"history":True,"doc":True,"delete":False,"folder":False,"wiki":False}


@pytest.mark.skipif(__import__("os").name!="nt",reason="Windows credential file format")
def test_powershell_dpapi_secret_compatibility_uses_test_profile(tmp_path,monkeypatch):
    from app.feishu import owner_oauth as oauth
    from app.feishu.setup_specialist import dpapi
    config={"_access_config_path":str(tmp_path/"feishu.json"),
            "steward":{"open_id":"ou_test","app_id":"cli_demo","secret_env":"FEISHU_DEMO_SECRET"}}
    monkeypatch.delenv("FEISHU_DEMO_SECRET",raising=False)
    (tmp_path/"cli_demo.dpapi").write_text(dpapi("demo-string".encode("utf-16-le"),unlock=False).hex(),encoding="ascii")
    assert oauth._app_secret(config)=="demo-string"


def test_expired_flow_and_disconnected_pending_callback(tmp_path,monkeypatch):
    from app.feishu import owner_oauth as oauth
    cfg={"_access_config_path":str(tmp_path/"feishu.json"),"steward":{"app_id":"cli_demo","open_id":"ou_me","secret_env":"DEMO_SECRET"}}
    uri="http://localhost/cb"
    query=parse_qs(urlsplit(oauth.start(cfg,"doc",uri,"browser")).query)
    now=oauth.time.time()
    monkeypatch.setattr(oauth.time,"time",lambda:now+601)
    monkeypatch.setattr(oauth,"_request",lambda *a,**kw:pytest.fail("An invalid callback must not reach Feishu"))
    with pytest.raises(oauth.OwnerOAuthError):
        oauth.finish(cfg,query["state"][0],"browser",uri,"code")
    query=parse_qs(urlsplit(oauth.start(cfg,"doc",uri,"browser")).query)
    oauth.disconnect(cfg)
    with pytest.raises(oauth.OwnerOAuthError):
        oauth.finish(cfg,query["state"][0],"browser",uri,"code")


def test_permanent_refresh_rejection_removes_saved_grant(tmp_path,monkeypatch):
    from app.feishu import owner_oauth as oauth
    cfg={"_access_config_path":str(tmp_path/"feishu.json"),"steward":{"app_id":"cli_test","open_id":"ou_me","secret_env":"DEMO_SECRET"}}
    monkeypatch.setenv("DEMO_SECRET","demo")
    with oauth._db(cfg,lock=True) as (db,key):
        oauth._save(db,key,{"access_token":"test","refresh_token":"test","expires_in":1,"refresh_token_expires_in":1000,
                    "scope":" ".join(oauth.ACTIONS["doc"])},oauth.ALL_SCOPES)
    def expired(*args,**kwargs):
        raise oauth._GrantExpired("expired")
    monkeypatch.setattr(oauth,"_request",expired)
    with pytest.raises(oauth.OwnerOAuthError):
        oauth.user_headers(cfg,required=oauth.ACTIONS["doc"])
    assert not oauth.status(cfg)["connected"]

def test_wiki_scope_is_explicit_and_independent(tmp_path):
    from app.feishu import owner_oauth as oauth
    cfg={"_access_config_path":str(tmp_path/"feishu.json"),"owner_id":"local",
         "steward":{"app_id":"cli_test","open_id":"ou_owner"}}
    def requested(action):
        link=oauth.start(cfg,action,"http://localhost:3000/callback","browser")
        return set(parse_qs(urlsplit(link).query)["scope"][0].split())
    assert requested("wiki")=={"offline_access","wiki:node:read"}
    assert "wiki:node:read" not in requested("both")
