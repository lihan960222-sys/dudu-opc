"""Employee Feishu actions. The job bridge supplies identity, entitlements and contact."""
import hashlib
import json
import re
import time
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from . import document_access, documents, employee_draft, history, knowledge_document, workspace_access
from .owner_oauth import employee_config
from .publisher import API, checked

ARGUMENTS={
    "feishu_delete_document":set(),
    "feishu_edit_document":{"old_text","new_text","body"},
    "feishu_read_document":{"url","offset"},
    "feishu_list_sources":set(),
    "feishu_read_history":{"offset","limit"},
    "feishu_read_message":{"id","offset"},
    "feishu_publish_knowledge":{"topics"},
    "feishu_create_document":{"title","body"},
}


async def staff_name(cfg,employee):
    from app.db.session import AsyncSessionLocal
    from app.team.service import member,team_for
    async with AsyncSessionLocal() as db:
        team=await team_for(db,cfg["owner_id"])
        if not team or team.lead_agent_id!=cfg["steward"]["agent_id"]:
            raise ValueError("请核对当前管家和团队。")
        agent=await member(db,cfg["owner_id"],employee,team_id=team.id)
        return agent.name


def create_document(cfg,employee,identity,db,client,invocation_id,title,body):
    from . import knowledge_revision
    if not isinstance(title,str) or not 1<=len(title.strip())<=200 or not isinstance(body,str):
        raise ValueError("请提供文档标题（最多 200 字）及正文。")
    title=title.strip()
    children=documents.blocks(body)
    destination,_=knowledge_document.target(cfg,employee)
    def headers():
        if knowledge_document.target(cfg,employee)[0]!=destination:
            raise ValueError("目标文件夹已变更，请重新派工。")
        return documents.user_headers(identity,required=documents.SCOPES | {document_access.FOLDER_SCOPE})
    auth=headers()
    fingerprint=hashlib.sha256(json.dumps([title,body,destination],sort_keys=True).encode()).hexdigest()
    key=(identity["steward"]["app_id"],employee,invocation_id)
    db.execute("""create table if not exists employee_document_jobs(
        app text,employee integer,job text,fingerprint text,doc text,confirmed integer default 0,
        primary key(app,employee,job))""")
    with db:
        reserved=db.execute("insert or ignore into employee_document_jobs(app,employee,job,fingerprint) values(?,?,?,?)",
                            (*key,fingerprint)).rowcount
    if reserved:
        response=checked(client.post(API+"/docx/v1/documents",headers=auth,
                             json={"folder_token":destination["folder_token"],"title":title},timeout=15))
        doc=response["data"]["document"]["document_id"]
        if not isinstance(doc,str) or not documents.TOKEN.fullmatch(doc):
            raise ValueError("飞书未返回有效文档编号。")
        with db:
            db.execute("update employee_document_jobs set doc=? where app=? and employee=? and job=?",(doc,*key))
        confirmed=False
    else:
        prior=db.execute("select fingerprint,doc,confirmed from employee_document_jobs where app=? and employee=? and job=?",key).fetchone()
        if prior[0]!=fingerprint:
            raise ValueError("这个派工已经提交另一份文档，请为新文档单独派工。")
        if not prior[1]:
            raise ValueError("前一次建档结果待核对，请检查目标文件夹。")
        doc,confirmed=prior[1],bool(prior[2])
    document_access.verify_destination(client,headers,destination["folder_token"],doc,title)
    actual=knowledge_revision.root_blocks(client,headers,doc)
    if not knowledge_revision.matches(actual,children):
        if actual or confirmed:
            raise ValueError("云端正文与原稿不符，已保留当前内容。")
        reply=checked(client.post(API+f"/docx/v1/documents/{doc}/blocks/{doc}/children",headers=headers(),
                params={"client_token":str(uuid5(NAMESPACE_URL,repr(key)+doc))},json={"index":0,"children":children},timeout=15))
        blocks=reply["data"]["children"]
        if len(blocks)!=len(children) or any(not block.get("block_id") for block in blocks):
            raise ValueError("飞书未确认正文写入。")
        if not knowledge_revision.matches(knowledge_revision.root_blocks(client,headers,doc),children):
            raise ValueError("飞书读回的正文与原稿不符。")
        document_access.verify_destination(client,headers,destination["folder_token"],doc,title)
    with db:
        db.execute("update employee_document_jobs set confirmed=1 where app=? and employee=? and job=?",key)
    return {"title":title,"url":destination["tenant_origin"]+"/docx/"+doc,"confirmed":True}


