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
    def respond(req):
        calls.append(req)
        if req.method=="GET" and req.url.path.endswith("/children"):
            body=json.loads(calls[1].content)
            return httpx.Response(200,json={"code":0,"data":{"items":body["children"]}})
        return httpx.Response(200,json={"code":0,"data":{"document":{"document_id":"doc"}} if len(calls)==1 else {"children":[{"block_id":"a"}]}})
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
    call,calls,_=run
    args={"title":"测试","body":"正文"}
    result=await call("feishu_create_document",args)
    assert result==await call("feishu_create_document",args)
    assert result["confirmed"] and result["url"]=="https://demo.feishu.cn/docx/doc"
    assert [r.method for r in calls]==["POST","POST","GET","GET"] and json.loads(calls[0].content)["folder_token"]=="folder"
    with pytest.raises(ValueError):
        await call("feishu_create_document",{**args,"body":"另一份"})

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
    async def publish(worker,row,review,source,cutoff,*,revise,instructions):
        assert row["app"]=="lead" and review[1]=="二狗" and source["oauth_app"]=="employee" and revise
        return "已写入 https://demo.feishu.cn/docx/test123",11
    monkeypatch.setattr(tools.knowledge_document,"publish",publish)
    receipt=await call("feishu_publish_knowledge",{})
    assert receipt["confirmed"] and receipt["url"]=="https://demo.feishu.cn/docx/test123"
    with pytest.raises(ValueError):
        await call("feishu_publish_knowledge",{"folder":"external"})

@pytest.mark.asyncio
async def test_source_metadata_only_discloses_confirmed_bound_document(run,monkeypatch):
    call,_,_=run
    from app.feishu import owner_oauth
    monkeypatch.setattr(owner_oauth,"status",lambda _:{"connected":True,"scopes":list(tools.history.SCOPES|tools.documents.SCOPES)})
    actions={"feishu_list_sources","feishu_publish_knowledge"}
    result=await call("feishu_list_sources",{},allowed_actions=actions)
    assert result["contacts"]==["李晗"] and result["knowledge_target"]=={"title":"知识库","url":None,"sync_available":True}
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
