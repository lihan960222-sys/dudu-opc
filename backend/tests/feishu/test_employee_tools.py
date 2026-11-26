import json
import sqlite3

import httpx
import pytest

from app.feishu import employee_tools as tools


@pytest.fixture
def run(monkeypatch):
    cfg={"owner_id":"owner","steward":{"agent_id":1,"app_id":"lead"}}
    employee={"_employee_oauth":True,"steward":{"app_id":"employee","open_id":"ou_self"}}
    source={"agent_id":4,"oauth_app":"employee","chat_id":"oc_chat","peer_open_id":"ou_peer"}
    settings={"history_sources":{"李晗":source,"其他":{**source,"agent_id":5}},"allow_codex_summary":True}
    async def name(*_): return "二狗"
    async def reviewer(*_): return (4,"二狗","codex","gpt-example","")
    monkeypatch.setattr(tools,"staff_name",name)
    monkeypatch.setattr(tools,"employee_config",lambda *_:employee)
    monkeypatch.setattr(tools.history,"reviewer",reviewer)
    monkeypatch.setattr(tools.workspace_access,"settings_for",lambda _:settings)
    monkeypatch.setattr(tools.knowledge_document,"target",lambda *_:({"tenant_origin":"https://demo.feishu.cn","folder_token":"folder"},"知识库"))
    def headers(cfg,*,required):
        assert cfg is employee
        return {}
    monkeypatch.setattr(tools.documents,"user_headers",headers)
    calls=[]
    cloud={"title":"","folder":"folder","children":[]}
    def respond(req):
        calls.append(req)
        if req.method=="POST" and req.url.path.endswith("/documents"):
            cloud["title"]=json.loads(req.content)["title"]
            return httpx.Response(200,json={"code":0,"data":{"document":{"document_id":"doc"}}})
        if req.method=="GET" and req.url.path.endswith("/drive/v1/files"):
            return httpx.Response(200,json={"code":0,"data":{"files":[{"token":"doc","type":"docx","parent_token":cloud["folder"]}]}})
        if req.method=="GET" and req.url.path.endswith("/documents/doc"):
            return httpx.Response(200,json={"code":0,"data":{"document":{"document_id":"doc","title":cloud["title"]}}})
        if req.method=="POST" and req.url.path.endswith("/children"):
            cloud["children"]=json.loads(req.content)["children"]
            if settings.pop("test_lost_ack",False):
                raise httpx.ReadTimeout("synthetic interrupted reply",request=req)
            return httpx.Response(200,json={"code":0,"data":{"children":[{"block_id":"a"} for _ in cloud["children"]]}})
        if req.method=="GET" and req.url.path.endswith("/children"):
            return httpx.Response(200,json={"code":0,"data":{"items":cloud["children"]}})
        raise AssertionError(f"Unexpected request to the test transport: {req.method} {req.url.path}")
    settings["test_cloud"]=cloud
    db=sqlite3.connect(":memory:")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        async def call(name,arguments,**changes):
            kwargs=dict(owner_id="owner",allowed_actions={name},contact="李晗",invocation_id="job-1",journal_db=db,client=client)
            return await tools.execute(cfg,4,name,arguments,**{**kwargs,**changes})
        yield call,calls,settings
    db.close()


@pytest.mark.asyncio
async def test_pinned_source_and_scope(run):
    call,calls,settings=run
    assert await call("feishu_list_sources",{})=={"contacts":["李晗"]}
    for args,opts in [({"chat_id":"oc_other"},{}),({},{"owner_id":"else"}),({},{"allowed_actions":set()}),({},{"contact":"其他"})]:
        with pytest.raises(ValueError):
            await call("feishu_read_history",args,**opts)
    settings["allow_codex_summary"]=False
    with pytest.raises(ValueError):
        await call("feishu_read_history",{})
    assert not calls