def pinned_read_scope(proof,db,staff,contact,source):
    command=proof.get("command")
    review=history.REVIEW.fullmatch(history.canonical(command)) if isinstance(command,str) else None
    if not review or review[1]!=staff or review[3]!=contact:
        raise ValueError("无法核对原派工指定的员工、联系人和时间范围。")
    received=db.execute("select received from delivery_timing where app=? and event=?",
                        (proof.get("app"),proof.get("event"))).fetchone()
    if not received or not isinstance(received[0],(int,float)) or received[0]<=0:
        raise ValueError("无法核对派工接收时间。")
    cutoff=int(received[0])
    if not review[4] and review[5].strip(" 。！？!?") in ("","全部","所有"):
        return cutoff,{}
    parsed,error=history.parse(command,{contact:source})
    if not parsed or error:
        raise ValueError("无法识别原派工的范围，请重新指定时间或条数。")
    _,_,hours,count,keyword=parsed
    filters={}
    if review[4]:
        filters["start"]=cutoff-hours*3600
    elif re.search(r"最近\s*\d+\s*(?:天|小时)",review[5]):
        filters["start"]=cutoff-hours*3600
    if re.search(r"\d+\s*条",review[5].split("关键词",1)[0]):
        filters["count"]=count
    if keyword:
        filters["keyword"]=keyword
    if not filters:
        raise ValueError("原派工的读取范围无效。")
    return cutoff,filters


