import json
from contextlib import closing

from app.feishu.ingress import receive
from app.feishu.routing import journal


def test_continue_private_history_to_drive(tmp_path):
    identity = dict(app_id="lead", tenant_key="t", open_id="owner")
    original = "让二狗整理我和李晗的聊天记录"
    followup = "你继续让他干，看全部的消息记录，并整理文档落入云盘"
    with closing(journal(tmp_path/"channel")) as db:
        def send(key, text, parent=None, chat="dm"):
            msg = dict(message_type="text", chat_type="p2p", chat_id=chat, message_id=key,
                       parent_id=parent, content=json.dumps({"text":text}))
            assert receive(db, identity, {"header":dict(app_id="lead",tenant_key="t",event_id=key),
                "event":{"sender":{"sender_type":"user","sender_id":{"open_id":"owner"}},"message":msg}})
            return db.execute("select command from history_continuations where event=?",(key,)).fetchone()
        send("1",original)
        assert send("2",followup)[0] == original+" 全部并写入云盘"
        send("3","另外一个项目")
        assert send("4",followup)[0] is None
        assert send("5",followup,"1")[0] == original+" 全部并写入云盘"
        assert send("6",followup,"1","another")[0] is None
        stored = db.execute("select payload from inbox where event='2'").fetchone()[0]
        assert json.loads(json.loads(stored)["content"])["text"] == followup

        assert send("7","请把我和李晗的聊天记录整理成飞书文档")[0] == original+" 全部并写入云盘"
        assert send("8","请把这份报告写成飞书文档","1")[0] == "让二狗写飞书文档 整理报告"
        assert send("9","请把它整理成飞书文档","1")[0] == original+" 全部并写入云盘"
        assert send("10","请不要写飞书文档") is None
        assert send("11","这个报告里谈到了写飞书文档") is None


def test_explicit_delivery_scope_and_questions():
    from app.feishu.ingress import is_delivery_request, document_assignment
    for phrase in ["这份报告最后要落实成飞书文档","交付物是飞书文档","不改项目，只需要写成飞书文档",
                   "请把这份说明写成飞书文档，可以吗？"]:
        assert is_delivery_request(phrase)
    for phrase in ["交付物是飞书文档吗？","怎么写成飞书文档","请不要写成飞书文档","请总结写成飞书文档的方法吗？",
                   "请总结写成飞书文档的方法","请不要把报告写成飞书文档","写飞书文档，但不要上传","这个报告提到了写成飞书文档"]:
        assert not is_delivery_request(phrase)
    assert document_assignment("这份报告最后要落实成飞书文档，标题为《现场记录》\n原始正文") == "让二狗写飞书文档 现场记录\n原始正文"
