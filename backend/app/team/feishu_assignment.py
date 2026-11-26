"""Attach the employee's Feishu capabilities to the actual delegated job."""
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from sqlalchemy import select

from app.db.models import AgentJobEvent, DirectTurnReceipt
from app.feishu import workspace_access


def configuration():
    path = os.getenv("FEISHU_CONFIG")
    if not path and os.getenv("LOCALAPPDATA"):
        path = Path(os.environ["LOCALAPPDATA"]) / "SuparagentSteward" / "feishu.json"
    return workspace_access.load_config(path) if path and Path(path).is_file() else None


def gateway_pin(cfg, key):
    folder = Path(cfg["_access_config_path"]).parent / cfg["state_dir"]
    with closing(sqlite3.connect((folder / "channel.sqlite3").resolve().as_uri()+"?mode=ro",uri=True)) as journal:
        journal.row_factory = sqlite3.Row
        if not journal.execute("select 1 from sqlite_master where type='table' and name='employee_feishu_assignments'").fetchone():
            return None
        row = journal.execute("""select a.* from employee_feishu_assignments a
            join owner_readwrite_requests r using(app,event,message)
            where a.app=? and a.event_key=? and r.owner=? and r.tenant=?""",
            (cfg["steward"]["app_id"],key,cfg["steward"]["open_id"],cfg["steward"]["tenant_key"])).fetchone()
        if not row:
            return None
        proof=dict(row)
        if proof["action"] in ("read_document","edit_document","delete_document"):
            proof["target_url"]=proof["title"]
        return proof


async def resolve(db, owner, lead, thread, turn, employee):
    cfg = configuration()
    if not cfg or cfg["owner_id"] != owner or cfg["steward"]["agent_id"] != lead:
        return None
    receipt = await db.scalar(select(DirectTurnReceipt).where(DirectTurnReceipt.owner_id==owner,
        DirectTurnReceipt.thread_id==thread,DirectTurnReceipt.turn_id==turn))
    if receipt:
        pin = gateway_pin(cfg,receipt.event_key)
        if pin:
            return {key:pin[key] for key in ("app","event","event_key","employee","contact","action","original")}
    # A human-originated work order also inherits the employee's configured
    # contact when the lead delegates it without any special channel command.
    sources = workspace_access.settings_for(cfg)["history_sources"]
    aliases = [name for name,src in sources.items() if src.get("agent_id")==employee
               and src.get("oauth_app") and any(role.get("agent_id")==employee and
               role.get("app_id")==src["oauth_app"] for role in cfg.get("roles",{}).values())]
    if len(aliases)==1:
        return {"app":cfg["steward"]["app_id"],"event":f"employee-{turn}-{employee}",
                "employee":employee,"contact":aliases[0],"action":"employee"}


async def for_job(db, job_id):
    pin = await db.scalar(select(AgentJobEvent.detail).where(AgentJobEvent.job_id==job_id,
                           AgentJobEvent.kind=="feishu_assignment").order_by(AgentJobEvent.id).limit(1))
    if pin and pin.get("event_key"):
        cfg = configuration()
        original = gateway_pin(cfg,pin["event_key"]) if cfg else None
        if not original or any(original.get(k)!=v for k,v in pin.items()):
            return None
        return original
    return pin


def required_tool(assignment):
    """Use the same delivery requirement when exposing, executing and verifying a tool."""
    return {"read_document":"feishu_read_document", "edit_document":"feishu_edit_document", "delete_document":"feishu_delete_document",
            "read": "feishu_read_history", "update": "feishu_publish_knowledge",
            "restructure": "feishu_publish_knowledge", "create_document": "feishu_create_document"}.get(
                assignment["action"], assignment.get("expected_tool"))


def delivery_contract(pin, delivery, original):
    from app.team import service
    if delivery is None:
        return pin
    if delivery in ("read_document","edit_document"):
        from app.feishu.document_access import urls
        links=urls(original or "")
        if len(links)!=1:
            raise service.TeamError("invalid_input","请指定本次要处理的一份文档链接。")
        return {**pin,"action":delivery,"target_url":links[0],"original":original,
                "expected_tool":"feishu_"+delivery}
    if delivery not in ("read","update","restructure"):
        raise service.TeamError("invalid_input","未知的飞书交付要求。")
    if delivery=="read":
        return {**pin,"expected_tool":"feishu_read_history"}
    return {**pin,"action":delivery,"original":original or "","expected_tool":"feishu_publish_knowledge"}