async def execute(cfg,employee_id,name,arguments,*,owner_id,journal_db,client,allowed_actions,contact=None,invocation_id,assignment=None):
    if (type(employee_id)!=int or employee_id<=0 or owner_id!=cfg.get("owner_id") or
            employee_id==cfg["steward"].get("agent_id") or not invocation_id or
            name not in ARGUMENTS or name not in allowed_actions):
        raise ValueError("此派工没有这项飞书权限。")
    if not isinstance(arguments,dict) or arguments.keys()-ARGUMENTS[name]:
        raise ValueError("不支持这些参数。")
    staff=await staff_name(cfg,employee_id)
    identity=employee_config(cfg,employee_id)
    if not identity.get("_employee_oauth"):
        raise ValueError("员工飞书应用尚未绑定。")
    retry_scope=(assignment or {}).get("_retry_resource")
    if retry_scope and name not in {"feishu_list_sources","feishu_read_history","feishu_read_message",
                                    "feishu_read_document","feishu_publish_knowledge"}:
        raise ValueError("此续办只准读取原来源和更新原知识库。")
    if name=="feishu_delete_document":
        from .document_delete import delete_owned
        return delete_owned(cfg, employee_id, identity, journal_db, client, assignment)
    if name=="feishu_create_document":
        return create_document(cfg,employee_id,identity,journal_db,client,invocation_id,
                               arguments.get("title"),arguments.get("body"))
    if name=="feishu_edit_document":
        if not assignment or assignment.get("action")!="edit_document":
            raise ValueError("此派工未指定编辑目标。")
        body=assignment.get("body") or ""
        if arguments:
            if body:
                raise ValueError("原派工已提供正文，本次使用原稿。")
            if set(arguments)=={"old_text","new_text"} and all(isinstance(v,str) and v.strip() and "\n" not in v for v in arguments.values()):
                body="原文："+arguments["old_text"]+"\n改为："+arguments["new_text"]
            elif set(arguments)=={"body"} and isinstance(arguments["body"],str):
                body=arguments["body"]
            else:
                raise ValueError("请提供原段落和新段落，或提供需追加的正文。")
        return document_access.append(cfg,employee_id,identity,journal_db,client,invocation_id,
                                      assignment.get("target_url",""),body)
    if name=="feishu_read_document" and assignment and assignment.get("action") in ("read_document","edit_document","delete_document"):
        target=assignment.get("target_url","")
        if not target or arguments.get("url",target)!=target:
            raise ValueError("文档链接与原派工不符。")
        return await document_access.read_page(client,identity,journal_db,employee_id,invocation_id,target,arguments.get("offset",0))
    def scoped():
        policy=workspace_access.settings_for(cfg)
        sources={alias:src for alias,src in policy["history_sources"].items()
                 if src.get("agent_id")==employee_id and src.get("oauth_app")==identity["steward"]["app_id"]
                 and alias==contact}
        return policy,sources
    policy,sources=scoped()
    def verify_retry_source(bound):
        if retry_scope:
            from .unpublished_retry import verify_outbound
            dest,title=knowledge_document.target(cfg,employee_id,contact)
            verify_outbound(assignment,cfg,employee_id,contact,bound.get(contact),dest,title,identity)
    verify_retry_source(sources)
    def known_document():
        dest,title=knowledge_document.target(cfg,employee_id,contact)
        key=knowledge_document.binding_key(cfg["steward"]["app_id"],employee_id,contact,sources[contact],dest,title)
        exists=journal_db.execute("select 1 from sqlite_master where type='table' and name='knowledge_documents'").fetchone()
        row=journal_db.execute("select document from knowledge_documents where binding=?",(key,)).fetchone() if exists else None
        return dest,title,row[0] if row else None
    if name=="feishu_list_sources":
        result={"contacts":list(sources)}
        if contact in sources and ("feishu_publish_knowledge" in allowed_actions or "feishu_read_document" in allowed_actions):
            from .owner_oauth import status
            try:
                dest,title,doc=known_document()
            except (OSError,KeyError,ValueError):
                return {**result,"knowledge_target":{"sync_available":False,"reason":"知识库目标未配置"}}
            grant=status(identity)
            sufficient=(history.SCOPES|documents.SCOPES).issubset(grant["scopes"])
            profile=await history.reviewer(cfg,sources[contact],staff)
            target={"title":title,"url":None,"sync_available":bool("feishu_publish_knowledge" in allowed_actions
                    and grant["connected"] and sufficient and workspace_access.permits_model(policy, profile, assignment))}
            if doc:
                metadata=knowledge_document.inspect_document(client,
                    lambda:documents.user_headers(identity,required={document_access.READ_SCOPE}),doc)
                target["remote_state"]="present" if metadata else "missing"
                if metadata:
                    target["url"]=dest["tenant_origin"]+"/docx/"+doc
                    target["remote_title"]=metadata.get("title")
            result["knowledge_target"]=target
        return result
    if not contact or contact not in sources:
        raise ValueError("此派工未绑定该员工获准读取的联系人。")
    source=sources[contact]
    profile=await history.reviewer(cfg,source,staff)
    if profile[0]!=employee_id or not workspace_access.permits_model(policy, profile, assignment):
        raise ValueError("请在该员工的飞书连接中，允许本轮使用的模型供应商处理指定联系人的私聊。")
    source_key=(identity["steward"]["app_id"],employee_id,invocation_id,source["chat_id"])
    weekly = (assignment or {}).get("weekly")
    def prior_weekly_plan():
        from . import weekly_knowledge, owner_oauth
        return weekly_knowledge.baseline(journal_db, client,
            lambda:documents.user_headers(identity,required=documents.SCOPES | owner_oauth.ACTIONS["folder"]), assignment)
    if name=="feishu_read_document":
        document_access.indexed(journal_db)
        url=arguments.get("url")
        if weekly:
            if url != weekly["destination"]["tenant_origin"] + "/docx/" + weekly["document"]:
                raise ValueError("周计划只能读取原知识库文档。")
            prior_weekly_plan()
        indexed_url=isinstance(url,str) and journal_db.execute(
                "select 1 from employee_source_links where app=? and employee=? and job=? and chat=? and url=?",
                (*source_key,url)).fetchone()
        if not indexed_url:
            dest,_,doc=known_document()
            if not doc or url!=dest["tenant_origin"]+"/docx/"+doc:
                raise ValueError("只能读取此员工的知识库或本次聊天中已核对的链接。")
        return await document_access.read_page(client,identity,journal_db,employee_id,invocation_id,url,arguments.get("offset",0))
    if name=="feishu_publish_knowledge":
        worker=SimpleNamespace(cfg=cfg,db=journal_db,pub=SimpleNamespace(http=client))
        instructions=""
        if assignment and assignment.get("action")=="restructure":
            instructions=(assignment.get("original") or "").strip()
            if not instructions:
                raise ValueError("重整派工缺少原始整理要求，无法核对交付。")
        if not isinstance(arguments.get("topics"), list):
            raise ValueError("请先读完聊天，再提交你写的分类、摘要和消息编号。")
        cursor = journal_db.execute("select 1 from sqlite_master where type='table' and name='employee_history_reads'").fetchone()
        read = journal_db.execute("""select cutoff from employee_history_reads where app=? and employee=? and job=? and chat=?""",
                                  source_key).fetchone() if cursor else None
        if not read:
            raise ValueError("本次任务还没有读取聊天记录，请从第一页开始。")
        def draft(records):
            from .knowledge_revision import employee_blocks
            employee_draft.require_read(journal_db,source_key,records)
            return employee_blocks(arguments["topics"],records,source["chat_id"])
        result,count=await knowledge_document.publish(worker,{"app":cfg["steward"]["app_id"],"event":invocation_id},
            [None,staff,None,contact],source,read[0],revise=True,instructions=instructions,
            employee_draft=draft, assignment=assignment)
        url=re.search(r"https://[a-z0-9-]+\.feishu\.cn/docx/[a-zA-Z0-9]+",result)
        return {"message":result,"messages_read":count,"url":url[0] if url else None,"confirmed":bool(url)}
    pinned_cutoff,filters=None,{}
    if weekly:
        pinned_cutoff = assignment["cutoff"]
        if assignment.get("since") is not None:
            filters = {"start":assignment["since"]}
    if assignment and assignment.get("action")=="read" and assignment.get("event_key"):
        pinned_cutoff,filters=pinned_read_scope(assignment,journal_db,staff,contact,source)
    offset,limit=arguments.get("offset",0),arguments.get("limit",10)
    if type(offset)!=int or offset<0 or type(limit)!=int or not 1<=limit<=10:
        raise ValueError("起始位置须为非负整数，每页可取 1 至 10 条。")
    def headers():
        current,bound=scoped()
        verify_retry_source(bound)
        if bound.get(contact)!=source or not workspace_access.permits_model(current, profile, assignment):
            raise ValueError("该员工的飞书读取授权已变更。")
        return history.headers(identity,history.SCOPES)
    if history.peer_for(client,headers(),source["chat_id"],identity["steward"]["open_id"])!=source["peer_open_id"]:
        raise ValueError("飞书会话身份已变更。")
    binding=(identity["steward"]["app_id"],employee_id,invocation_id,source["chat_id"])
    journal_db.execute("""create table if not exists employee_history_reads(app text,employee integer,
        job text,chat text,cutoff integer,frontier integer default 0,primary key(app,employee,job,chat))""")
    with journal_db:
        journal_db.execute("""insert or ignore into employee_history_reads(app,employee,job,chat,cutoff)
            values(?,?,?,?,?)""",(*binding,pinned_cutoff or int(time.time())))
    cutoff,frontier=journal_db.execute("""select cutoff,frontier from employee_history_reads
        where app=? and employee=? and job=? and chat=?""",binding).fetchone()
    if name != "feishu_read_message" and offset>frontier:
        raise ValueError(f"请从第 {frontier} 个位置继续读取。")
    records=knowledge_document.collect(client,headers,source,pinned_cutoff or cutoff,**filters)
    headers()
    if weekly:
        from .weekly_knowledge import source_ids
        seen = source_ids(prior_weekly_plan(), source["chat_id"])
        records = {mid:row for mid,row in records.items() if mid not in seen}
    if name == "feishu_read_message":
        mid = arguments.get("id")
        if not isinstance(mid, str) or mid not in records:
            raise ValueError("只能读取本次来源里的消息。")
        piece=employee_draft.part(journal_db,source_key,mid,records[mid],offset,indexed_only=True)
        return {**piece,"source_complete":frontier>=len(records) and employee_draft.complete(journal_db,source_key,records),
                "total":len(records)}
    completed=min(len(records),offset+limit)
    with journal_db:
        journal_db.execute("""update employee_history_reads set frontier=max(frontier,?)
            where app=? and employee=? and job=? and chat=?""",(completed,*binding))
    page=list(records.items())[offset:offset+limit]
    document_access.indexed(journal_db)
    with journal_db:
        journal_db.executemany("insert or ignore into employee_source_links values(?,?,?,?,?)",
            [(*source_key,url) for _,item in page for url in document_access.message_urls(item)])
    excerpts=[{**employee_draft.part(journal_db,source_key,mid,item),"created":item["create_time"],
               "type":item["msg_type"],"urls":document_access.message_urls(item)} for mid,item in page]
    return {"contact":contact,"total":len(records),"complete":completed==len(records),
            "source_complete":completed==len(records) and employee_draft.complete(journal_db,source_key,records),
            "next_offset":completed if completed<len(records) else None,"excerpts":excerpts}
