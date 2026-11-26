import pytest

from app.feishu import knowledge_revision as revision
from app.team import feishu_tools_bridge as bridge, policy


def test_authorized_employee_can_choose_how_to_deliver(monkeypatch):
    identity = {"_employee_oauth": True, "steward": {"app_id": "worker"}}
    monkeypatch.setattr(bridge.owner_oauth, "employee_config", lambda *_: identity)
    monkeypatch.setattr(bridge.owner_oauth, "status", lambda *_: {"actions": {"doc": True, "history": True}})
    monkeypatch.setattr(bridge.workspace_access, "settings_for", lambda *_: {
        "history_sources": {"客户": {"agent_id": 4, "oauth_app": "worker"}}, "allow_codex_summary": True})
    monkeypatch.setattr(bridge.knowledge_document, "target", lambda *_: ({}, "知识库"))
    goal = {"action": "employee", "employee": 4, "contact": "客户"}
    for original in ("把之前的结论按主题归到文档", "把那份再理顺点", "继续我引用的事"):
        _, names = bridge.configured({}, 4, {**goal, "original": original})
        assert "feishu_publish_knowledge" in names
        assert "feishu_read_history" in names
    _, read_only = bridge.configured({}, 4, {**goal, "read_only": True})
    assert "feishu_read_history" in read_only and "feishu_publish_knowledge" not in read_only
    assert bridge.configured({}, 5, goal) is None


def test_read_only_turn_can_actually_use_its_read_tools():
    team = policy.TurnTeam(1, 4, False, False, feature_consult=True,
                           feishu_tools=("feishu_read_history", "feishu_publish_knowledge"))
    assert policy.automatic("mcp__team__feishu_read_history", "plan", team, {})[0] == "allowed"
    assert policy.automatic("mcp__team__feishu_publish_knowledge", "plan", team, {})[0] == "denied"


def test_worker_draft_links_only_to_messages_observed_in_this_task():
    topics = [{"title": "交付", "summary": "周五核对交付。", "sources": ["m1", "m2"]}]
    blocks = revision.employee_blocks(topics, {"m1": {}, "m2": {}}, "oc_example")
    assert blocks[-1]["text"]["elements"][0]["text_run"]["content"] == "周五核对交付。"
    assert "messageId=m2" in blocks[-1]["text"]["elements"][-1]["text_run"]["text_element_style"]["link"]["url"]
    for sources in (["m1"], ["m1", "invented"], ["m1", "m1"], [], [True, "m2"]):
        with pytest.raises(ValueError):
            revision.employee_blocks([{**topics[0], "sources": sources}], {"m1": {}, "m2": {}}, "oc_example")


def test_missing_write_grant_preserves_bound_read_and_blocks_unverifiable_creation(monkeypatch):
    identity = {"_employee_oauth":True,"steward":{"app_id":"employee"}}
    monkeypatch.setattr(bridge.owner_oauth,"employee_config",lambda *_:identity)
    monkeypatch.setattr(bridge.owner_oauth,"status",lambda *_:{
        "actions":{"doc":True,"delete":False,"folder":False,"history":False},"scopes":["docx:document:readonly"]})
    monkeypatch.setattr(bridge.knowledge_document,"target",lambda *_:({"folder_token":"folder"},"Doc"))
    for action in ("edit_document","delete_document"):
        proof,names=bridge.configured({},4,{"action":action,"event_key":"event","target_url":"https://demo.feishu.cn/docx/doc"})
        assert names==("feishu_read_document",) and proof["delivery_blocker"]
    proof,names=bridge.configured({},4,{"action":"create_document","title":"Doc"})
    assert "feishu_create_document" not in names and "space:document:retrieve" in proof["delivery_blocker"]
