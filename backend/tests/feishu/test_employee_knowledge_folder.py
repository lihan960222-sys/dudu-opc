import json
import sqlite3

import httpx
import pytest

from app.feishu import employee_tools, knowledge_document, owner_oauth


@pytest.fixture
def employee(monkeypatch):
    cfg = {"owner_id":"owner", "steward":{"agent_id":1,"app_id":"lead"}}
    identity = {"_employee_oauth":True, "steward":{"app_id":"employee","open_id":"ou_self"}}
    contact = "合成联系人"
    source = {"agent_id":4,"oauth_app":"employee","peer_open_id":"ou_peer","chat_id":"oc_chat"}
    dest = {"tenant_origin":"https://example.feishu.cn","folder_token":"assigned"}
    title = "项目摘要"
    scopes = set().union(*(owner_oauth.ACTIONS[key] for key in ("history","doc","folder")))
    policy = {"history_sources":{contact:source},"allow_codex_summary":True}
    async def staff(*args):
        return "合成员工"
    async def profile(*args):
        return 4, "合成员工", "codex", "model", ""
    monkeypatch.setattr(employee_tools,"staff_name",staff)
    monkeypatch.setattr(employee_tools,"employee_config",lambda *args:identity)
    monkeypatch.setattr(owner_oauth,"employee_config",lambda *args:identity)
    monkeypatch.setattr(employee_tools.workspace_access,"settings_for",lambda *args:policy)
    monkeypatch.setattr(knowledge_document,"target",lambda *args:(dest,title))
    monkeypatch.setattr(knowledge_document.history,"reviewer",profile)
    monkeypatch.setattr(owner_oauth,"status",lambda current:{"connected":current is identity,"scopes":sorted(scopes)})
    def headers(current, *, required):
        assert current is identity and set(required)<=scopes
        return {"Authorization":"Bearer employee-fixture"}
    monkeypatch.setattr(employee_tools.documents,"user_headers",headers)
    monkeypatch.setattr(owner_oauth,"user_headers",headers)
    db = sqlite3.connect(":memory:")
    db.execute("create table knowledge_documents(binding text primary key, document text)")
    key = knowledge_document.binding_key("lead",4,contact,source,dest,title)
    calls=[]
    def entry(token="doc", name=title):
        return {"type":"docx","token":token,"parent_token":"assigned","name":name}
    cloud = {"pages":[{"files":[entry("other","无关标题")],"has_more":False}], "docs":{}, "blocks":[],
             "messages":[{"message_id":mid, "message_position":n, "chat_id":"oc_chat", "msg_type":"text", "create_time":"1000",
                          "body":{"content":json.dumps({"text":mid})}} for n, mid in enumerate(("m1", "m2"))]}
    def transport(req):
        calls.append(req)
        assert req.headers.get("Authorization")=="Bearer employee-fixture"
        if req.url.path.endswith("/drive/v1/files"):
            assert req.method=="GET" and req.url.params["folder_token"]=="assigned"
            if cloud.get("denied"):
                return httpx.Response(403,json={"code":1061004,"msg":"private service detail"})
            return httpx.Response(200,json={"code":0,"data":cloud["pages"][int(req.url.params.get("page_token","0"))]})
        if req.url.path.endswith("/im/v1/chats/batch_query"):
            assert json.loads(req.content)=={"chat_ids":["oc_chat"]}
            data={"items":[{"chat_id":"oc_chat","chat_mode":"p2p","p2p_target_type":"user","p2p_target_id":"ou_peer"}]}
            return httpx.Response(200,json={"code":0,"data":data})
        if req.url.path.endswith("/im/v1/messages"):
            assert req.url.params["container_id"]=="oc_chat"
            data={"items":cloud["messages"],"has_more":False}
            return httpx.Response(200,json={"code":0,"data":data})
        if req.url.path.endswith("/blocks/new/children"):
            if req.method=="POST":
                cloud["blocks"]=json.loads(req.content)["children"]
                data={"children":[{"block_id":"b"+str(i)} for i in range(len(cloud["blocks"]))]}
                if cloud.get("moved"):
                    cloud["pages"]=[{"files":[],"has_more":False}]
            else:
                data={"items":[] if cloud.get("bad_body") else cloud["blocks"],"has_more":False}
            return httpx.Response(200,json={"code":0,"data":data})
        if req.method=="GET" and "/docx/v1/documents/" in req.url.path:
            token=req.url.path.rsplit("/",1)[-1]
            if cloud.get("doc_denied"):
                return httpx.Response(403,json={"code":99991679})
            if token in cloud["docs"]:
                return httpx.Response(200,json={"code":0,"data":{"document":{"document_id":token,"title":cloud["docs"][token],"revision_id":1}}})
            return httpx.Response(404,json={"code":1770002})
        if req.method=="POST" and req.url.path.endswith("/docx/v1/documents"):
            assert json.loads(req.content)=={"folder_token":"assigned","title":title}
            if cloud.get("lost_reply"):
                raise httpx.ReadTimeout("synthetic lost reply",request=req)
            cloud["docs"]["new"]=title
            cloud["pages"][-1]["files"].append(entry("new"))
            return httpx.Response(200,json={"code":0,"data":{"document":{"document_id":"new"}}})
        raise AssertionError((req.method,req.url.path))
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        async def discover(*, writable=True, proof=None, args=None, **kwargs):
            actions={"feishu_list_sources","feishu_read_document"}
            if writable:
                actions.add("feishu_publish_knowledge")
            return await employee_tools.execute(cfg,4,"feishu_list_sources",args or {},owner_id="owner",journal_db=db,
                client=client,allowed_actions=actions,contact=contact,invocation_id="job",
                assignment={"employee":4,"contact":contact,"action":"update","runtime_vendor":"openai",**(proof or {})},**kwargs)
        def bind(doc):
            with db:
                db.execute("insert into knowledge_documents values(?,?)",(key,doc))
        def resolve():
            return knowledge_document.resolve_document(db,client,lambda:headers(identity,required=owner_oauth.ACTIONS["doc"]),
                key,dest,title,folder_auth=lambda:headers(identity,required=owner_oauth.ACTIONS["folder"]))
        async def submit(name,args,**proof):
            return await employee_tools.execute(cfg,4,name,args,owner_id="owner",journal_db=db,client=client,
                allowed_actions={"feishu_read_history","feishu_read_message","feishu_publish_knowledge"},contact=contact,invocation_id="job",
                assignment={"employee":4,"contact":contact,"action":"update","runtime_vendor":"openai",**proof})
        cloud["submit"]=submit
        yield discover, bind, resolve, cloud, calls, scopes, entry
    db.close()


