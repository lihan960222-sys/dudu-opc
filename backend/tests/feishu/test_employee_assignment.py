import json
from contextlib import closing

import pytest

from app.feishu import documents, employee_assignment, history, ingress
from app.feishu.routing import journal
from app.feishu.worker import Worker
from types import SimpleNamespace


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected",[
    ("让二狗整理我和李晗的聊天记录","read"),
    ("请把我和李晗的聊天记录整理成飞书文档","update"),
    ("让二狗整理我和李晗的聊天记录 全部并重整知识库","restructure"),
    ("写飞书文档 测试标题\n测试正文","create_document"),
    ("让二狗阅读文档 https://tenant.feishu.cn/wiki/node","read_document"),
    ("让二狗修改文档 https://tenant.feishu.cn/docx/doc\n原文：第一段\n改为：第二段","edit_document"),
    ("让二狗删除文档 https://tenant.feishu.cn/docx/doc","delete_document"),
])
async def test_authenticated_assignment_goes_to_lead_never_gateway(tmp_path,monkeypatch,text,expected):
    identity=dict(app_id="lead",tenant_key="t",open_id="boss")
    with closing(journal(tmp_path/"channel")) as db:
        cfg=dict(steward=identity,roles={},steward_thread=123,history_sources={"李晗":{"agent_id":5,"chat_id":"oc_chat","peer_open_id":"ou_peer"}})
        worker=Worker(cfg,SimpleNamespace(db=db))
        async def reviewer(*_): return (5,"二狗")
        async def writer(*_): return None,5,{}
        sent=[]
        async def lead(row,msg,prompt): sent.append(prompt)
        async def bypass(*args): pytest.fail("gateway executed employee action")
        monkeypatch.setattr(history,"reviewer",reviewer)
        monkeypatch.setattr(documents,"writer",writer)
        monkeypatch.setattr(history,"answer",bypass)
        monkeypatch.setattr(documents,"handle",bypass)
        monkeypatch.setattr(worker,"route_direct",lead)
        msg=dict(message_id="m",chat_id="dm",chat_type="p2p",message_type="text",content=json.dumps({"text":text}))
        assert ingress.receive(db,identity,dict(header=dict(app_id="lead",event_id="e",tenant_key="t"),
            event=dict(sender=dict(sender_type="user",sender_id={"open_id":"boss"}),message=msg)))
        row=db.execute("select * from inbox").fetchone()
        await worker.process(row)
        assignment=db.execute("select * from employee_feishu_assignments").fetchone()
        assert assignment["employee"]==5 and assignment["action"]==expected
        assert "delegate" in sent[0] and "5" in sent[0] and len(sent)==1
        if expected=="create_document":
            assert assignment["title"]=="测试标题" and assignment["body"]=="测试正文"
            assert "测试正文" not in sent[0]
        db.execute("delete from owner_readwrite_requests")
        await worker.process(row)
        assert len(sent)==1
        worker.api.close()


@pytest.mark.asyncio
async def test_resume_keeps_requirements_of_the_quoted_assignment(tmp_path,monkeypatch):
    from app.feishu import knowledge_intent
    identity=dict(app_id="lead",tenant_key="t",open_id="boss")
    with closing(journal(tmp_path/"channel")) as db:
        cfg=dict(steward=identity,roles={},steward_thread=123,
                 history_sources={"李晗":{"agent_id":5,"chat_id":"oc_chat","peer_open_id":"ou_peer"}})
        worker=Worker(cfg,SimpleNamespace(db=db))
        async def reviewer(*args): return (5,"二狗")
        async def accept(*args): pass
        async def resume(*args): return "resume"
        monkeypatch.setattr(history,"reviewer",reviewer)
        monkeypatch.setattr(worker,"route_direct",accept)
        monkeypatch.setattr(knowledge_intent,"classify",resume)
        def incoming(key,text,parent=None):
            msg=dict(message_id=key,chat_id="dm",chat_type="p2p",message_type="text",
                     parent_id=parent,content=json.dumps({"text":text}))
            ingress.receive(db,identity,dict(header=dict(app_id="lead",event_id=key,tenant_key="t"),
                event=dict(sender=dict(sender_type="user",sender_id={"open_id":"boss"}),message=msg)))
            return db.execute("select * from inbox where event=?",(key,)).fetchone(),msg
        original="让二狗整理我和李晗的聊天记录 全部并重整知识库"
        row,msg=incoming("first",original)
        await employee_assignment.route(worker,row,msg,original)
        row,msg=incoming("follow","再派一次，写进去","first")
        assert await knowledge_intent.handle(worker,row,msg,"再派一次，写进去")
        pin=db.execute("select original,action from employee_feishu_assignments where event='follow'").fetchone()
        assert tuple(pin)==(original,"restructure")
        worker.api.close()
