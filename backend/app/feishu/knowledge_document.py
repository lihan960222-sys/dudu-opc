"""Publish all accessible messages from one verified employee contact to its configured knowledge document."""
import httpx
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid5, NAMESPACE_URL

from . import documents, history, workspace_access
from .publisher import API, PlatformError, checked


def target(cfg, employee, alias=None):
    path = Path(cfg["_access_config_path"]).parent / "employee-settings" / str(employee) / "knowledge-task.json"
    if not path.is_file():
        raise ValueError("员工还没有配置知识库任务。")
    task = json.loads(path.read_text("utf-8-sig"))
    if alias and (task["contact"] != alias or task["initial_range"] != "all_available"):
        raise ValueError("请核对知识库的联系人和整理范围。")
    url = urlsplit(task["folder_url"])
    folder = re.fullmatch(r"/drive/folder/([A-Za-z0-9]+)/?", url.path)
    if not folder:
        raise ValueError("知识库文件夹链接无效。")
    dest = workspace_access.validate_document_target(dict(folder_token=folder[1], tenant_origin=f"{url.scheme}://{url.netloc}"))
    return dest, task["document_name"]


def collect(client, auth, source, cutoff, *, start=None, count=None, keyword=None):
    chat = source["chat_id"]
    params = dict(container_id_type="chat", container_id=chat, page_size=50, end_time=str(cutoff), sort_type="ByCreateTimeAsc")
    if start is not None:
        params["start_time"]=str(start)
    if count is not None:
        params.update(sort_type="ByCreateTimeDesc",page_size=min(count,50))
    records = {}
    tokens = set()
    while True:
        data = checked(client.get(API+"/im/v1/messages", params=params, headers=auth(), timeout=15))["data"]
        for item in data["items"]:
            if item.get("chat_id") == chat and not item.get("deleted"):
                if start is not None or count is not None:
                    stamp=int(item["create_time"])//1000
                    if stamp>cutoff or start is not None and stamp<start:
                        continue
                if keyword and keyword.casefold() not in history.excerpt(item).casefold():
                    continue
                records[item["message_id"]] = item
                if count is not None and len(records)>=count:
                    return records
        if not data["has_more"]:
            return records
        token = data.get("page_token")
        if not token or token in tokens:
            raise ValueError("飞书分页中断，请稍后重试。")
        params["page_token"] = token
        tokens.add(token)


def entry(item, chat):
    value = history.excerpt(item)
    kind = item.get("msg_type")
    if kind not in ("text", "post"):
        value = "附件或其他消息（"+str(kind)+"），可打开原消息查看"
    category = ("资料与链接" if kind not in ("text","post") or "http" in value else
                "问题" if any(c in value for c in "？?") else
                "安排与决定" if any(w in value for w in ("计划","需要","决定","确认")) else "交流记录")
    day = datetime.fromtimestamp(int(item["create_time"])/1000).strftime("%Y-%m-%d")
    link = "https://applink.feishu.cn/client/chat/open?openChatId="+quote(chat,safe="")+"&messageId="+quote(item["message_id"],safe="")
    return {"block_type":2,"text":{"elements":[{"text_run":{"content":f"{category} · {day}　{value}　"}},
              {"text_run":{"content":"原消息","text_element_style":{"link":{"url":link}}}}]}}


def binding_key(app,employee,alias,source,dest,title):
    return hashlib.sha256(json.dumps([app,employee,alias,source["chat_id"],source["peer_open_id"],dest,title],
                                     sort_keys=True).encode()).hexdigest()


def inspect_document(client,auth,doc):
    if not isinstance(doc,str) or not documents.TOKEN.fullmatch(doc):
        raise ValueError("无法核对已保存的文档编号。")
    response=client.get(API+f"/docx/v1/documents/{doc}",headers=auth(),timeout=15)
    try:
        body=response.json()
    except ValueError:
        body={}
    code=body.get("code") if isinstance(body,dict) else None
    if (response.status_code,code) in ((404,1770002),(400,1770003)):
        return None
    if code==99991679 or response.status_code==403:
        raise ValueError("飞书拒绝读取原知识库。请核对员工的文档读取授权和原文档访问权限。")
    meta=checked(response)["data"]["document"]
    if meta.get("document_id")!=doc:
        raise ValueError("无法确认原知识库文档的身份。")
    return meta

