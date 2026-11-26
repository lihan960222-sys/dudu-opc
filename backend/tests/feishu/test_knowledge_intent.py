import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from app.feishu import knowledge_intent
from app.feishu.ingress import receive
from app.feishu.routing import journal


@pytest.mark.asyncio
@pytest.mark.parametrize("decision",["restructure","update","resume","clarify","other","status","offline"])
async def test_semantic_followup_is_scoped_to_the_real_owner(tmp_path,monkeypatch,decision):
    identity = dict(app_id="lead", tenant_key="tenant", open_id="owner")
    with closing(journal(tmp_path/"db")) as db:
        finals=[]
        worker=SimpleNamespace(cfg={"steward":identity},db=db,direct_final=lambda *args:finals.append(args))
        def send(key,text,sender="owner",parent=None):
            msg=dict(message_id=key,chat_id="owner-dm",message_type="text",chat_type="p2p",
                     parent_id=parent,content=json.dumps({"text":text}))
            accepted=receive(db,identity,{"header":dict(app_id="lead",tenant_key="tenant",event_id=key),
                "event":{"sender":{"sender_type":"user","sender_id":{"open_id":sender}},"message":msg}})
            return accepted,msg
        assert send("1","请把我和李晗的聊天记录整理成飞书文档")[0]
        assert not send("bad","它也算上一节","another")[0]
        _,msg=send("2","刚提的那条跟第三节是同一件事，放一起就行。","owner","1")
        row=db.execute("select * from inbox where event='2'").fetchone()
        async def classify(cfg,task,text):
            assert task=="让二狗整理我和李晗的聊天记录 全部并写入云盘"
            if decision=="offline":
                raise ValueError()
            return decision
        routed=[]
        async def answer(worker,row,msg,command):
            routed.append(command)
            return True
        monkeypatch.setattr(knowledge_intent,"classify",classify)
        from app.feishu import employee_assignment
        monkeypatch.setattr(employee_assignment,"route",answer)
        assert await knowledge_intent.handle(worker,row,msg,"刚提的那条跟第三节是同一件事，放一起就行。") == (decision!="other")
        assert bool(routed)==(decision in ("restructure","update","resume"))
        if routed:
            assert routed[0].endswith("重整知识库" if decision=="restructure" else "写入云盘")
        proof=db.execute("select owner from owner_readwrite_requests where event='2'").fetchone()
        assert bool(proof)==bool(routed)
        assert bool(finals)==(decision in ("status","clarify","offline"))
        if decision in ("clarify", "offline"):
            assert finals[0][2] == "老板，我还没拿准您的意思。您是要我请二狗继续更新「李晗」知识库，还是只向您汇报进度？"
        if decision == "status":
            assert finals[0][2].startswith("老板，我查了二狗负责的「李晗」知识库")
        send("3","第三节")
        continued=db.execute("select command from knowledge_intent_candidates where event='3'").fetchone()
        assert bool(continued)==(decision!="other")

@pytest.mark.asyncio
@pytest.mark.parametrize("result,expected",[
    ({"intent":"restructure","confidence":"high"},"restructure"),
    ({"intent":"update","confidence":"low"},"clarify"),
    ({"intent":"update","confidence":"high","contact":"其他人"},"clarify"),
    ({"intent":"create_new_contact","confidence":"high"},"clarify"),
])
async def test_classifier_can_only_select_the_closed_enum(monkeypatch,result,expected):
    import httpx
    client=httpx.AsyncClient
    async def selection(*args,**kwargs):
        return 1,"deepseek-flash"
    monkeypatch.setattr(knowledge_intent.lead_chat,"select_thread",selection)
    monkeypatch.setattr(knowledge_intent.steward_deepseek,"api_key",lambda:"fixture-key")
    def complete(request):
        payload=json.loads(request.content)
        assert payload["model"]=="deepseek-flash"
        assert not payload.get("tools")
        assert payload["response_format"]=={"type":"json_object"}
        return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps(result)}}]})
    monkeypatch.setattr(knowledge_intent.httpx,"AsyncClient",lambda **kwargs:client(transport=httpx.MockTransport(complete)))
    assert await knowledge_intent.classify({},"known task","current request")==expected

def test_paraphrase_after_pre_fix_misdispatch_keeps_context(tmp_path):
    from app.feishu.routing import journal
    identity=dict(app_id="lead",tenant_key="t",open_id="owner")
    legacy="你让二狗再看一下，我刚刚更新了一个消息，继续整理，另外整理的时候不要只是按时间顺序记录，要分析摘要，并且合并、分类，用md格式整理好文档结构。"
    new="刚加的意见跟第三节是同一件事，放一起。"
    with closing(journal(tmp_path/"channel")) as db:
        def send(key,text):
            msg=dict(message_id=key,chat_id="dm",chat_type="p2p",message_type="text",content=json.dumps({"text":text}))
            receive(db,identity,{"header":dict(app_id="lead",tenant_key="t",event_id=key),
                "event":{"sender":{"sender_type":"user","sender_id":{"open_id":"owner"}},"message":msg}})
            return msg
        send("1","请把我和李晗的聊天记录整理成飞书文档")
        msg=send("2",legacy)
        # Reproduce the original gateway, which recorded an ordinary finished job only.
        for table in ["history_continuations","owner_readwrite_requests","knowledge_intent_candidates"]:
            db.execute("delete from "+table+" where event='2'")
        db.execute("insert into direct_team_reports(app,event,job,finished) values('lead','2',26,1)")
        send("3",new)
        assert db.execute("select command from knowledge_intent_candidates where event='3'").fetchone()[0].startswith("让二狗整理我和李晗")
        send("4","另一个项目，Jensen 帮我查看执行状态")
        send("5",new)
        assert not db.execute("select 1 from knowledge_intent_candidates where event='5'").fetchone()
