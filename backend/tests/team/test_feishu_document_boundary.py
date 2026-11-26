from types import SimpleNamespace

import pytest

from app.team import feishu_assignment


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
