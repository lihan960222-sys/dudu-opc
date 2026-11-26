"""Verify scoped work against its assigned output, using bounded host receipts."""
import hashlib
import json
from pathlib import Path, PureWindowsPath

from sqlalchemy import select
from app.db.models import Agent, AgentJob, AgentJobEvent, Event, Thread, WorkItem
from app.db.models.feature import FeatureMessage
from app.services import feature_files
from app.team import service


def contract(value):
    if value is None:
        return {"kind":"report"}
    if not isinstance(value,dict) or set(value)-{"kind","files"} or value.get("kind") not in ("report","inspection","files"):
        raise service.TeamError("invalid_input","交付方式应为答复、查阅或任务文件。")
    if value["kind"]!="files":
        if "files" in value:
            raise service.TeamError("invalid_input","仅文件交付需要文件路径。")
        return {"kind":value["kind"]}
    files=value.get("files")
    if not isinstance(files,list) or not 1<=len(files)<=16:
        raise service.TeamError("invalid_input","文件交付需要 1 至 16 条员工目录内的相对路径。")
    out=[]
    for item in files:
        if (not isinstance(item,dict) or set(item)!={"scope","path"} or item["scope"] not in ("task","role")
                or not isinstance(item["path"],str) or not 0<len(item["path"])<=400):
            raise service.TeamError("invalid_input","每个文件都需要目录范围和相对路径。")
        path=PureWindowsPath(item["path"])
        if (path.anchor or not path.parts or any(part.startswith(".") or part.casefold() in feature_files.PRIVATE
                or feature_files.ntpath.isreserved(part) or any(char in part for char in ':*?"<>|')
                or part.endswith((" ",".")) for part in path.parts)
                or item["scope"]=="role" and path.parts[0]=="任务"):
            raise service.TeamError("invalid_input","文件超出员工可用范围。")
        clean={"scope":item["scope"],"path":path.as_posix()}
        if clean not in out:
            out.append(clean)
    return {"kind":"files","files":out}


def local_receipt(call, result):
    """Accept only typed replies from our isolated file tool, never model prose."""
    if not call or call[0]!="feature_files" or not isinstance(result,dict) or result.get("isError"):
        return None
    name,args=call[1:]
    try:
        data=json.loads(result["content"][0]["text"])
        path=PureWindowsPath(args["path"]).as_posix()
        scope=args.get("scope","task")
        receipt={"scope":scope,"path":path,"operation":name}
        if name=="list_files" and isinstance(data["entries"],list):
            return {**receipt,"count":len(data["entries"])}
        if name=="read_text":
            body=data["text"].encode("utf8")
        elif name=="write_text":
            body=args["text"].encode("utf8")
            if data["bytes"]!=len(body):
                return None
        else:
            return None
        return {**receipt,"sha256":hashlib.sha256(body).hexdigest(),"bytes":len(body)}
    except (KeyError,IndexError,TypeError,ValueError,AttributeError):
        return None


async def assess(db,job,turn,check):
    text=(turn.result or "").strip()
    if text.startswith("BLOCKED:"):
        return {"state":"blocked","detail":text[8:].strip()[:500] or "员工报告阻塞，尚未交付。"}
    receipts=await db.scalars(select(Event.data).where(Event.turn_id==turn.id,
        Event.type=="tool.completed").order_by(Event.id.desc()))
    evidence=[r["file_receipt"] for r in receipts if r.get("file_receipt")]
    if not text:
        return {"state":"missing","detail":"员工尚未交付答复。"}
    if check["kind"]=="report":
        return {"state":"reported","evidence":evidence[:12]}
    if check["kind"]=="inspection":
        seen=[r for r in evidence if r["operation"] in ("list_files","read_text")]
        if seen:
            return {"state":"inspected","evidence":seen[:12]}
        return {"state":"missing","detail":"没有本轮查阅项目文件或目录的成功记录。"}
    thread=await db.get(Thread,job.assignee_thread_id)
    agent=await db.get(Agent,job.assignee_agent_id)
    try:
        fs=feature_files.FeatureFiles(Path(thread.workspace_path))
        if (not agent.role_path or Path(agent.role_path)!=fs.role or agent.owner_id!=job.owner_id
                or thread.agent_id!=agent.id or fs.task.name!=str(job.task_id)):
            raise ValueError()
        from app.services.feature_tasks import check_employee_root
        await check_employee_root(db,job.owner_id,agent,fs.role)
    except (OSError,ValueError,TypeError):
        return {"state":"blocked","detail":"工作目录绑定已变更，无法核对交付文件。"}
    verified=[]
    for item in check["files"]:
        actual=next((r for r in evidence if r["scope"]==item["scope"] and r["path"].casefold()==item["path"].casefold()
                    and "sha256" in r),None)
        try:
            data=fs.call("read_text",item)["text"].encode("utf8")
            digest=hashlib.sha256(data).hexdigest()
        except (OSError,ValueError):
            digest=None
        if not actual or not digest or actual["sha256"]!=digest:
            return {"state":"missing","detail":"缺少与当前内容一致的文件回执："+item["scope"]+"/"+item["path"]}
        verified.append(actual)
    return {"state":"files_verified","evidence":verified}


async def queue_verification(db,job,turn,detail):
    """One read-only pass in the existing task, never repeat external effects."""
    prior = await db.scalar(select(AgentJobEvent.id).where(AgentJobEvent.job_id==job.id,
                            AgentJobEvent.kind=="verification_retry").limit(1))
    item=await db.get(WorkItem,job.task_id)
    if prior or not item or item.archived or item.completed_at or item.thread_id!=job.assignee_thread_id:
        return False
    newer=await db.scalar(select(AgentJob.id).where(AgentJob.owner_id==job.owner_id,
        AgentJob.task_id==job.task_id,AgentJob.assignee_agent_id==job.assignee_agent_id,
        AgentJob.id>job.id).limit(1))
    if newer:
        return False
    job.origin_ref=f"team-job-{job.id}-verify-{turn.id}"
    job.answer="仅核对上次工作的结果。"+detail+" 用只读工具检查原交付物，找到后简要汇报；确实缺失则用 BLOCKED: 说明。"
    db.add(FeatureMessage(owner_id=job.owner_id,event_key=job.origin_ref,agent_id=job.assignee_agent_id,
        project=job.project,text=job.answer,result={"task_id":job.task_id,"job_id":job.id}))
    db.add(AgentJobEvent(job_id=job.id,kind="verification_retry",detail={"previous_turn":turn.id,"reason":detail}))
    job.status,job.approval_mode,job.current_turn_id,job.assignee_turn_id="queued","plan",None,None
    return True
