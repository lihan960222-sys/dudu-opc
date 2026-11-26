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


async def resolve(db, owner, lead, thread, turn, employee, *, delivery=None, original=""):
    cfg = configuration()
    if not cfg or cfg["owner_id"] != owner or cfg["steward"]["agent_id"] != lead:
        return None
    receipt = await db.scalar(select(DirectTurnReceipt).where(DirectTurnReceipt.owner_id==owner,
        DirectTurnReceipt.thread_id==thread,DirectTurnReceipt.turn_id==turn))
    if receipt:
        pin = gateway_pin(cfg,receipt.event_key)
        if pin:
            return {key:pin[key] for key in ("app","event","event_key","employee","contact","action","original","target_url") if key in pin}
    if delivery=="none":
        return None
    # A human-originated work order also inherits the employee's configured
    # contact when the lead delegates it without any special channel command.
    sources = workspace_access.settings_for(cfg)["history_sources"]
    aliases = [name for name,src in sources.items() if src.get("agent_id")==employee
               and src.get("oauth_app") and any(role.get("agent_id")==employee and
               role.get("app_id")==src["oauth_app"] for role in cfg.get("roles",{}).values())]
    named=[name for name in aliases if name and name in (original or "")]
    contact=named[0] if len(named)==1 else None
    if delivery in ("create_document","edit_document","read_document"):
        if not any(role.get("agent_id")==employee and role.get("app_id") for role in cfg.get("roles",{}).values()):
            return None
        # A document-only instruction does not implicitly authorize reading a person's chat.
        if len(named)>1:
            return None
    elif contact is None:
        if len(aliases)!=1:
            return None
        contact=aliases[0]
    return {"app":cfg["steward"]["app_id"],"event":f"employee-{turn}-{employee}",
            "employee":employee,"contact":contact,"action":"employee"}


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
    if not isinstance(assignment.get("action"),str):
        return assignment.get("expected_tool")
    return {"read_document":"feishu_read_document", "edit_document":"feishu_edit_document", "delete_document":"feishu_delete_document",
            "read": "feishu_read_history", "update": "feishu_publish_knowledge",
            "restructure": "feishu_publish_knowledge", "create_document": "feishu_create_document"}.get(
                assignment.get("action"), assignment.get("expected_tool"))


def document_employees(cfg):
    """Employees the owner has authorized for Feishu documents.

    A contact mapped to the employee in the saved access policy is the existing
    proof that the employee may read the owner's Feishu sources and publish the
    knowledge document. A role may also opt in explicitly for document-only
    work, without a contact, with feishu_documents set to true.
    """
    found=set()
    try:
        sources=workspace_access.settings_for(cfg).get("history_sources") or {}
    except (OSError,ValueError,KeyError,sqlite3.Error):
        sources={}
    for source in sources.values():
        if (isinstance(source,dict) and type(source.get("agent_id")) is int
                and source.get("oauth_app")):
            found.add(source["agent_id"])
    for role in cfg.get("roles",{}).values():
        if role.get("feishu_documents") is True and type(role.get("agent_id")) is int:
            found.add(role["agent_id"])
    return found


def owner_choices(owner, lead, event_key=None):
    """New owner orders select a delivery. Existing authenticated gateway orders already carry it."""
    cfg=configuration()
    if not cfg or cfg["owner_id"]!=owner or cfg["steward"]["agent_id"]!=lead:
        return (),None
    expected=gateway_pin(cfg,event_key) if event_key else None
    documents=document_employees(cfg)
    choices=tuple(role["agent_id"] for role in cfg.get("roles",{}).values()
                  if role.get("app_id") and type(role.get("agent_id")) is int
                  and role["agent_id"] in documents
                  and (not expected or role["agent_id"]!=expected["employee"]))
    return choices,expected


def delivery_choice(args):
    choice=args.get("feishu_delivery")
    if choice is None and isinstance(check:=args.get("completion_check"),dict):
        if check.get("kind") in ("files","inspection","commands"):
            return "none"
    return choice


def choice_missing(run,args):
    targets=args.get("agent_ids")
    return (isinstance(targets,list) and args.get("mode","work")=="work" and delivery_choice(args) is None and
            any(target in getattr(run,"delivery_choices",()) for target in targets))


def rework_contract(previous, current, tool, choice):
    """An employee retry uses the same owner's grant and cannot replace a document with a chat answer."""
    from app.team.service import TeamError
    if not tool:
        return current
    if (not current or previous.get("employee")!=current.get("employee") or
            previous.get("contact")!=current.get("contact") or
            (current.get("target_url") and current["target_url"]!=previous.get("target_url")) or
            (choice is not None and required_tool({"action":choice})!=tool)):
        raise TeamError("changed_delivery","返工需要沿用原飞书目标和交付要求。")
    return previous


def delivery_contract(pin, delivery, original):
    from app.team import service
    if delivery is None:
        return pin
    if delivery=="create_document":
        if not isinstance(original,str) or not original.strip():
            raise service.TeamError("invalid_input","请保留老板要求创建文档的原消息。")
        return {**pin,"action":delivery,"original":original,"expected_tool":"feishu_create_document"}
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
        return {**pin,"expected_tool":"feishu_read_history","read_only":True}
    return {**pin,"action":delivery,"original":original or "","expected_tool":"feishu_publish_knowledge"}
