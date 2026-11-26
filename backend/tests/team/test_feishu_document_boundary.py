from types import SimpleNamespace

import pytest

from app.team import feishu_assignment


def test_rework_keeps_the_existing_delivery_and_cannot_downgrade_it_to_a_local_report():
    from app.team.service import TeamError
    import pytest
    previous={"action":"employee","employee":4,"contact":"联系人","event":"old"}
    current={"action":"employee","employee":4,"contact":"联系人","event":"new"}
    assert feishu_assignment.rework_contract(previous,current,"feishu_publish_knowledge",None)==previous
    with pytest.raises(TeamError):
        feishu_assignment.rework_contract(previous,None,"feishu_publish_knowledge","none")
    with pytest.raises(TeamError):
        feishu_assignment.rework_contract(previous,{**current,"employee":5},"feishu_publish_knowledge","restructure")


@pytest.mark.asyncio
async def test_ordinary_person_delegation_gets_only_its_own_employee_capability(monkeypatch):
    cfg = {"owner_id":"owner","steward":{"agent_id":1,"app_id":"boss"},
           "roles":{"worker":{"agent_id":4,"app_id":"worker-app"}}}
    monkeypatch.setattr(feishu_assignment,"configuration",lambda: cfg)
    policies = {"history_sources":{"contact":{"agent_id":4,"oauth_app":"worker-app"}}}
    monkeypatch.setattr(feishu_assignment.workspace_access,"settings_for",lambda _:policies)
    class Session:
        async def scalar(self,statement):
            return None
    db=Session()
    pin=await feishu_assignment.resolve(db,"owner",1,10,20,4)
    assert pin["employee"]==4 and pin["contact"]=="contact" and pin["action"]=="employee"
    assert await feishu_assignment.resolve(db,"owner",1,10,20,2) is None
    assert await feishu_assignment.resolve(db,"owner",2,10,20,4) is None
    policies["history_sources"]["other"]={"agent_id":4,"oauth_app":"worker-app"}
    assert await feishu_assignment.resolve(db,"owner",1,10,20,4) is None


def test_employee_delivery_uses_the_owner_goal_without_keyword_matching():
    pin={"action":"employee","contact":"bound","employee":4}
    request="这几块合在一处，以后我想按主题找。"
    explicit=feishu_assignment.delivery_contract(pin,"restructure",request)
    assert explicit["original"]==request
    assert explicit["expected_tool"]=="feishu_publish_knowledge"
    assert feishu_assignment.delivery_contract(pin,"read",request)["expected_tool"]=="feishu_read_history"
    assert feishu_assignment.delivery_contract(pin,None,request)==pin


async def test_cloud_document_work_does_not_require_or_inherit_an_unrelated_contact(monkeypatch):
    cfg={"owner_id":"owner","steward":{"agent_id":1,"app_id":"lead"},
         "roles":{"editor":{"agent_id":4,"app_id":"employee"}}}
    monkeypatch.setattr(feishu_assignment,"configuration",lambda:cfg)
    sources={}
    monkeypatch.setattr(feishu_assignment.workspace_access,"settings_for",lambda _:{"history_sources":sources})
    class Session:
        async def scalar(self,_): return None
    for assigned in (False,True):
        if assigned:
            sources["联系人"]={"agent_id":4,"oauth_app":"employee"}
        scope=await feishu_assignment.resolve(Session(),"owner",1,10,20,4,delivery="create_document",original="把这份报告存成云文档")
        assert scope["employee"]==4 and scope["contact"] is None
    assert (await feishu_assignment.resolve(Session(),"owner",1,10,20,4,delivery="create_document",original="把我和联系人的沟通整理成文档"))["contact"]=="联系人"
    assert await feishu_assignment.resolve(Session(),"owner",1,10,20,5,delivery="create_document",original="写文档") is None

def test_owner_choices_only_lists_employees_authorized_for_documents(monkeypatch):
    cfg = {"owner_id": "owner", "steward": {"agent_id": 1, "app_id": "lead"},
           "roles": {"二狗": {"agent_id": 4, "app_id": "employee"},
                     "Jensen": {"agent_id": 2, "app_id": "other"}}}
    monkeypatch.setattr(feishu_assignment, "configuration", lambda: cfg)
    monkeypatch.setattr(feishu_assignment.workspace_access, "settings_for", lambda _: {
        "history_sources": {"李晗": {"agent_id": 4, "oauth_app": "employee"}}})
    assert feishu_assignment.owner_choices("owner", 1) == ((4,), None)
    cfg["roles"]["writer"] = {"agent_id": 5, "app_id": "writer", "feishu_documents": True}
    assert feishu_assignment.owner_choices("owner", 1) == ((4, 5), None)
