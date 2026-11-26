import hashlib
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from app.db.models import Agent, Thread
from app.team import service, work_verification as verify


def test_receipts_are_typed_and_paths_cannot_escape():
    call=("feature_files","read_text",{"scope":"role","path":"docs/note.md"})
    result={"content":[{"text":json.dumps({"text":"hello"})}]}
    assert verify.local_receipt(call,result)["sha256"]==hashlib.sha256(b"hello").hexdigest()
    assert verify.local_receipt(call,{**result,"isError":True}) is None
    assert verify.local_receipt(("fake",*call[1:]),result) is None
    for path in ("../other/a","C:/other","skills/a"):
        with pytest.raises(service.TeamError):
            verify.contract({"kind":"files","files":[{"scope":"task","path":path}]})


@pytest.mark.asyncio
async def test_file_claim_requires_actual_matching_artifact(tmp_path):
    role=tmp_path/"worker"
    folder=role/"任务"/"2"
    folder.mkdir(parents=True)
    target=folder/"deliver.md"
    target.write_text("real",encoding="utf8")
    job=NS(assignee_thread_id=1,assignee_agent_id=8,owner_id="owner",task_id=2)
    turn=NS(id=9,result="Wrote deliver.md")
    check=verify.contract({"kind":"files","files":[{"scope":"task","path":"deliver.md"}]})
    db=AsyncMock()
    async def get(model,key):
        if model==Thread: return NS(workspace_path=str(folder),agent_id=8)
        if model==Agent: return NS(role_path=str(role),owner_id="owner",id=8)
    db.get.side_effect=get
    db.scalars.return_value=[]
    assert (await verify.assess(db,job,turn,check))["state"]=="missing"
    receipt=verify.local_receipt(("feature_files","write_text",{"text":"real","path":"deliver.md"}),
                                 {"content":[{"text":'{"bytes":4}'}]})
    db.scalars.return_value=[{"file_receipt":receipt}]
    assert (await verify.assess(db,job,turn,check))["state"]=="files_verified"
    target.write_text("modified externally",encoding="utf8")
    assert (await verify.assess(db,job,turn,check))["state"]=="missing"
    turn.result="BLOCKED: Missing license"
    assert (await verify.assess(db,job,turn,check))=={"state":"blocked","detail":"Missing license"}