@pytest.mark.asyncio
async def test_general_doc_uses_employee_and_is_idempotent(run):
    call,calls,settings=run
    args={"title":"测试","body":"正文"}
    result=await call("feishu_create_document",args)
    assert result==await call("feishu_create_document",args)
    assert result["confirmed"] and result["url"]=="https://demo.feishu.cn/docx/doc"
    assert sum(r.method=="POST" for r in calls)==2 and json.loads(calls[0].content)["folder_token"]=="folder"
    assert any(r.url.path.endswith("/drive/v1/files") for r in calls)
    assert any(r.method=="GET" and r.url.path.endswith("/documents/doc") for r in calls)
    with pytest.raises(ValueError):
        await call("feishu_create_document",{**args,"body":"另一份"})
    settings["test_cloud"]["folder"]="elsewhere"
    with pytest.raises(ValueError,match="文件夹"):
        await call("feishu_create_document",args)
    settings["test_cloud"].update(folder="folder",title="人工改名")
    with pytest.raises(ValueError,match="名称"):
        await call("feishu_create_document",args)
    assert sum(r.method=="POST" for r in calls)==2


async def test_create_document_recovers_written_body_after_lost_reply_without_a_second_write(run):
    call,calls,settings=run
    settings["test_lost_ack"]=True
    args={"title":"摘要","body":"### 结果\n正文"}
    with pytest.raises(httpx.ReadTimeout):
        await call("feishu_create_document",args)
    assert (await call("feishu_create_document",args))["confirmed"]
    assert sum(r.method=="POST" for r in calls)==2

@pytest.mark.asyncio
async def test_employee_read_checks_contact_and_requires_every_page(run,monkeypatch):
    call,_,_=run
    monkeypatch.setattr(tools.history,"headers",lambda cfg,scopes:{"token":cfg["steward"]["app_id"]})
    monkeypatch.setattr(tools.history,"peer_for",lambda client,auth,chat,owner:"ou_peer" if auth=={"token":"employee"} else None)
    def collect(client,auth,source,cutoff):
        assert auth()=={"token":"employee"} and source["chat_id"]=="oc_chat"
        return {str(i):{"create_time":"100","msg_type":"text","body":{"content":json.dumps({"text":f"msg{i}"})}}
                for i in range(11)}
    monkeypatch.setattr(tools.knowledge_document,"collect",collect)
    with pytest.raises(ValueError):
        await call("feishu_read_history",{"offset":10})
    first=await call("feishu_read_history",{})
    assert not first["complete"] and len(first["excerpts"])==10
    last=await call("feishu_read_history",{"offset":first["next_offset"]})
    assert last["complete"] and last["total"]==11


@pytest.mark.asyncio
async def test_pinned_worker_publishes_semantic_revision_and_returns_receipt(run,monkeypatch):
    call,_,_=run
    records={"m1":{"message_position":15,"msg_type":"text","create_time":"100","body":{"content":json.dumps({"text":"甲"*2600})}}}
    monkeypatch.setattr(tools.history,"headers",lambda *_:{})
    monkeypatch.setattr(tools.history,"peer_for",lambda *_:"ou_peer")
    monkeypatch.setattr(tools.knowledge_document,"collect",lambda *_:records)
    async def publish(worker,row,review,source,cutoff,*,revise,instructions,employee_draft,assignment,verification):
        assert row["app"]=="lead" and review[1]=="二狗" and source["oauth_app"]=="employee" and revise
        assert employee_draft(records)[-1]["text"]["elements"][0]["text_run"]["content"]=="员工写的摘要"
        verification.update(url="https://demo.feishu.cn/docx/test123",parent_verified=True,body_verified=True)
        return "已写入 https://demo.feishu.cn/docx/test123",11
    monkeypatch.setattr(tools.knowledge_document,"publish",publish)
    draft={"topics":[{"title":"进展","summary":"员工写的摘要","sources":["m1"]}]}
    with pytest.raises(ValueError,match="先读"):
        await call("feishu_publish_knowledge",{})
    with pytest.raises(ValueError,match="第一页"):
        await call("feishu_publish_knowledge",draft)
    page=await call("feishu_read_history",{})
    with pytest.raises(ValueError,match="未读完"):
        await call("feishu_publish_knowledge",draft)
    await call("feishu_read_message",{"id":"m1","offset":page["excerpts"][0]["next_text_offset"]})
    receipt=await call("feishu_publish_knowledge",draft)
    assert receipt["confirmed"] and receipt["url"]=="https://demo.feishu.cn/docx/test123"
    records["m1"]["body"]["content"]=json.dumps({"text":"已修改"})
    with pytest.raises(ValueError,match="已变更"):
        await call("feishu_publish_knowledge",draft)
    with pytest.raises(ValueError):
        await call("feishu_publish_knowledge",{"folder":"external"})


