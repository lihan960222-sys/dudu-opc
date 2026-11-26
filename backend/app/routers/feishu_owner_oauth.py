"""Browser-only owner consent for a local steward's Feishu workspace."""
import hashlib
from html import escape
import ipaddress
import json
import secrets
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from app.auth.middleware import principal_of
from app.core import public_url
from app.core.config import settings
from app.feishu import owner_oauth
from app.feishu.workspace_access import config_from_env

router = APIRouter(prefix="/feishu/owner-oauth", tags=["feishu"])
COOKIE = "steward_feishu_link"
COOKIE_PATH = "/api/v1/feishu/owner-oauth"
CALLBACK = COOKIE_PATH + "/callback"


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["history","doc","both","delete","folder","wiki"]


def owner_browser(request: Request):
    principal = principal_of(request.scope)
    if (not principal or principal.kind != "browser" or not principal.is_admin
            or not isinstance(principal.session_id,int) or principal.session_id <= 0
            or principal.tenant_id is not None
            or (principal.role != "owner" and not (principal.role is None and principal.person_id is None))):
        raise HTTPException(403, "只有本机工作区的老板可以管理此项授权。")
    return principal


def _consent_owner(request: Request):
    principal = owner_browser(request)
    if principal.role != "owner" or principal.person_id is None:
        raise HTTPException(403, "请先用本机工作区的老板账号登录，再管理飞书授权。")
    return principal


def _origin(value):
    parts = urlsplit(value)
    return public_url.normalize(f"{parts.scheme}://{parts.netloc}")


def _allowed(origin):
    parts = urlsplit(origin)
    host = parts.hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if host == "localhost" or (address and address.is_loopback):
        return True
    configured = settings.SUPARAGENT_PUBLIC_URL
    if not configured or origin != public_url.normalize(configured) or parts.scheme != "https":
        return False
    if address:
        return (address.is_private and not address.is_link_local and not address.is_unspecified
                and not address.is_multicast) or address in ipaddress.ip_network("100.64.0.0/10")
    return host.endswith((".ts.net",".lan",".internal",".local"))


def redirect_uri(request, *, callback=False):
    # A browser reaching Next.js may be proxied to a different loopback port.
    # Accept origins only from the server-side allowlist on every leg of the exchange.
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    forwarded = request.headers.get("x-forwarded-host")
    try:
        if origin and not callback:
            origin = public_url.normalize(origin)
        elif referer and not callback:
            origin = _origin(referer)
        elif forwarded:
            scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
            origin = public_url.normalize(f"{scheme}://{forwarded}")
        else:
            origin = _origin(str(request.url))
        if not origin or not _allowed(origin):
            raise ValueError
    except ValueError:
        raise HTTPException(400,"请从本机管理页面或已配置的私有 HTTPS 地址发起飞书授权。") from None
    return origin + CALLBACK


def _binding(principal, nonce):
    if not nonce or len(nonce)>160:
        return ""
    browser = (principal.kind,principal.session_id,principal.person_id,principal.role,nonce)
    return hashlib.sha256(json.dumps(browser).encode()).hexdigest()


def _employee_prefix(value):
    prefix, sep, _ = value.partition(".")
    return int(prefix) if sep and prefix.isascii() and prefix.isdigit() and len(prefix) < 16 else None


def _cookie_name(employee):
    return COOKIE if employee is None else f"{COOKIE}_{employee}"


def _config():
    try:
        return config_from_env()
    except (ValueError,OSError):
        raise HTTPException(409,"请先在本机配置嘟嘟的飞书应用和老板身份。") from None


HEADERS = {"Cache-Control":"no-store","Referrer-Policy":"no-referrer"}

def employee_config(employee):
    try:
        return owner_oauth.employee_config(_config(), employee)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from None


@router.get("/status")
def connection_status(request: Request, employee: int | None = None):
    _consent_owner(request)
    uri = redirect_uri(request)
    cfg=employee_config(employee)
    current=owner_oauth.status(cfg)
    return JSONResponse({**current,"redirect_uri":uri,
        "consent":owner_oauth.consent_progress(cfg,current["scopes"]),
        "permissions_url":"https://open.feishu.cn/app/"+cfg["steward"]["app_id"]+"/auth"},headers=HEADERS)


@router.post("/start")
def begin(payload: StartRequest, request: Request, employee: int | None = None):
    principal = _consent_owner(request)
    uri = redirect_uri(request)
    nonce = secrets.token_urlsafe(32)
    try:
        url = owner_oauth.start(employee_config(employee),payload.action,uri,_binding(principal,nonce),employee=employee)
    except owner_oauth.OwnerOAuthError as exc:
        raise HTTPException(409,str(exc)) from None
    response = JSONResponse({"authorize_url":url,"redirect_uri":uri},headers=HEADERS)
    response.set_cookie(_cookie_name(employee),nonce,max_age=600,path=COOKIE_PATH,httponly=True,samesite="lax",secure=uri.startswith("https://"))
    return response


@router.get("/callback")
def callback(request: Request, state: str = "", code: str = "", error: str = ""):
    principal = _consent_owner(request)
    uri = redirect_uri(request,callback=True)
    employee = _employee_prefix(state)
    cookie = _cookie_name(employee)
    nonce = request.cookies.get(cookie) or ""
    if employee is None:
        # An authorization begun before employee-specific cookies has an unprefixed state.
        employee = _employee_prefix(nonce)
    destination = f"/?section=people&capability=feishu&employee={employee}" if employee is not None else "/?section=host"
    try:
        owner_oauth.finish(employee_config(employee),state,_binding(principal,nonce),uri,"" if error else code)
        outcome = "connected"
    except owner_oauth.OwnerOAuthError as exc:
        if employee is not None:
            return HTMLResponse("<meta charset=utf-8><title>飞书授权</title><h2>授权未保存</h2><p>"
                                +escape(str(exc))+f"</p><a href='{escape(destination)}'>返回这名员工的飞书连接</a>",headers=HEADERS)
        outcome = "error"
    response = RedirectResponse(uri.removesuffix(CALLBACK)+destination+"&feishu_oauth="+outcome,status_code=303,headers=HEADERS)
    if outcome == "connected":
        response.delete_cookie(cookie,path=COOKIE_PATH,secure=uri.startswith("https://"),httponly=True,samesite="lax")
    return response


@router.delete("/connection")
def unlink(request: Request, employee: int | None = None):
    _consent_owner(request)
    uri = redirect_uri(request)
    cfg = employee_config(employee)
    owner_oauth.disconnect(cfg)
    response = JSONResponse({**owner_oauth.status(cfg),"redirect_uri":uri},headers=HEADERS)
    cookies = {_cookie_name(employee)} if employee is not None else set()
    if _employee_prefix(request.cookies.get(COOKIE) or "") == employee:
        cookies.add(COOKIE)
    for cookie in cookies:
        response.delete_cookie(cookie,path=COOKIE_PATH,secure=uri.startswith("https://"),httponly=True,samesite="lax")
    return response
