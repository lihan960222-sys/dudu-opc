"""Owner user grants for the explicitly selected Feishu app, stored in its private profile."""
import base64
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlencode

import httpx

from .setup_specialist import APP, dpapi

AUTHORIZE = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN = "https://accounts.feishu.cn/oauth/v3/token"
USER_INFO = "https://open.feishu.cn/open-apis/authen/v1/user_info"
ACTIONS = {
    "history": {"im:chat:read", "im:message:readonly", "im:message.p2p_msg:get_as_user"},
    "doc": {"docx:document:create", "docx:document:write_only"},
}
ALL_SCOPES = frozenset().union(*ACTIONS.values(), {"offline_access"})


class OwnerOAuthError(RuntimeError):
    """Safe to display: never includes provider responses, credentials or submitted codes."""


class _GrantExpired(OwnerOAuthError):
    pass


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _profile(cfg):
    path = Path(cfg["_access_config_path"])
    role = cfg["steward"]
    if not path.is_absolute() or not APP.fullmatch(role["app_id"]) or not role.get("open_id"):
        raise OwnerOAuthError("请先配置嘟嘟应用和老板的飞书身份。")
    identity = [cfg.get("owner_id"), role["app_id"], role["open_id"], role.get("tenant_key")]
    return path, _hash(json.dumps(identity, ensure_ascii=False))


@contextmanager
def _db(cfg, *, lock=False):
    path, key = _profile(cfg)
    with closing(sqlite3.connect(path.parent / "owner-oauth.sqlite3", timeout=25)) as db:
        db.row_factory = sqlite3.Row
        db.execute("create table if not exists flow (profile text primary key, state text, browser text, uri text, verifier text, scopes text, until real, used integer)")
        db.execute("create table if not exists grant_data (profile text primary key, envelope text, scopes text, expires real, refresh_expires real)")
        if lock:
            db.execute("begin immediate")
        with db:
            yield db, key


def _seal(data):
    from app.core.crypto import encrypt
    return encrypt(json.dumps(data))


def _open(envelope):
    from cryptography.fernet import InvalidToken
    from app.core.crypto import decrypt
    try:
        return json.loads(decrypt(envelope))
    except (InvalidToken, ValueError, TypeError):
        raise OwnerOAuthError("本机无法解锁飞书授权，请重新连接。") from None


def _app_secret(cfg):
    path, _ = _profile(cfg)
    role = cfg["steward"]
    secret = os.environ.get(role["secret_env"])
    if not secret and os.name == "nt":
        try:
            # set-feishu-secret.ps1 writes CurrentUser DPAPI as hex over UTF-16LE.
            protected = (path.parent / (role["app_id"] + ".dpapi")).read_text(encoding="utf-8-sig")
            secret = dpapi(bytes.fromhex(protected.strip()), unlock=True).decode("utf-16-le")
        except (OSError, ValueError):
            pass
    if not secret:
        raise OwnerOAuthError("本机找不到嘟嘟的应用密钥，请先在设置里保存。")
    return secret


def _request(method, url, **kwargs):
    try:
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            response = client.request(method, url, **kwargs)
            data = response.json()
            if isinstance(data,dict) and (data.get("error") == "invalid_grant"
                    or str(data.get("code","")) in {"20037","20073"}):
                raise _GrantExpired("老板的飞书授权已失效，请重新连接。")
            response.raise_for_status()
            if not isinstance(data, dict) or data.get("error") or data.get("code", 0) != 0:
                raise ValueError
            return data
    except (httpx.HTTPError, ValueError):
        raise OwnerOAuthError("飞书未能完成授权。请检查应用权限和回调地址后重新连接。") from None


def _token(cfg, **form):
    return _request("POST", TOKEN, data={"client_id":cfg["steward"]["app_id"],
                                         "client_secret":_app_secret(cfg), **form})


def start(cfg, action, uri, browser):
    if action not in ("both", *ACTIONS):
        raise OwnerOAuthError("请选择聊天记录、云文档或两项授权。")
    scopes = set(ALL_SCOPES) if action == "both" else ACTIONS[action] | {"offline_access"}
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    with _db(cfg, lock=True) as (db, key):
        existing = db.execute("select scopes from grant_data where profile=? and max(expires,refresh_expires)>?",
                              (key, time.time())).fetchone()
        if existing:
            scopes.update(ALL_SCOPES.intersection(existing["scopes"].split()))
        scope = " ".join(sorted(scopes))
        db.execute("insert or replace into flow values(?,?,?,?,?,?,?,0)",
                   (key, _hash(state), _hash(browser), uri, _seal(verifier), scope, time.time()+600))
    args = {"client_id":cfg["steward"]["app_id"], "response_type":"code", "redirect_uri":uri,
            "scope":scope, "state":state, "code_challenge":challenge, "code_challenge_method":"S256",
            "prompt":"consent"}
    if tenant := cfg["steward"].get("tenant_key"):
        args["target_tenant"] = tenant
    return AUTHORIZE + "?" + urlencode(args)


