import json

import httpx
import pytest

from app.feishu.publisher import Publisher
from app.feishu.routing import enqueue, journal


def test_long_reply_uploads_complete_markdown_once_and_resumes_summary(tmp_path, monkeypatch):
    db = journal(tmp_path / "channel.sqlite3")
    body = "已处理，详细结果如下。\n" + "证据与来源。\n" * 180
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
    assert json.loads(sends[-1]["content"])["text"] == "已处理，详细结果如下。\n详见完整回复。"
    assert len(files) == 1 and not files[0].exists()
    db.close()


@pytest.mark.parametrize("detail", ["\n1. 第一步。\n2. ", " 1. 第一步。 2. ", "：1. 第一步。2. "])
def test_preview_preserves_status_and_omits_numbered_details(detail):
    from app.services.steward_reply import preview
    assert preview("**尚未写入**。我会继续跟进。" + detail + "细节"*510) == "尚未写入。我会继续跟进。\n详见完整回复。"


def test_steward_owns_failures_and_retains_the_original_error():
    from app.services import steward_reply
    detail = "尚未派工。\n原文：\n> 你必须确认权限。\n```sh\nexit 7\n```"
    text = steward_reply.turn_status("failed", detail)
    assert text.startswith("老板，这次我没办完。") and text.endswith(detail)
    assert "已交给" not in text and "自动重试" not in text
    assert steward_reply.preview(text) == text
    assert steward_reply.turn_status("cancelled") == "老板，这次办理已经停止。"
    assert "尚未核实" in steward_reply.turn_status("interrupted")


def test_steward_instructions_explain_the_limit_and_require_evidence_before_reporting():
    from app.services.steward_reply import INSTRUCTIONS
    for phrase in ("1000", "Markdown", "老板", "您", "催促", "承担", "回执", "授权", "先听取", "已经做了什么"):
        assert phrase in INSTRUCTIONS


def test_direct_requests_drop_pressure_and_keep_the_owners_quotes():
    from app.services.steward_reply import preview
    assert preview("老板，你赶紧确认权限。") == "老板，请您确认权限。"
    assert preview("你希望我用哪份文档？") == "您希望我用哪份文档？"
    assert preview("你得知结果。") == "你得知结果。"
    quoted = "员工原话：\n> 老板，你赶紧确认权限。"
    assert preview(quoted) == quoted


def test_mixed_markdown_keeps_evidence_and_softens_only_the_direct_question():
    from app.services.steward_reply import preview
    evidence = '这一步返回“不能派工。你赶紧确认权限。”，字段是 `task_id`。'
    blocks = '\n\n> 你给个编号。\n\n~~~~text\n给个编号。\n\n你赶紧确认权限。\n~~~~\n\n'
    original = evidence + '给个任务编号，我再核对。' + blocks + '老板，你赶紧确认权限。'
    expected = evidence + '您方便告诉我任务编号吗？我再核对。' + blocks + '老板，请您确认权限。'
    assert preview(original) == expected
    details = '\n\n运行记录：\n给个编号。\n你必须确认范围。'
    assert preview('给个编号。' + details) == '您方便告诉我编号吗？' + details
    raw_error = '报错原文：\n你赶紧确认权限。\n给个编号。'
    assert preview(raw_error + '\n\n你直接给个文件名。') == raw_error + '\n\n您方便告诉我文件名吗？'
    cited = "员工说'第一句。你赶紧确认权限。'，文档为[原文](local.txt)。"
    assert preview(cited + '给个编号。') == cited + '您方便告诉我编号吗？'


@pytest.mark.parametrize("size", [999, 1000, 1001])
def test_length_increasing_polite_question_keeps_exact_boundary(size):
    from app.services.steward_reply import preview
    tail = '。给个编号。'
    original = '𠮷' * (size - len(tail)) + tail
    rendered = preview(original)
    assert len(rendered) <= 1000
    if size <= 1000:
        assert rendered == original[:-len('给个编号。')] + '请问编号？'
    else:
        assert rendered.endswith('详见完整回复。')


@pytest.mark.parametrize("length", [999, 1000, 1001])
@pytest.mark.parametrize("reply_to", [None, "om_original"])
def test_owner_text_boundary_counts_chinese_and_non_bmp_characters(tmp_path, monkeypatch, length, reply_to):
    opening = "你必须确认范围。"
    body = opening + "𠮷" * (length - len(opening))
    db = journal(tmp_path / "gateway.db")
    payload = {"text": body}
    if reply_to:
        payload["reply_to"] = reply_to
    identity = {"open_id": "owner"}
    row_id = enqueue(db, None, "steward", payload)
    row = db.execute("select * from outbox where id=?", (row_id,)).fetchone()
    pub = Publisher({"steward": identity}, db, tmp_path)
    pub.http.close()
    sent, files = [], []

    def upload(role, path, kind):
        assert role == identity and path.name == "完整回复.md" and kind == "file"
        files.append(path.read_bytes())
        return "uk_file"

    def handle(req):
        sent.append(json.loads(req.content))
        assert req.url.path.endswith("/im/v1/messages" + ("/om_original/reply" if reply_to else ""))
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_sent"}})

    monkeypatch.setattr(pub, "upload", upload)
    monkeypatch.setattr(pub, "headers", lambda role: {})
    with httpx.Client(transport=httpx.MockTransport(handle)) as pub.http:
        assert pub.deliver(row) == "om_sent"
    db.close()
    text = json.loads(sent[-1]["content"])["text"]
    if length <= 1000:
        assert [message["msg_type"] for message in sent] == ["text"]
        assert text == body.replace("你必须", "请您") and not files
    else:
        assert [message["msg_type"] for message in sent] == ["file", "text"]
        assert files == [body.encode("utf-8")]
        assert text == "请您确认范围。\n详见完整回复。"


def test_retry_finishes_an_attachment_sent_before_the_limit_change(tmp_path, monkeypatch):
    db = journal(tmp_path / "gateway.db")
    body = "老板，我还在等回执。\n" + "详情" * 200
    row_id = enqueue(db, None, "steward", {"text": body, "reply_to": "om_original"})
    db.execute("create table if not exists steward_reply_files(outgoing text primary key, remote text not null)")
    db.execute("insert into steward_reply_files values(?,?)", (row_id, "om_older_attachment"))
    row = db.execute("select * from outbox where id=?", (row_id,)).fetchone()
    pub = Publisher({"steward": {}}, db, tmp_path)
    pub.http.close()
    sent = []

    def handle(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_text"}})

    monkeypatch.setattr(pub, "headers", lambda role: {})
    with httpx.Client(transport=httpx.MockTransport(handle)) as pub.http:
        assert pub.deliver(row) == "om_text"
    db.close()
    assert [item["msg_type"] for item in sent] == ["text"]
    assert json.loads(sent[0]["content"])["text"] == "老板，我还在等回执。\n详见完整回复。"
