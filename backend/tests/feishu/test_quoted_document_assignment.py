import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.feishu import documents, ingress
from app.feishu.routing import journal
from app.feishu.worker import Worker
from tests.feishu.test_documents import seed_answer


@pytest.mark.asyncio
@pytest.mark.parametrize("text,parent,expected", [
    ("写飞书文档 例会结论", "om_unavailable", None),
    ("写飞书文档 例会结论", "om_old", "上次的回答"),
    ("写飞书文档 例会结论\n先核对排期。", "om_unavailable", "先核对排期。"),
    ("写飞书文档 空白模板", None, ""),
    ("请新建飞书文档，标题为《例会模板》，正文留空", "om_old", ""),
    ("请新建飞书文档，标题为《例会模板》，正文留空", "om_unavailable", ""),
    ("写飞书文档 正文留空", "om_unavailable", None),
    ("请新建飞书文档，标题为《例会，正文留空，备份》", "om_unavailable", None),
    ("写飞书文档 例会结论", {"root_id":"om_old"}, None),
])
async def test_quoted_document_assignment_keeps_the_requested_source(tmp_path,monkeypatch,text,parent,expected):
    identity = {"app_id":"app","tenant_key":"tenant","open_id":"owner"}
    message = {"message_id":"om_request","chat_id":"chat1","chat_type":"p2p",
               "message_type":"text","content":json.dumps({"text":text})}
    if isinstance(parent,dict):
        message.update(parent)
    elif parent:
        message["parent_id"] = parent
    event = {"header":{"app_id":"app","tenant_key":"tenant","event_id":"request"},
             "event":{"sender":{"sender_type":"user","sender_id":{"open_id":"owner"}},"message":message}}
    with closing(journal(tmp_path / "gateway.sqlite")) as db:
        worker = Worker({"steward":identity,"roles":{},"steward_thread":7},SimpleNamespace(db=db))
        with worker.api:
            monkeypatch.setattr(worker,"route_direct",AsyncMock())
            monkeypatch.setattr(documents,"writer",AsyncMock(return_value=(None,4,{})))
            seed_answer(db,"chat1")
            assert ingress.receive(db,identity,event)
            row = db.execute("select * from inbox where event='request'").fetchone()
            await worker.process(row)
            assignment = db.execute("select body from employee_feishu_assignments where event='request'").fetchone()
            if expected is None:
                worker.route_direct.assert_not_awaited()
                assert assignment is None
                reply = json.loads(db.execute("""select o.body from direct_finals f join outbox o on o.id=f.outgoing
                    where f.app='app' and f.event='request'""").fetchone()[0])
                assert reply["reply_to"]=="om_request" and "引用" in reply["text"] and "正文" in reply["text"]
            else:
                worker.route_direct.assert_awaited_once()
                assert assignment["body"]==expected