def _save(db, key, token, allowed):
    access, refresh = token.get("access_token"), token.get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise OwnerOAuthError("飞书未授予离线访问，请在应用后台启用并发布 offline_access 后重新授权。")
    try:
        duration, refresh_duration = int(token["expires_in"]), int(token["refresh_token_expires_in"])
        scopes = set(token["scope"].split()).intersection(allowed)
        if min(duration, refresh_duration) <= 0 or not (scopes - {"offline_access"}):
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise OwnerOAuthError("飞书返回的授权期限或权限范围不完整，请重新连接。") from None
    now = time.time()
    db.execute("insert or replace into grant_data values(?,?,?,?,?)", (
        key, _seal({"profile":key, "access":access, "refresh":refresh}), " ".join(sorted(scopes)), now+duration, now+refresh_duration))
    return scopes


def finish(cfg, state, browser, uri, code):
    if not isinstance(state,str) or len(state)>256 or not browser:
        raise OwnerOAuthError("授权请求已失效，请在原来的浏览器重新连接。")
    digest = _hash(state)
    with _db(cfg, lock=True) as (db, key):
        row = db.execute("select * from flow where profile=? and state=? and browser=? and uri=? and used=0 and until>?",
                         (key, digest, _hash(browser), uri, time.time())).fetchone()
        if not row:
            raise OwnerOAuthError("授权请求已失效，请在原来的浏览器重新连接。")
        db.execute("update flow set used=1 where profile=?", (key,))
    # Commit single use before exchanging the code. A disconnect or newer attempt invalidates this one.
    if not code or len(code)>2048:
        raise OwnerOAuthError("本次授权未完成。")
    token = _token(cfg, grant_type="authorization_code", code=code,
                   code_verifier=_open(row["verifier"]), redirect_uri=uri)
    access = token.get("access_token")
    if not isinstance(access,str) or not access:
        raise OwnerOAuthError("飞书未返回有效授权，请重新连接。")
    identity = _request("GET", USER_INFO, headers={"Authorization":"Bearer "+access}).get("data")
    owner = cfg["steward"]
    if (not isinstance(identity,dict) or identity.get("open_id") != owner["open_id"]
            or (identity.get("tenant_key") and owner.get("tenant_key") and identity["tenant_key"] != owner["tenant_key"])):
        raise OwnerOAuthError("这次登录的飞书账号与已配置的老板身份不一致。")
    with _db(cfg, lock=True) as (db, key):
        changed = db.execute("delete from flow where profile=? and state=? and used=1 and until>?",
                             (key, digest, time.time())).rowcount
        if changed != 1:
            raise OwnerOAuthError("授权已过期或被另一次操作替换，请重新连接。")
        requested = set(row["scopes"].split()) - {"offline_access"}
        granted = token.get("scope")
        if not isinstance(granted,str) or not requested.issubset(granted.split()):
            # An incomplete incremental grant must not displace a working earlier grant.
            raise OwnerOAuthError("飞书尚未授予本次所需的全部权限，请检查应用权限后重试。")
        _save(db, key, token, row["scopes"].split())


def status(cfg):
    with _db(cfg) as (db, key):
        row = db.execute("select scopes,expires,refresh_expires from grant_data where profile=?", (key,)).fetchone()
    active = bool(row and max(row["expires"],row["refresh_expires"]) > time.time())
    scopes = set(row["scopes"].split()) if active else set()
    return {"connected":active,"scopes":sorted(scopes),"actions":{k:v.issubset(scopes) for k,v in ACTIONS.items()}}


def disconnect(cfg):
    with _db(cfg, lock=True) as (db, key):
        db.execute("delete from grant_data where profile=?", (key,))
        db.execute("delete from flow where profile=?", (key,))


def user_headers(cfg, *, required):
    """Provide the app's verified owner credential only to a caller requesting explicitly granted scopes."""
    required = set(required)
    if not required or not required.issubset(ALL_SCOPES):
        raise OwnerOAuthError("该操作不在本机支持的飞书授权范围内。")
    with _db(cfg, lock=True) as (db, key):
        row = db.execute("select * from grant_data where profile=?", (key,)).fetchone()
        if not row or not required.issubset(row["scopes"].split()):
            raise OwnerOAuthError("请先在管理页面连接老板的飞书账号，并授权这项操作。")
        stored = _open(row["envelope"])
        if stored.get("profile") != key:
            raise OwnerOAuthError("飞书授权与本机配置不符，请重新连接。")
        if row["expires"] > time.time()+60:
            return {"Authorization":"Bearer "+stored["access"]}
        if row["refresh_expires"] <= time.time():
            raise OwnerOAuthError("老板的飞书授权已到期，请重新连接。")
        # Feishu rotates its single-use refresh token. Keep this transaction locked across the exchange.
        try:
            token = _token(cfg, grant_type="refresh_token", refresh_token=stored["refresh"])
        except _GrantExpired:
            db.execute("delete from grant_data where profile=?", (key,))
            db.commit()
            raise
        granted = _save(db, key, token, row["scopes"].split())
    if not required.issubset(granted):
        raise OwnerOAuthError("飞书当前授权范围已变化，请重新连接。")
    return {"Authorization":"Bearer "+token["access_token"]}