async def test_deleted_document_can_be_rebuilt_once_in_verified_bound_folder(employee):
    discover,bind,resolve,cloud,calls,_,entry=employee
    bind("deleted")
    cloud["pages"]=[{"files":[entry("other","私人文件名")],"has_more":True,"next_page_token":"1"},
                    {"files":[],"has_more":False}]
    report=(await discover())["knowledge_target"]
    assert report["remote_state"]=="missing" and report["url"] is None
    assert report["sync_available"] and report["publish_action"]=="create_in_bound_folder"
    folder=report["folder"]
    assert folder["url"]=="https://example.feishu.cn/drive/folder/assigned"
    assert folder["complete"] and folder["pages_checked"]==2 and folder["same_title_entries_seen"]==0
    assert "私人文件名" not in str(report) and all(call.method=="GET" for call in calls)
    with pytest.raises(ValueError, match="参数"):
        await discover(args={"folder_token":"another"})
    assert resolve()=="new"
    cloud["docs"]["new"]="项目摘要"
    cloud["pages"]=[{"files":[entry("new")],"has_more":False}]
    assert resolve()=="new"
    report=(await discover())["knowledge_target"]
    assert report["publish_action"]=="update_bound_document" and report["folder"]["bound_document_in_folder"]
    assert report["url"]=="https://example.feishu.cn/docx/new"
    assert sum(request.method=="POST" for request in calls)==1


@pytest.mark.parametrize("case",["name_conflict","incomplete","page_limit","denied","doc_denied",
                                "unknown_creation","weekly_missing","read_only"])
async def test_discovery_fails_closed_without_publishing(employee,case):
    discover,bind,resolve,cloud,calls,_,entry=employee
    bind(None if case=="unknown_creation" else "deleted")
    proof={}
    if case=="name_conflict":
        cloud["pages"]=[{"files":[entry("unrelated")],"has_more":False}]
    if case=="incomplete":
        cloud["pages"]=[{"files":[],"has_more":True,"next_page_token":"0"}]
    if case=="page_limit":
        cloud["pages"]=[{"files":[],"has_more":True,"next_page_token":str(i+1)} for i in range(8)]
    if case in ("denied","doc_denied"):
        cloud[case]=True
    if case=="weekly_missing":
        proof["weekly"]={"document":"deleted"}
    if case=="read_only":
        proof["read_only"]=True
    report=(await discover(writable=case!="read_only",proof=proof))["knowledge_target"]
    assert not report["sync_available"] and report["reason"]
    if case=="unknown_creation":
        assert report["remote_state"]=="creation_unknown"
    if case=="name_conflict":
        assert report["folder"]["same_title_entries_seen"]==1
    if case in ("incomplete","page_limit"):
        assert not report["folder"]["complete"] and report["folder"]["pages_checked"]<=5
    if case=="denied":
        assert report["folder"]["state"]=="unavailable" and report["folder"]["feishu_code"]==1061004
        assert "private service detail" not in str(report)
    if case=="doc_denied":
        assert report["remote_state"]=="unknown"
    if case in ("name_conflict","incomplete","page_limit","denied","doc_denied","unknown_creation"):
        with pytest.raises(ValueError,match="同名|完整|待核对|拒绝读取"):
            resolve()
    assert all(request.method=="GET" for request in calls)


