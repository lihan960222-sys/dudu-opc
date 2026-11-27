import json
from contextlib import closing
from unittest.mock import AsyncMock

import pytest

from app.feishu.ingress import receive
from app.feishu.publisher import Publisher
from app.feishu.routing import journal
from app.feishu.worker import Worker


@pytest.mark.parametrize("text", ["让 Jensen 检查这组结果", "再让 Jensen 试一下", "再试试看", "先处理前天说的第二件事"])
async def test_bound_steward_receives_owner_words_once(tmp_path, monkeypatch, text):
    identity = {"app_id":"lead", "tenant_key":"t", "open_id":"owner", "agent_id":1}
    cfg = {"steward":identity, "steward_thread":31, "owner_id":"boss", "isolated_runner_enabled":True,
           "roles":{"Jensen":{"agent_id":2}}}
    message = {"message_id":"m", "message_type":"text", "chat_id":"dm", "chat_type":"p2p",
               "content":json.dumps({"text":text})}
    event = {"header":{"app_id":"lead", "tenant_key":"t", "event_id":"e"}, "event":{
        "sender":{"sender_type":"user", "sender_id":{"open_id":"owner"}}, "message":message}}
    with closing(journal(tmp_path/"journal")) as db:
        worker = Worker(cfg, Publisher(cfg, db, tmp_path))
        try:
            direct = AsyncMock()
            monkeypatch.setattr(worker, "route_direct", direct)
            assert receive(db, identity, event)
            assert not receive(db, identity, event)
            row = db.execute("select * from inbox").fetchone()
            await worker.process(row)
            direct.assert_awaited_once_with(row, message, text)
            assert not db.execute("select 1 from submissions").fetchone()
        finally:
            worker.api.close()
            worker.pub.http.close()