def resolve_document(db,client,auth,key,dest,title):
    saved=db.execute("select document from knowledge_documents where binding=?",(key,)).fetchone()
    previous=saved[0] if saved else None
    if saved and not previous:
        raise ValueError("创建文档的结果待核对，暂不重复创建。")
    if previous:
        missing=inspect_document(client,auth,previous) is None
        # Legacy rows used the employee/contact binding alone. They belong to
        # this exact old document even when Feishu confirms it was deleted.
        with db:
            for table in ("knowledge_entries","knowledge_revisions"):
                if db.execute("select 1 from sqlite_master where type='table' and name=?",(table,)).fetchone():
                    db.execute(f"update {table} set binding=? where binding=?",(key+"/"+previous,key))
        if not missing:
            return previous
    headers=auth()
    with db:
        if previous:
            claimed=db.execute("update knowledge_documents set document=null where binding=? and document=?",(key,previous))
        else:
            claimed=db.execute("insert or ignore into knowledge_documents values(?,null)",(key,))
        if not claimed.rowcount:
            raise ValueError("另一项任务正在核对知识库文档，请稍后重试。")
    try:
        response=client.post(API+"/docx/v1/documents",headers=headers,
            json={"folder_token":dest["folder_token"],"title":title},timeout=15)
        created=checked(response)["data"]["document"]["document_id"]
    except httpx.HTTPStatusError as exc:
        # Explicit request rejection can be retried; ambiguous outcomes keep
        # the durable claim to prevent creating a second cloud document.
        if exc.response.status_code in (400,401,403,404,422,429):
            with db:
                if previous:
                    db.execute("update knowledge_documents set document=? where binding=? and document is null",(previous,key))
                else:
                    db.execute("delete from knowledge_documents where binding=? and document is null",(key,))
        raise
    if not documents.TOKEN.fullmatch(created):
        raise ValueError("云盘返回的文档编号无效。")
    with db:
        db.execute("update knowledge_documents set document=? where binding=? and document is null",(created,key))
    return created


