"""Owner-selected Feishu resources. The gateway re-reads these on each explicit request."""
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path


class SettingsChanged(ValueError):
    pass


def config_from_env():
    filename = os.getenv("FEISHU_CONFIG")
    if not filename:
        raise ValueError("这台主机还没有指定飞书配置文件。")
    return load_config(Path(filename))


def load_config(path):
    try:
        path = Path(path).resolve(strict=True)
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        if cfg["owner_id"] != "local-owner" or not all(cfg["steward"][key] for key in ("tenant_key","app_id","open_id")):
            raise ValueError
    except (ValueError, KeyError, OSError, TypeError):
        raise ValueError("请先在本机完成 Dudu 飞书应用和老板身份的绑定。") from None
    return {**cfg, "_access_config_path": str(path)}


def _sources(value, owner_open_id):
    if not isinstance(value, dict) or len(value) > 100:
        raise ValueError("每次最多可登记 100 位联系人。")
    chats, peers = set(), set()
    for alias, contact in value.items():
        if not re.fullmatch(r"[\w·.\-]{1,40}", alias) or alias in {"列表","来源","有哪些"}:
            raise ValueError("联系人简称限 1 至 40 个字，可用文字、数字、点和短横线。")
        if not isinstance(contact, dict) or set(contact) != {"chat_id", "peer_open_id", "agent_id"}:
            raise ValueError("联系人需要经过核对的单聊和对方身份。")
        chat, peer, agent = contact["chat_id"], contact["peer_open_id"], contact["agent_id"]
        if (not isinstance(chat, str) or not re.fullmatch(r"oc_[a-zA-Z0-9]{1,100}", chat) or
                not isinstance(peer, str) or not re.fullmatch(r"ou_[a-zA-Z0-9]{1,100}", peer) or peer == owner_open_id):
            raise ValueError("请指定你和另一位用户的单聊。")
        if agent is not None and (type(agent) is not int or agent <= 0):
            raise ValueError("请选择管家或一个有效的员工。")
        if chat in chats or peer in peers:
            raise ValueError("这位联系人已经登记。")
        chats.add(chat)
        peers.add(peer)
    return value


def validate_document_target(value):
    if value == {}:
        return {}
    if not isinstance(value, dict) or set(value) != {"folder_token","tenant_origin"}:
        raise ValueError("请选择一个飞书云盘文件夹。")
    token, origin = value["folder_token"], value["tenant_origin"]
    if not isinstance(token,str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}",token) or not isinstance(origin,str):
        raise ValueError("飞书文件夹链接无效。")
    origin = origin.lower().removesuffix("/")
    if not re.fullmatch(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.feishu\.cn",origin):
        raise ValueError("请粘贴所在飞书租户的云盘文件夹链接。")
    return {"folder_token":token, "tenant_origin":origin}


def _serialized(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",",":"))


def _binding(cfg):
    steward = cfg.get("steward", {})
    return _serialized([cfg.get("owner_id"), steward.get("tenant_key"), steward.get("app_id"), steward.get("open_id")])


def _database(cfg):
    root = Path(cfg["_access_config_path"]).parent
    return (root / cfg["state_dir"]).resolve() / "workspace-access.sqlite3"


def _effective(cfg, row=None):
    value = json.loads(row[0]) if row else cfg
    fields = {"history_sources":_sources(value.get("history_sources",{}),cfg.get("steward",{}).get("open_id")),
              "document_target":validate_document_target(value.get("document_target",{})),
              "allow_codex_summary":value.get("allow_codex_summary") is True}
    rev = hashlib.sha256(_serialized([_binding(cfg), fields]).encode()).hexdigest()
    return {**fields, "revision":rev}


def _row(db, cfg):
    return db.execute("select allowed from access_policy where binding=?", (_binding(cfg),)).fetchone()


def settings_for(cfg):
    if not cfg.get("_access_config_path"):
        return _effective(cfg)
    path = _database(cfg)
    if not path.is_file():
        return _effective(cfg)
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as db:
        if not db.execute("select 1 from sqlite_master where type='table' and name='access_policy'").fetchone():
            return _effective(cfg)
        return _effective(cfg, _row(db, cfg))


def save_settings(path, *, expected_revision, history_sources=None, document_target=None, allow_codex_summary=None):
    cfg = load_config(path)
    target = _database(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target,timeout=4)) as db, db:
        db.execute("begin immediate")
        db.execute("create table if not exists access_policy(binding text primary key, allowed text not null)")
        before = _effective(cfg,_row(db,cfg))
        if before["revision"] != expected_revision:
            raise SettingsChanged("其他设备已改过飞书设置。请重新读取后再保存。")
        if allow_codex_summary is not None and type(allow_codex_summary) is not bool:
            raise ValueError("请选择是否允许用 OpenAI Codex 整理指定的私聊。")
        settings = {"history_sources":_sources(history_sources if history_sources is not None else before["history_sources"],
                                               cfg["steward"]["open_id"]),
                    "document_target":validate_document_target(document_target if document_target is not None else before["document_target"]),
                    "allow_codex_summary":before["allow_codex_summary"] if allow_codex_summary is None else allow_codex_summary}
        payload = _serialized(settings)
        db.execute("insert into access_policy(binding,allowed) values(?,?) on conflict(binding) do update set allowed=excluded.allowed",
                   (_binding(cfg), payload))
        return _effective(cfg,(payload,))
