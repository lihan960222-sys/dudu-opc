"""Resumable initial import of the explicitly configured employee knowledge source."""
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx

from . import history, workspace_access
from .publisher import API, checked


def collect(path, employee):
    cfg = workspace_access.load_config(path)
    folder = path.parent / "employee-settings" / str(employee)
    task = json.loads((folder / "knowledge-task.json").read_text("utf-8"))
    alias = task["contact"]
    source = workspace_access.settings_for(cfg)["history_sources"][alias]
    if source["agent_id"] != employee or task["initial_range"] != "all_available":
        raise ValueError("请核对员工和首次整理范围。")
    profile = history.source_config(cfg, source)
    chat = source["chat_id"]
    with httpx.Client(timeout=20) as client, closing(sqlite3.connect(folder / "history.sqlite3")) as db:
        auth = history.headers(profile, history.SCOPES)
        if history.peer_for(client, auth, chat, profile["steward"]["open_id"]) != source["peer_open_id"]:
            raise ValueError("会话身份不匹配")
        db.executescript("""create table if not exists imports(chat text primary key, cutoff integer, cursor text, done integer);
            create table if not exists messages(chat text,id text,created text,kind text,body text,primary key(chat,id));""")
        with db:
            db.execute("insert or ignore into imports values(?,?,null,0)",(chat,int(time.time())))
        cutoff,cursor,done = db.execute("select cutoff,cursor,done from imports where chat=?",(chat,)).fetchone()
        while not done:
            if workspace_access.settings_for(cfg)["history_sources"].get(alias) != source:
                raise ValueError("联系人绑定已变更")
            params = dict(container_id_type="chat",container_id=chat,end_time=cutoff,page_size=50,sort_type="ByCreateTimeAsc")
            if cursor:
                params["page_token"] = cursor
            page = checked(client.get(API+"/im/v1/messages",headers=history.headers(profile,history.SCOPES),params=params))["data"]
            done = not page["has_more"]
            following = page.get("page_token")
            if not done and (not following or following == cursor):
                raise ValueError("飞书未返回下一页，已保留进度")
            with db:
                for item in page["items"]:
                    if item.get("chat_id")==chat and not item.get("deleted"):
                        db.execute("insert or ignore into messages values(?,?,?,?,?)",(chat,item["message_id"],
                                   item["create_time"],item["msg_type"],json.dumps(item.get("body"),ensure_ascii=False)))
                db.execute("update imports set cursor=?,done=? where chat=?",(following,done,chat))
            cursor = following
        print("archived",db.execute("select count(*) from messages where chat=?",(chat,)).fetchone()[0])


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("employee", type=int)
    collect(Path(os.environ["FEISHU_CONFIG"]), parser.parse_args().employee)
