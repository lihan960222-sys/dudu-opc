import json
import shlex
from types import SimpleNamespace

import httpx
import pytest

from app.feishu import approvals, ingress
from app.feishu.publisher import Publisher
from app.feishu.routing import journal


def test_bound_owner_click_sends_exact_decision_once(tmp_path):
    db = journal(tmp_path/"channel")
    approvals.initialize(db)
    identity = {"app_id":"app", "tenant_key":"tenant", "open_id":"owner"}
    worker = SimpleNamespace(db=db, cfg={"steward":identity}, respond=lambda *args: None)
    calls, sent = [], []

    def backend(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"rule_id":1})

    def platform(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"code":0,"data":{"message_id":"om_live"}})

    approvals.queue_notice(db, {"thread":2,"turn":3,"event":"team:42","assignment_epoch":4,"agent_id":5}, 8,
        {"id":"a1","tool":"Bash","can_remember":True,"input":{"command":"tool --version","cwd":"C:/project"}}, "Jensen")
    row=db.execute("select * from outbox").fetchone()
    pub = Publisher(worker.cfg, db, tmp_path)
    pub.http.close()
    with httpx.Client(transport=httpx.MockTransport(platform)) as pub.http:
        pub.headers=lambda _role: {}
        assert pub.deliver(row)=="om_live"
    db.execute("update outbox set state='sent', remote='om_live'")
    assert sent[0]["msg_type"]=="interactive"
    card=json.loads(sent[0]["content"])
    action=card["elements"][-1]["actions"][0]
    assert action["value"] == {"approval":"a1","decision":"允许并记住"}
    event={"header":{"app_id":"app","tenant_key":"tenant","event_type":"card.action.trigger","event_id":"e1"},
           "event":{"operator":{"open_id":"owner","tenant_key":"tenant"},
                    "action":action,"context":{"open_message_id":"om_live","open_chat_id":"oc_dm"}}}
    for obj,key in ((event["event"]["operator"],"open_id"),(event["header"],"tenant_key"),
                    (event["event"]["context"],"open_message_id"),(action["value"],"approval")):
        saved=obj[key]
        obj[key]="wrong"
        assert not ingress.receive_approval_click(db,identity,event)
        obj[key]=saved
    assert ingress.receive_approval_click(db,identity,event)
    assert not ingress.receive_approval_click(db,identity,event)
    inbox=db.execute("select * from inbox").fetchone()
    msg=json.loads(inbox["payload"])
    with httpx.Client(transport=httpx.MockTransport(backend),base_url="http://localhost") as worker.api:
        assert approvals.answer(worker,inbox,msg,json.loads(msg["content"])["text"])
    assert calls==[{"decision":"allow","remember":True,"expected_turn_id":3,"expected_assignment_epoch":4,"expected_employee_id":5}]
    assert not ingress.receive_approval_click(db,identity,event)
    db.close()


def test_new_card_default_work_order_and_late_review(tmp_path):
    db = journal(tmp_path / "channel")
    approvals.initialize(db)
    args = {"thread":2, "turn":3, "event":"team:42", "assignment_epoch":1, "agent_id":5}
    item = {"id":"new", "tool":"Bash", "can_remember":True, "can_scope_task":True,
            "input":{"command":"Get-Location", "cwd":str(tmp_path)}}
    approvals.queue_notice(db, args, 10, item, "员工 · 工单 #10")
    db.execute("update outbox set state='sent',remote='sent'")
    requests, replies = [], []
    def backend(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json={"status":"review_requested"})
    with httpx.Client(transport=httpx.MockTransport(backend), base_url="http://localhost") as api:
        worker = SimpleNamespace(db=db, cfg={"steward":{"app_id":"app","open_id":"owner"}},
                                 api=api, respond=lambda *args: replies.append(args[2]))
        assert approvals.answer(worker, {"app":"app","event":"reply"},
                                {"parent_id":"sent","chat_type":"p2p","message_type":"text"}, "允许")
    assert requests == [{"decision":"allow","remember":True,"expected_turn_id":3,
                         "expected_assignment_epoch":1,"expected_employee_id":5,
                         "task_scope":True,"review_if_stale":True}]
    assert "只读核对" in replies[0]
    db.close()


def test_recursive_metadata_pipeline_has_plain_language_description_but_no_shell_grant():
    from app.services.employee_approval_rules import identity
    from pathlib import Path
    script = ("Get-ChildItem -LiteralPath 'C:/logs','D:/other' -Recurse -Filter 'kit_*.log' "
              "-ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | "
              "Select-Object -First 3 FullName,LastWriteTime | Format-List")
    view = approvals._card_display({"tool":"Bash", "input":{"command":script,"cwd":"C:/work"}}, "员工", "detail")
    assert "最近 3 个文件" in view["summary"]
    project = SimpleNamespace(root=Path("C:/work"))
    assert identity(project, "Bash", {"command":script, "cwd":str(project.root)}) is None