async def test_unknown_create_receipt_is_never_retried_even_if_folder_looks_empty(employee):
    discover,_,resolve,cloud,calls,_,_=employee
    cloud["lost_reply"]=True
    with pytest.raises(httpx.ReadTimeout):
        resolve()
    report=(await discover())["knowledge_target"]
    assert report["remote_state"]=="creation_unknown" and not report["sync_available"]
    with pytest.raises(ValueError,match="待核对"):
        resolve()
    assert sum(request.method=="POST" for request in calls)==1


async def test_unknown_permission_and_unconfigured_target_disclose_no_location(employee,monkeypatch):
    discover,_,_,_,calls,scopes,_=employee
    scopes.difference_update(owner_oauth.ACTIONS["folder"])
    report=(await discover())["knowledge_target"]
    assert report["folder"]["state"]=="permission_required" and "url" not in report["folder"]
    assert not report["sync_available"] and not calls
    def missing(*args):
        raise ValueError("fixture unconfigured")
    monkeypatch.setattr(knowledge_document,"target",missing)
    report=(await discover())["knowledge_target"]
    assert not report["sync_available"] and "folder" not in report and not calls
    other=await discover(proof={"action":"read","expected_tool":"feishu_read_history"})
    assert "knowledge_target" not in other
    for proof in ({"employee":5},{"contact":"另一联系人"}):
        with pytest.raises(ValueError, match="不匹配"):
            await discover(proof=proof)
    assert not calls


@pytest.mark.parametrize("problem",[None,"bad_body","moved"])
async def test_normal_employee_publish_requires_full_cloud_readback(employee,problem):
    _,bind,_,cloud,calls,_,_=employee
    bind("deleted")
    submit=cloud["submit"]
    topics={"topics":[{"title":"合成主题","summary":"合成摘要","sources":["m1","m2"]}]}
    assert (await submit("feishu_read_history",{}))["source_complete"]
    with pytest.raises(ValueError,match="没有授权"):
        await submit("feishu_publish_knowledge",topics,read_only=True)
    if problem:
        cloud[problem]=True
        with pytest.raises(ValueError,match="读回|文件夹"):
            await submit("feishu_publish_knowledge",topics)
    else:
        result=await submit("feishu_publish_knowledge",topics)
        assert result["confirmed"] and result["messages_read"]==2
        assert result["verification"]=={"document_resolution":"created","title":"项目摘要",
            "url":"https://example.feishu.cn/docx/new","folder_url":"https://example.feishu.cn/drive/folder/assigned",
            "body_verified":True,"parent_verified":True,"source_messages_verified":2,"same_title_entries":1}
        assert result["url"]==result["verification"]["url"]
        repeated=await submit("feishu_publish_knowledge",topics)
        assert repeated["verification"]["document_resolution"]=="existing"
        assert repeated["verification"]["source_messages_verified"]==2
    assert sum(r.method=="POST" and r.url.path.endswith("/docx/v1/documents") for r in calls)==1


async def test_27_positioned_sources_with_long_text_and_attachment(employee):
    from app.feishu.weekly_knowledge import source_ids
    _, bind, _, cloud, calls, _, _ = employee
    bind("deleted")
    rows = [{"message_id": f"om_test_{n}", "message_position": str(100 + n * 3), "chat_id": "oc_chat",
             "msg_type": "file" if n == 12 else "text", "create_time": "1000",
             "body": {"content": json.dumps({"file_name": "合成附件"} if n == 12 else {"text": "文" * (4200 if n == 0 else 8)})}}
            for n in range(27)]
    cloud["messages"] = rows
    submit = cloud["submit"]
    seen = {}
    for offset in (0, 10, 20):
        page = await submit("feishu_read_history", {"offset": offset})
        seen.update({row["id"]: row for row in page["excerpts"]})
    assert page["complete"] and not page["source_complete"] and seen["om_test_12"]["type"] == "file"
    topics = {"topics": [{"title": "进展", "summary": "汇总合成消息和附件记录。", "sources": list(seen)}]}
    with pytest.raises(ValueError, match="未读完"):
        await submit("feishu_publish_knowledge", topics)
    long = seen["om_test_0"]
    while long["next_text_offset"] is not None:
        long = await submit("feishu_read_message", {"id": "om_test_0", "offset": long["next_text_offset"]})
    assert long["source_complete"]
    receipt = await submit("feishu_publish_knowledge", topics)
    assert receipt["confirmed"] and receipt["verification"]["source_messages_verified"] == 27
    assert source_ids(cloud["blocks"], "oc_chat", records={row["message_id"]:row for row in rows}) == set(seen)
    assert sum(call.method == "POST" and call.url.path.endswith("/docx/v1/documents") for call in calls) == 1
