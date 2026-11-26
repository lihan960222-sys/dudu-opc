import json

import httpx
import pytest

from app.feishu.publisher import Publisher
from app.feishu.routing import enqueue, journal


def test_long_reply_uploads_complete_markdown_once_and_resumes_summary(tmp_path, monkeypatch):
    db = journal(tmp_path / "channel.sqlite3")
    body = "已处理，详细结果如下。\n" + "证据与来源。\n" * 60
    row_id = enqueue(db, None, "steward", {"text":body, "reply_to":"om_owner"})
    row = db.execute("select * from outbox where id=?", (row_id,)).fetchone()
    pub = Publisher({"steward":{}}, db, tmp_path)
    pub.http.close()
    sends, files = [], []
    def upload(role, path, kind):
        assert path.name == "完整回复.md" and path.read_bytes() == body.encode()
        files.append(path)
        return "uploaded"
    def handle(req):
        assert req.url.path.endswith("om_owner/reply")
        data = json.loads(req.content)
        sends.append(data)
        if len(sends) == 2:
            raise httpx.ConnectError("lost connection")
        return httpx.Response(200, json={"code":0, "data":{"message_id":"om_" + data["msg_type"]}})
    monkeypatch.setattr(pub, "upload", upload)
    monkeypatch.setattr(pub, "headers", lambda role:{})
    with httpx.Client(transport=httpx.MockTransport(handle)) as pub.http:
        with pytest.raises(httpx.ConnectError):
            pub.deliver(row)
        assert pub.deliver(row) == "om_text"
    assert [s["msg_type"] for s in sends] == ["file","text","text"]
    assert sends[1]["uuid"] == sends[2]["uuid"] == row_id
    assert len(json.loads(sends[-1]["content"])["text"]) <= 200
    assert len(files) == 1 and not files[0].exists()
    db.close()


@pytest.mark.parametrize("detail", ["\n1. 第一步。\n2. ", " 1. 第一步。 2. ", "：1. 第一步。2. "])
def test_preview_preserves_status_and_omits_numbered_details(detail):
    from app.services.steward_reply import preview
    assert preview("**尚未写入**。我会继续跟进。" + detail + "细节"*120) == "尚未写入。我会继续跟进。\n详见完整回复。"