async def publish(worker,row,review,source,cutoff,*,read_only=False,revise=False,instructions="",
                  employee_draft=None,assignment=None):
    def stage(name, count=None):
        if "event" not in row.keys():
            return
        with worker.db:
            worker.db.execute("""create table if not exists knowledge_steps(
                app text,event text,stage text,count integer,at integer,
                primary key(app,event,stage))""")
            worker.db.execute("insert or replace into knowledge_steps values(?,?,?,?,unixepoch())",
                              (row["app"],row["event"],name,count))

    stage("resolve_employee")
    staff, alias = review[1], review[3]
    profile = await history.reviewer(worker.cfg,source,staff)
    employee = profile[0]
    dest,title = target(worker.cfg, employee, alias)
    if not source.get("oauth_app"):
        raise ValueError("请在该员工的飞书能力中重新核对联系人绑定。")
    cfg = history.source_config(worker.cfg,source)
    if not cfg.get("_employee_oauth") or cfg["steward"]["app_id"] != source["oauth_app"]:
        raise ValueError("当前飞书授权与员工身份不匹配。")
    def auth(scopes):
        if (workspace_access.settings_for(worker.cfg)["history_sources"].get(alias)!=source or
                target(worker.cfg,employee,alias)!=(dest,title)):
            raise ValueError("联系人或目标文件夹已变更，请重新发起整理。")
        return history.headers(cfg,scopes)
    client = worker.pub.http
    if history.peer_for(client,auth(history.SCOPES),source["chat_id"],cfg["steward"]["open_id"])!=source["peer_open_id"]:
        raise ValueError("会话身份已变更。")
    stage("read_history")
    weekly = (assignment or {}).get("weekly")
    prior = None
    if weekly:
        from . import weekly_knowledge, owner_oauth
        if cutoff != assignment["cutoff"] or not await assignment["_weekly_authorized"]():
            raise ValueError("周计划的授权或本次读取截止时间已变更。")
        prior = weekly_knowledge.baseline(worker.db, client,
            lambda:auth(documents.SCOPES | owner_oauth.ACTIONS["folder"]), assignment)
        records = collect(client,lambda:auth(history.SCOPES),source,cutoff,start=assignment.get("since"))
        seen = weekly_knowledge.source_ids(prior, source["chat_id"])
        records = {mid:row for mid,row in records.items() if mid not in seen}
    else:
        records=collect(client,lambda:auth(history.SCOPES),source,cutoff)
    stage("history_received",len(records))
    if read_only:
        lines=[history.excerpt(item) for item in records.values()]
        return f"{staff}查完了与{alias}的全部可见历史，共 {len(lines)} 条。"+("\n"+history.digest(lines) if lines else ""),len(lines)
    db=worker.db
    key=binding_key(row["app"],employee,alias,source,dest,title)
    db.executescript("""create table if not exists knowledge_documents(binding text primary key,document text);
        create table if not exists knowledge_entries(binding text,id text,primary key(binding,id));""")
    if not records:
        if weekly:
            return f"已核对原知识库，本周没有新增消息。\n《{title}》{dest['tenant_origin']}/docx/{weekly['document']}", 0
        return f"{staff}查完了，当前可读取的全部历史里有 0 条消息。",0
    draft_blocks = employee_draft(records) if employee_draft is not None else None
    if weekly:
        if not draft_blocks:
            raise ValueError("本周更新需要员工提交包含新消息来源的综述。")
        draft_blocks = weekly_knowledge.merged_draft(db, assignment, prior, draft_blocks)
    def verify_target(doc):
        from . import document_access, owner_oauth
        folder_auth = lambda: auth(documents.SCOPES | owner_oauth.ACTIONS["folder"])
        document_access.verify_destination(client,folder_auth,dest["folder_token"],doc,title)
    if draft_blocks is not None:
        from . import owner_oauth
        auth(documents.SCOPES | owner_oauth.ACTIONS["folder"])
    created=weekly["document"] if weekly else resolve_document(db,client,lambda:auth(documents.SCOPES),key,dest,title)
    if draft_blocks is not None:
        verify_target(created)
    layout_key=key
    key=key+"/"+created
    has_revision_log=db.execute("select 1 from sqlite_master where type='table' and name='knowledge_revisions'").fetchone()
    if revise or (has_revision_log and db.execute(
            "select 1 from knowledge_revisions where binding=? or binding like ? limit 1",(key,layout_key+"/%")).fetchone()):
        from .knowledge_revision import revise_document
        async def authorized():
            if weekly and not await assignment["_weekly_authorized"]():
                return False
            settings=workspace_access.settings_for(worker.cfg)
            if not (workspace_access.permits_model(settings,profile,assignment) if draft_blocks is not None
                    else settings.get("allow_codex_summary") is True):
                return False
            auth(history.SCOPES)
            return await history.reviewer(worker.cfg,source,staff)==profile
        opts={"employee_draft":draft_blocks} if draft_blocks is not None else {}
        result=await revise_document(db,client,lambda:auth(documents.SCOPES),authorized,profile,key,created,
            dest["tenant_origin"],title,source["chat_id"],records,stage=stage,instructions=instructions,layout_key=layout_key,**opts)
        if draft_blocks is not None:
            verify_target(created)
        stage("revision_confirmed",result[1])
        return result
    known={r[0] for r in db.execute("select id from knowledge_entries where binding=?",(key,))}
    # A stable request token per message also handles an interrupted acknowledgement.
    for mid,item in records.items():
        if mid in known:
            continue
        data=checked(client.post(API+f"/docx/v1/documents/{created}/blocks/{created}/children",
            params={"client_token":str(uuid5(NAMESPACE_URL,key+"/"+mid))}, headers=auth(documents.SCOPES),
            json={"children":[entry(item,source["chat_id"])],"index":-1},timeout=15))["data"]
        if len(data["children"])!=1:
            raise ValueError("云盘未确认正文写入。")
        with db:
            db.execute("insert or ignore into knowledge_entries values(?,?)",(key,mid))
    from .knowledge_revision import block_signature, root_blocks
    remote=root_blocks(client,lambda:auth(documents.SCOPES),created)
    signatures={json.dumps(block_signature(block)) for block in remote}
    if any(json.dumps(block_signature(entry(item,source["chat_id"]))) not in signatures for item in records.values()):
        raise ValueError("飞书正文与原消息核对不一致，尚未完成。")
    return f"二狗查完了与{alias}的全部可见历史，共 {len(records)} 条。这次新增整理 {len(set(records)-known)} 条。\n《{title}》{dest['tenant_origin']}/docx/{created}",len(records)
