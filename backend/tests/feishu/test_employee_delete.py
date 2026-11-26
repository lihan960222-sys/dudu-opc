import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import httpx
import pytest

from app.feishu import document_delete as deletion, owner_oauth


def test_delete_is_separate_opt_in(tmp_path):
    cfg={"_access_config_path":str(tmp_path/"feishu.json"),"owner_id":"me","_employee_oauth":True,
         "steward":{"app_id":"cli_123","open_id":"ou_me"}}
    url=lambda action: owner_oauth.start(cfg, action,"http://localhost/cb","browser")
    assert "space:document:delete" not in parse_qs(urlsplit(url("both")).query)["scope"][0].split()
    assert set(parse_qs(urlsplit(url("delete")).query)["scope"][0].split())=={"space:document:delete","space:document:retrieve","offline_access"}


def test_exact_employee_document_only_and_replay(monkeypatch):
    db=sqlite3.connect(":memory:")
    db.executescript("create table employee_document_jobs(app,employee,doc,confirmed);insert into employee_document_jobs values('worker',4,'abc',1);")
    identity={"steward":{"app_id":"worker"},"_employee_oauth":True}
    url="https://team.feishu.cn/docx/abc"
    proof={"event_key":"event","action":"delete_document","employee":4,"target_url":url,"original":"删除文档 "+url}
    monkeypatch.setattr(deletion.knowledge_document,"target",lambda *args:({"tenant_origin":"https://team.feishu.cn","folder_token":"fld"},"kb"))
    monkeypatch.setattr(deletion.document_access,"in_folder",lambda *args: True)
    calls=[]
    def delete(request):
        calls.append(request)
        return httpx.Response(200,json={"code":0})
    monkeypatch.setattr(deletion.documents,"user_headers",lambda cfg,required:{"Authorization":"test"} if required==owner_oauth.ACTIONS["delete"] else {})
    with httpx.Client(transport=httpx.MockTransport(delete)) as http:
        for changes in [{"original":"不要删除 "+url},{"employee":7},{"event_key":""},{"action":"employee"},{"target_url":url+"x"},{"target_url":"https://other.feishu.cn/docx/abc"}]:
            with pytest.raises(ValueError):
                deletion.delete_owned({},4,identity,db,http,{**proof,**changes})
        assert not calls
        assert deletion.delete_owned({},4,identity,db,http,proof)["confirmed"]
        assert deletion.delete_owned({},4,identity,db,http,proof)["confirmed"]
        assert len(calls)==1 and calls[0].method=="DELETE" and calls[0].url.params["type"]=="docx"
        db.execute("update employee_document_deletions set confirmed=0")
        with pytest.raises(ValueError,match="待核对"):
            deletion.delete_owned({},4,identity,db,http,proof)
        assert len(calls)==1
