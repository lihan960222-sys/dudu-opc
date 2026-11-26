"""Delete one explicitly requested document previously created by the assigned employee."""
from . import document_access, documents, knowledge_document, owner_oauth, workspace_access
from .publisher import API, checked


def delete_owned(cfg, employee, identity, db, client, proof):
    if not (proof and proof.get("event_key") and proof.get("action") == "delete_document"
            and proof.get("employee") == employee and identity.get("_employee_oauth")):
        raise ValueError("删除文档需要老板本次明确指定目标。")
    dest, title = knowledge_document.target(cfg, employee)
    prefix = dest["tenant_origin"] + "/docx/"
    url = proof.get("target_url") or ""
    request = document_access.request(proof.get("original") or "")
    if not request or request[1:3] != ("delete_document", url):
        raise ValueError("删除目标必须来自老板本次明确的原话。")
    doc = url.removeprefix(prefix)
    if not url.startswith(prefix) or not documents.TOKEN.fullmatch(doc):
        raise ValueError("请提供此工作区的完整飞书文档链接。")
    app = identity["steward"]["app_id"]
    tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
    owned = "employee_document_jobs" in tables and db.execute(
        "select 1 from employee_document_jobs where app=? and employee=? and doc=? and confirmed=1",
        (app, employee, doc)).fetchone()
    if not owned and "knowledge_documents" in tables:
        sources = workspace_access.settings_for(cfg)["history_sources"]
        for alias, source in sources.items():
            if source.get("agent_id") != employee or source.get("oauth_app") != app:
                continue
            key = knowledge_document.binding_key(cfg["steward"]["app_id"], employee, alias, source, dest, title)
            if db.execute("select 1 from knowledge_documents where binding=? and document=?", (key, doc)).fetchone():
                owned = True
                break
    if not owned:
        raise ValueError("这份文档没有该员工的创建记录。")
    headers = documents.user_headers(identity, required=owner_oauth.ACTIONS["delete"])
    db.execute("""create table if not exists employee_document_deletions(
        app text, employee integer, event text, doc text, confirmed integer default 0,
        primary key(app, employee, event))""")
    key = (app, employee, proof["event_key"])
    if not db.execute("select 1 from employee_document_deletions where app=? and employee=? and event=?", key).fetchone():
        if not document_access.in_folder(client, lambda: headers, dest["folder_token"], doc):
            raise ValueError("目标文档已不在该员工获准管理的文件夹。")
    with db:
        claimed = db.execute("insert or ignore into employee_document_deletions(app,employee,event,doc) values(?,?,?,?)",
                             (*key, doc)).rowcount
    if not claimed:
        prior = db.execute("select doc,confirmed from employee_document_deletions where app=? and employee=? and event=?", key).fetchone()
        if not prior or prior[0] != doc or not prior[1]:
            raise ValueError("上一次删除结果待核对，暂不重复提交。")
    else:
        response = client.delete(API + "/drive/v1/files/" + doc, params={"type":"docx"}, headers=headers, timeout=15)
        if response.status_code in (400, 401, 403, 404, 429):
            with db:
                db.execute("delete from employee_document_deletions where app=? and employee=? and event=?", key)
        checked(response)
        with db:
            db.execute("update employee_document_deletions set confirmed=1 where app=? and employee=? and event=?", key)
    return {"url":url, "confirmed":True, "message":"飞书已确认，这份文档已移入回收站。"}
