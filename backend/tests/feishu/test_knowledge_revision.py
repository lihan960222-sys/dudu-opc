import json
import sqlite3

import httpx
import pytest

from app.feishu import knowledge_revision as revision


@pytest.mark.asyncio
@pytest.mark.parametrize("failure",[None,"missing_source","write_unconfirmed","readback_empty"])
async def test_versioned_summary_tracks_edits_and_remote_confirmation(monkeypatch,failure):
    db=sqlite3.connect(":memory:")
    calls=[]
    inputs=[]
    async def permitted():
        return True
    async def model(profile,entries,authorize,instruction,schema):
        assert await authorize()
        inputs.extend(entries)
        return {"topics":[{"title":"接口设计","summary":"开发计划已更新","sources":[] if failure=="missing_source" else list(range(1,len(entries)+1))}]}
    monkeypatch.setattr(revision,"model_result",model)
    def respond(req):
        if req.method=="GET":
            return httpx.Response(200,json={"code":0,"data":{"items":[] if failure=="readback_empty" else calls[-1]}})
        body=json.loads(req.content)
        assert body["index"]==0 and req.url.path.endswith("/existing/blocks/existing/children")
        calls.append(body["children"])
        return httpx.Response(200,json={"code":0,"data":{"children":[] if failure=="write_unconfirmed" else
            [{"block_id":str(n)} for n,_ in enumerate(body["children"])]}})
    records={"m1":{"body":{"content":"a"*9000+"尾端"},"msg_type":"text","create_time":"1"}}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        async def run():
            return await revision.revise_document(db,client,lambda:{},permitted,("employee",),"key","existing",
                "https://demo.feishu.cn","知识库","chat",records)
        if failure:
            with pytest.raises(ValueError):
                await run()
            assert not db.execute("select 1 from knowledge_revisions where status='done'").fetchone()
            if failure=="write_unconfirmed":
                with pytest.raises(ValueError,match="待核对"):
                    await run()
                assert len(calls)==1
            elif failure=="readback_empty":
                with pytest.raises(ValueError,match="读回"):
                    await run()
                assert len(calls)==1
            else:
                assert calls==[]
            return
        message,count=await run()
        assert count==1 and "/existing" in message
        assert "尾端" in "".join(inputs)
        assert len(calls)==1
        await run()
        assert len(calls)==1
        records["m1"]["body"]={"content":"编辑后的内容"}
        await run()
        assert len(calls)==2 and "编辑后的内容" in inputs[-1]
        assert all("messageId=m1" in json.dumps(blocks) for blocks in calls)

@pytest.mark.asyncio
async def test_restructure_same_history_replaces_managed_summary_and_verifies(monkeypatch):
    db=sqlite3.connect(":memory:")
    records={"m1":{"body":{"content":"项目 A"},"create_time":"1"}}
    human=revision.text_block("text","老板的笔记")
    remote=[human]
    prompts=[]
    async def permitted():
        return True
    async def model(profile,entries,authorize,instruction,schema):
        prompts.append(instruction)
        return {"topics":[{"title":"项目","summary":str(len(prompts)),"sources":[1]}]}
    monkeypatch.setattr(revision,"model_result",model)
    def respond(req):
        if req.method=="POST":
            blocks=json.loads(req.content)["children"]
            remote[:0]=blocks
            data={"children":[{"block_id":str(i)} for i in range(len(blocks))]}
        elif req.method=="DELETE":
            body=json.loads(req.content)
            assert req.url.params["document_revision_id"]=="17"
            del remote[body["start_index"]:body["end_index"]]
            data={}
        elif req.url.path.endswith("/children"):
            data={"items":remote}
        else:
            data={"document":{"revision_id":17}}
        return httpx.Response(200,json={"code":0,"data":data})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        async def run(instructions=""):
            return await revision.revise_document(db,client,lambda:{},permitted,(),"key","doc",
                "https://demo.feishu.cn","知识库","chat",records,instructions=instructions)
        await run()
        await run("把分类收一收")
        assert len(prompts)==2 and "把分类收一收" in prompts[-1]
        assert len(remote)==5 and remote[-1]==human
        await run()
        assert len(prompts)==2
        remote[0]=human
        with pytest.raises(ValueError,match="已变化"):
            await run("把分类收一收")
