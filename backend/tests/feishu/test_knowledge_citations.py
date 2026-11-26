from copy import deepcopy
from urllib.parse import unquote

import pytest

from app.feishu import knowledge_revision as revision, weekly_knowledge as weekly


def make_draft(mid, item):
    return revision.employee_blocks([{"title": "项目", "summary": "已核对", "sources": [mid]}], {mid: item}, "oc_chat")


def test_message_anchors_use_provider_position_and_verify_source_id_separately():
    records = {"om_a": {"message_position": "0"}, "om_b": {"message_position": 23}}
    draft = revision.employee_blocks([{"title": "进展", "summary": "两条记录", "sources": list(records)}], records, "oc_chat")
    first, second = draft[-1]["text"]["elements"][1:]
    stored_url = first["text_run"]["text_element_style"]["link"]["url"]
    assert stored_url.startswith("https%3A%2F%2F")
    assert unquote(stored_url) == "https://applink.feishu.cn/client/chat/open?openChatId=oc_chat&position=0"
    link = second["text_run"]["text_element_style"]["link"]
    assert "position=23" in unquote(link["url"]) and "messageId" not in link["url"]
    assert "om_b" in second["text_run"]["content"]
    server = deepcopy(draft)
    for element in server[-1]["text"]["elements"][1:]:
        url = element["text_run"]["text_element_style"]["link"]
        url["url"] = unquote(url["url"])
    assert revision.matches(server, draft)
    assert weekly.source_ids(server, "oc_chat", records=records) == set(records)
    link["url"] = unquote(link["url"]).replace("position=23", "position=24")
    with pytest.raises(ValueError):
        weekly.source_ids(draft, "oc_chat", records=records)
    with pytest.raises(ValueError):
        weekly.source_ids(server, "oc_another", records=records)


@pytest.mark.parametrize("position", [None, "", False, -1, "1&foo=2"])
def test_missing_or_invalid_position_never_claims_a_message_link(position):
    with pytest.raises(ValueError, match="定位"):
        make_draft("om_nolink", {"message_position": position})


def test_existing_weekly_sources_survive_without_republishing_unverified_chat_links():
    prior = make_draft("om_old", {"message_position": 1})
    prior[-1]["text"]["elements"][-1] = {"text_run": {"content": "　来源1", "text_element_style": {"link": {
        "url": "https://applink.feishu.cn/client/chat/open?openChatId=oc_chat&messageId=om_old"}}}}
    second = deepcopy(prior[-1]["text"]["elements"][-1])
    second["text_run"]["text_element_style"]["link"]["url"] += "_two"
    prior[-1]["text"]["elements"].append(second)
    new = {"om_new": {"message_position": 4}}
    assert weekly.source_ids(prior, "oc_chat") == {"om_old", "om_old_two"}
    merged = weekly.merge_blocks(prior, make_draft("om_new", new["om_new"]), "oc_chat")
    assert "messageId=" not in str(merged) and "messageId=om_old" in str(prior)
    assert "仅编号" in str(merged)
    assert weekly.source_ids(merged, "oc_chat", records=new) == {"om_old", "om_old_two", "om_new"}
    assert len(merged[3]["text"]["elements"]) == 2 and revision.removable(merged[3])
    with pytest.raises(ValueError):
        weekly.source_ids(prior, "oc_chat", records={"om_old": {"message_position": 1}})
