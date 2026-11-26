import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app.feishu import knowledge_document as knowledge


@pytest.mark.asyncio
async def test_pages_scope_and_repeat_publication(tmp_path, monkeypatch):
    source = dict(chat_id="oc_one", peer_open_id="ou_other", agent_id=4,oauth_app="dog")
    cfg = {"_access_config_path":str(tmp_path/"cfg.json"),"steward":{"open_id":"ou_owner"}}
    task = tmp_path/"employee-settings"/"4"
    task.mkdir(parents=True)
    (task/"knowledge-task.json").write_text(json.dumps(dict(contact="小李", document_name="知识库",
        folder_url="https://test.feishu.cn/drive/folder/abc",initial_range="all_available")),encoding="utf8")
    async def reviewer(*args):
        return (4,"员工")
    monkeypatch.setattr(knowledge.history,"reviewer",reviewer)
    monkeypatch.setattr(knowledge.history,"source_config",lambda *_:{"_employee_oauth":True,"steward":{"app_id":"dog","open_id":"ou_owner"}})
    monkeypatch.setattr(knowledge.history,"headers",lambda *_: {})
    monkeypatch.setattr(knowledge.history,"peer_for",lambda *_: "ou_other")
    monkeypatch.setattr(knowledge.workspace_access,"settings_for",lambda _:{"history_sources":{"小李":source}})
    writes = []
    remote = []
    def transport(req):
        if req.method=="GET" and req.url.path.endswith("/documents/testdoc"):
            data={"document":{"document_id":"testdoc"}}
        elif req.method=="GET" and req.url.path.endswith("/children"):
            data={"items":remote}
        elif req.method=="GET":
            assert "start_time" not in req.url.params
            second = "page_token" in req.url.params
            row = dict(chat_id="oc_one",message_id="b" if second else "a",create_time="1000",msg_type="text",
                       body={"content":json.dumps({"text":"确认"})})
            data=dict(has_more=not second, page_token="cursor",items=[row,{**row,"chat_id":"oc_other"}])
        elif req.url.path.endswith("/documents"):
            writes.append("create")
            data={"document":{"document_id":"testdoc"}}
        else:
            writes.append("append")
            remote.extend(json.loads(req.content)["children"])
            data={"children":[{}]}
        return httpx.Response(200,json={"code":0,"data":data})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        worker=SimpleNamespace(pub=SimpleNamespace(http=client),cfg=cfg,db=sqlite3.connect(":memory:"))
        for _ in range(2):
            result,count=await knowledge.publish(worker,{"app":"app"},[None,"员工",None,"小李"],source,100)
            assert count==2 and "https://test.feishu.cn/docx/testdoc" in result
        assert writes==["create","append","append"]
        remote.pop()
        with pytest.raises(ValueError,match="尚未完成"):
            await knowledge.publish(worker,{"app":"app"},[None,"员工",None,"小李"],source,100)

@pytest.mark.parametrize("status,code,should_create",[
    (404,1770002,True),(400,1770003,True),(403,99991679,False),(404,0,False),(503,1771005,False),
])
def test_recover_only_definitively_missing_bound_document(status,code,should_create):
    db=sqlite3.connect(":memory:")
    db.executescript("""create table knowledge_documents(binding text primary key,document text);
        insert into knowledge_documents values('key','old');
        create table knowledge_entries(binding text,id text,primary key(binding,id));
        insert into knowledge_entries values('key','m1');""")
    creates=[]
    def transport(req):
        if req.method=="POST":
            creates.append(json.loads(req.content))
            with pytest.raises(ValueError,match="暂不重复创建"):
                knowledge.resolve_document(db,client,lambda:{},"key",{"folder_token":"folder"},"知识库")
            return httpx.Response(200,json={"code":0,"data":{"document":{"document_id":"new"}}})
        return httpx.Response(status,json={"code":code})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        def run():
            return knowledge.resolve_document(db,client,lambda:{},"key",{"folder_token":"folder"},"知识库")
        if should_create:
            assert run()=="new"
            assert creates==[{"folder_token":"folder","title":"知识库"}]
            assert db.execute("select binding from knowledge_entries").fetchone()==("key/old",)
        else:
            with pytest.raises((ValueError,httpx.HTTPStatusError)):
                run()
            assert creates==[]
            assert db.execute("select document from knowledge_documents").fetchone()==("old",)
