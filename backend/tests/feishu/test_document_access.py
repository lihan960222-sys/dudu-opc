import json
import sqlite3

import httpx
import pytest

from app.feishu import document_access as access
from app.team.feishu_assignment import delivery_contract
from app.team.service import TeamError


def test_document_actions_and_original_link_only():
    url="https://demo.feishu.cn/docx/doc"
    assert access.request("让二狗修改文档 "+url)==("二狗","edit_document",url,"")
    assert access.request("不要删除 "+url) is None
    assert access.request("如果我说删除 "+url+" 会怎么样？") is None
    assert access.request("删除 "+url+"\n但是先别动") is None
    assert access.request("删除 "+url)==("二狗","delete_document",url,"")
    assert delivery_contract({},"edit_document","让二狗把第二段写清楚一点 "+url)["target_url"]==url
    with pytest.raises(TeamError):
        delivery_contract({},"edit_document","改一下这个")
    assert access.message_urls({"body":{"content":json.dumps({"content":[[{"tag":"a","href":url}]]})}})==[url]


@pytest.mark.asyncio
async def test_read_wiki_docx_and_reject_skipped_pages(monkeypatch):
    db=sqlite3.connect(":memory:")
    identity={"steward":{"app_id":"employee"}}
    url="https://demo.feishu.cn/wiki/node"
    scopes=[]
    def headers(_,*,required):
        scopes.extend(required)
        return {}
    monkeypatch.setattr(access.documents,"user_headers",headers)
    def send(req):
        data={"node":{"obj_type":"docx","obj_token":"doc"}} if req.url.path.endswith("get_node") else {"content":"甲"*8001}
        return httpx.Response(200,json={"code":0,"data":data})
    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        async def read(offset):
            return await access.read_page(client,identity,db,4,"job",url,offset)
        with pytest.raises(ValueError,match="顺序"):
            await read(8000)
        first=await read(0)
        assert first["next_offset"]==8000 and not first["complete"]
        last=await read(first["next_offset"])
        assert last["complete"] and last["text"]=="甲"
    assert set(scopes)=={"wiki:node:read","docx:document:readonly"}


@pytest.mark.parametrize("present",[True,False])
def test_scoped_replace_and_readback(monkeypatch,present):
    db=sqlite3.connect(":memory:")
    identity={"steward":{"app_id":"employee"}}
    url="https://demo.feishu.cn/docx/doc"
    dest={"folder_token":"folder","tenant_origin":"https://demo.feishu.cn"}
    monkeypatch.setattr(access.knowledge_document,"target",lambda *_:(dest,"知识库"))
    monkeypatch.setattr(access.documents,"user_headers",lambda *_a,**_kw:{})
    state={"block_id":"b","parent_id":"doc","block_type":2,"text":{"elements":[{"text_run":{"content":"旧内容"}}]}}
    writes=[]
    def send(req):
        if req.url.path.endswith("/files"):
            data={"files":[{"type":"docx","token":"doc","parent_token":"folder"}] if present else [],"has_more":False}
        elif req.method=="PATCH":
            assert req.url.params["document_revision_id"]=="7"
            writes.append(req)
            state["text"]["elements"]=json.loads(req.content)["update_text_elements"]["elements"]
            data={"block":state}
        elif req.url.path.endswith("/doc"):
            data={"document":{"revision_id":7}}
        elif req.url.path.endswith("/children"):
            data={"items":[state]}
        else:
            data={"block":state}
        return httpx.Response(200,json={"code":0,"data":data})
    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        def run():
            return access.append({},4,identity,db,client,"job",url,"原文：旧内容\n改为：新内容")
        if not present:
            with pytest.raises(ValueError,match="文件夹内"):
                run()
            assert not writes
        else:
            assert run()["confirmed"] and run()["confirmed"] and len(writes)==1