def test_native_card_preview_for_string_wrapper_preserves_permission_request():
    cmd = r'''"C:\Program Files\PowerShell\7\pwsh.exe" -NoProfile -Command "Get-Location"'''
    args = {"command":cmd, "cwd":"C:/work", "additionalPermissions":{"network":{"enabled":True}}}
    view = approvals._card_display({"tool":"Bash","input":args}, "Jensen", "verified")
    assert "Get-Location" in view["summary"] and "pwsh.exe" not in view["summary"]
    assert "额外权限" in view["summary"] and json.loads(view["details"]) == args


def test_long_native_command_card_explains_purpose_and_available_scope(tmp_path):
    from app.feishu.publisher import _approval_card

    script = "Get-Location\n" * 130
    argv = ["pwsh.exe", "-NoProfile", "-Command", script]
    args = {"command": shlex.join(argv), "argv": argv, "cwd": "C:/job", "reason": "核对当前项目目录"}
    db = journal(tmp_path / "channel")
    approvals.initialize(db)
    execution = {"thread": 2, "turn": 3, "event": "team:10", "assignment_epoch": 1, "agent_id": 5}
    approvals.queue_notice(db, execution, 10, {"id": "a", "input": args, "tool": "Bash"}, "Jensen · 工单 #10")
    notice = json.loads(db.execute("select body from outbox").fetchone()[0])
    binding = db.execute("select * from approval_dm").fetchone()
    card = _approval_card(notice, binding)
    summary = card["elements"][0]["text"]["content"]
    assert "查看工作目录" in summary and "员工说明：核对当前项目目录" in summary
    assert "没有可复用的授权范围" in summary
    assert [a["value"]["decision"] for a in card["elements"][-1]["actions"]] == ["仅本次", "拒绝"]
    assert json.loads(notice["approval_display"]["details"]) == args
    db.execute("update outbox set remote='sent',state='sent'")
    messages = []
    worker = SimpleNamespace(db=db, cfg={"steward": {"app_id":"app"}}, respond=lambda *a: messages.append(a[2]))
    assert approvals.answer(worker, {"app":"app","event":"reply"},
                            {"parent_id":"sent","chat_type":"p2p","message_type":"text"}, "允许并记住")
    assert messages and db.execute("select state from approval_dm").fetchone()[0] == "pending"
    db.close()


def test_native_approval_purpose_not_guessed_from_code_or_private_task():
    args = {"command": "Stop-Process -Id 2133", "cwd": "C:/work"}
    approval = {"input": args, "tool": "Bash", "task_brief": "password=example"}
    display = approvals._card_display(approval, "Jensen", "details")
    assert "结束进程，进程号 2133" in display["summary"]
    assert "没有单独说明" in display["summary"] and "password" not in display["summary"]


@pytest.mark.parametrize("reject_layout", [False, True])
def test_native_card_shows_task_employee_and_script_before_exact_details(tmp_path, reject_layout):
    cfg = {"steward":{"app_id":"app","open_id":"owner"}}
    argv = [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoProfile", "-Command",
            "rg --version\nGet-Location"]
    args = {"command":shlex.join(argv), "argv":argv, "cwd":str(tmp_path), "reason":"check version"}
    db = journal(tmp_path/"channel")
    approvals.initialize(db)
    approvals.queue_notice(db, {"thread":2,"turn":3,"event":"team:118","assignment_epoch":4,"agent_id":5}, 33,
        {"id":"req","tool":"Bash","input":args}, "Jensen · 工单 #33")
    row = db.execute("select * from outbox").fetchone()
    sent = []
    def platform(request):
        sent.append(json.loads(request.content))
        if reject_layout and len(sent) == 1:
            return httpx.Response(400, json={"code":230099,"msg":"Failed to create card content"})
        return httpx.Response(200, json={"code":0,"data":{"message_id":"om_card"}})
    pub = Publisher(cfg, db, tmp_path)
    pub.http.close()
    pub.headers = lambda role: {}
    with httpx.Client(transport=httpx.MockTransport(platform)) as pub.http:
        assert pub.deliver(row) == "om_card"
    card = json.loads(sent[0]["content"])
    assert "Jensen · 工单 #33" in card["header"]["title"]["content"]
    summary = card["elements"][0]["text"]["content"]
    assert "rg --version" in summary and "powershell.exe" not in summary and "完整" in summary
    panel = card["elements"][1]
    assert panel["tag"] == "collapsible_panel" and panel["expanded"] is False
    assert json.loads(panel["elements"][0]["text"]["content"]) == args
    assert card["elements"][-1]["actions"][0]["value"] == {"approval":"req","decision":"仅本次"}
    if reject_layout:
        assert len(sent) == 2 and sent[0]["uuid"] == sent[1]["uuid"]
        fallback = json.loads(sent[1]["content"])
        assert fallback["elements"][1] == panel["elements"][0]
        assert fallback["elements"][-1] == card["elements"][-1]
    else:
        assert len(sent) == 1
    db.close()