@pytest.mark.asyncio
async def test_private_excerpts_follow_actual_employee_vendor_and_revocation(run,monkeypatch):
    call,_,settings=run
    monkeypatch.setattr(tools.history,"headers",lambda *_:{})
    monkeypatch.setattr(tools.history,"peer_for",lambda *_:"ou_peer")
    monkeypatch.setattr(tools.knowledge_document,"collect",lambda *_:{})
    args={"assignment":{"action":"employee","runtime_vendor":"deepseek"}}
    with pytest.raises(ValueError,match="供应商"):
        await call("feishu_read_history",{},**args)
    settings["employee_summary_vendors"]={"4":["deepseek"]}
    assert (await call("feishu_read_history",{},**args))["complete"]
    settings["employee_summary_vendors"]["4"]=[]
    with pytest.raises(ValueError,match="供应商"):
        await call("feishu_read_history",{},**args)

@pytest.mark.asyncio
async def test_source_metadata_only_discloses_confirmed_bound_document(run,monkeypatch):
    call,_,_=run
    from app.feishu import owner_oauth
    monkeypatch.setattr(owner_oauth,"status",lambda _:{"connected":True,"scopes":list(tools.history.SCOPES|tools.documents.SCOPES)})
    actions={"feishu_list_sources","feishu_publish_knowledge"}
    result=await call("feishu_list_sources",{},allowed_actions=actions)
    target=result["knowledge_target"]
    assert result["contacts"]==["李晗"] and target["title"]=="知识库" and target["url"] is None
    assert target["folder"]["state"]=="permission_required" and not target["sync_available"]
    assert "knowledge_target" not in await call("feishu_list_sources",{})

@pytest.mark.parametrize("suffix,expected",[
    ("最近2小时的聊天记录 2条",{"start":92800,"count":2}),
    ("的聊天记录 2条",{"count":2}),
    ("最近1天的聊天记录",{"start":13600}),
    ("的聊天记录",{}),
    ("的聊天记录 全部",{}),
])
def test_read_bounds_come_from_original_assignment_time(suffix,expected):
    db=sqlite3.connect(":memory:")
    db.executescript("create table delivery_timing(app,event,received);insert into delivery_timing values('app','old',100000)")
    proof={"command":"让二狗整理我和李晗"+suffix,"app":"app","event":"old"}
    assert tools.pinned_read_scope(proof,db,"二狗","李晗",{})==(100000,expected)
    with pytest.raises(ValueError):
        tools.pinned_read_scope({**proof,"command":"让二狗整理我和李晗最近99天的聊天记录"},db,"二狗","李晗",{})
    with pytest.raises(ValueError):
        tools.pinned_read_scope({**proof,"event":"unknown"},db,"二狗","李晗",{})


def test_bounded_fetch_asks_feishu_for_latest_exact_range():
    def response(req):
        params=req.url.params
        assert params["start_time"]=="92" and params["end_time"]=="100" and params["page_size"]=="2"
        assert params["sort_type"]=="ByCreateTimeDesc"
        rows=[dict(message_id=str(n),chat_id="oc_chat",create_time=str(n*1000)) for n in [110,99,98,90]]
        return httpx.Response(200,json={"code":0,"data":{"items":rows,"has_more":True}})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        found=tools.knowledge_document.collect(client,lambda:{},{"chat_id":"oc_chat"},100,start=92,count=2)
    assert list(found)==["99","98"]
