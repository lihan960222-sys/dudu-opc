"""Verify scoped work against its assigned output, using bounded host receipts."""
import hashlib
import json
import re
from pathlib import Path, PureWindowsPath

from sqlalchemy import select
from app.db.models import Agent, AgentJob, AgentJobEvent, Event, Thread, WorkItem
from app.db.models.feature import FeatureMessage
from app.services import feature_files
from app.team import service


def contract(value):
    if value is None:
        return {"kind":"report"}
    if isinstance(value,dict) and value.get("kind")=="commands":
        entries=value.get("commands")
        if set(value)-{"kind","commands","files"} or not isinstance(entries,list) or not 1<=len(entries)<=8:
            raise service.TeamError("invalid_input","请列出 1 至 8 条用于验收的命令。")
        checks=[]
        for entry in entries:
            if not isinstance(entry,dict) or set(entry)-{"command","cwd","contains"}:
                raise service.TeamError("invalid_input","命令验收需要命令和项目内的相对目录。")
            command=entry.get("command")
            directory=entry.get("cwd",".")
            assertion=entry.get("contains","")
            if (not isinstance(command,str) or not 1<=len(command.strip())<=600
                    or not isinstance(directory,str) or len(directory)>400 or not directory or PureWindowsPath(directory).anchor
                    or ".." in PureWindowsPath(directory).parts
                    or not isinstance(assertion,str) or len(assertion)>160):
                raise service.TeamError("invalid_input","请核对命令、相对目录和预期输出。")
            checks.append({"command":command.strip(),"cwd":PureWindowsPath(directory).as_posix(),
                           **({"contains":assertion} if assertion else {})})
        result={"kind":"commands","commands":checks}
        if "files" in value:
            result["files"]=contract({"kind":"files","files":value["files"]})["files"]
        return result
    if not isinstance(value,dict) or set(value)-{"kind","files"} or value.get("kind") not in ("report","inspection","files"):
        raise service.TeamError("invalid_input","请选择答复、查阅、文件或运行命令作为交付方式。")
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


def _project_path(raw, root):
    if not isinstance(raw,str) or not raw or ".." in PureWindowsPath(raw).parts:
        raise ValueError("项目路径无效。")
    root=feature_files.checked(Path(root))
    path=Path(raw)
    relative=(path if path.is_absolute() else root/path).relative_to(root)
    current=root
    for part in relative.parts:
        if (part.endswith((" ",".")) or feature_files.ntpath.isreserved(part)
                or any(char in part for char in ':*?"<>|')):
            raise ValueError("项目路径无效。")
        current=feature_files.checked(current/part)
    return current, relative.as_posix()


def command_receipt(item, root):
    if (item.get("type")!="commandExecution" or not isinstance(item.get("id"),str)
            or not isinstance(item.get("command"),str) or not item["command"].strip()
            or item.get("status") not in ("completed","failed","declined","cancelled","interrupted")):
        return None
    try:
        cwd,relative=_project_path(item.get("cwd"),root)
        if not cwd.is_dir():
            return None
    except (OSError,ValueError):
        return None
    code=item.get("exitCode")
    receipt = {"command":item["command"],"cwd":relative,"root":str(root),"exit_code":code if type(code)==int else None,
               "status":item["status"],"call_id":item["id"]}
    actions = item.get("commandActions")
    if receipt["exit_code"] != 0 or receipt["status"] != "completed" or not isinstance(actions,list) or len(actions)!=1:
        return receipt
    action = actions[0]
    if not isinstance(action,dict) or action.get("type") not in ("read","listFiles","search"):
        return receipt
    if not isinstance(action.get("command"),str) or not _matches_command(item["command"],action["command"]):
        return receipt
    raw = action.get("path")
    if raw is None and action["type"] in ("listFiles","search"):
        raw = "."
    if not isinstance(raw,str) or not raw:
        return receipt
    try:
        path, name = _project_path(str(cwd / raw), root)
        if ((action["type"]=="read" and path.is_file()) or
                (action["type"]=="listFiles" and path.is_dir()) or
                (action["type"]=="search" and (path.is_dir() or path.is_file()))):
            receipt["inspection"] = {"operation":action["type"],"path":name,"scope":"role","root":str(root)}
    except (OSError,ValueError):
        pass
    return receipt


def _content_digest(path):
    if not path.is_file() or path.stat().st_size>256*1024*1024:
        raise ValueError("交付物过大或已移走，需单独核验。")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def file_change_receipts(item, root):
    if item.get("type")!="fileChange" or item.get("status")!="completed" or not isinstance(item.get("changes"),list):
        return []
    results=[]
    for change in item["changes"]:
        try:
            if not isinstance(change,dict):
                continue
            path,relative=_project_path(change.get("path"),root)
            results.append({"scope":"role","path":relative,"operation":"fileChange","root":str(root),
                            "sha256":_content_digest(path)})
        except (OSError,ValueError):
            continue
    return results


def _matches_command(command, wanted):
    if command.strip()==wanted:
        return True
    # The Windows host may include its PowerShell launcher in the event.
    launcher=re.fullmatch(r'''(?is)(?:"[^"]*(?:powershell|pwsh)\.exe"|(?:[^"\s]*[\\/])?(?:powershell|pwsh)(?:\.exe)?)\s+(?:(?:-NoLogo|-NoProfile|-NonInteractive)\s+)*-Command\s+"([^"]*)"''',command.strip())
    return bool(launcher and launcher[1]==wanted)


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
    receipts=list(await db.scalars(select(Event.data).where(Event.turn_id==turn.id,
        Event.type=="tool.completed").order_by(Event.id.desc())))
    evidence=[e for r in receipts for e in ([r["file_receipt"]] if r.get("file_receipt") else r.get("file_receipts",[]))]
    if not text:
        return {"state":"missing","detail":"员工尚未交付答复。"}
    if check["kind"]=="report":
        return {"state":"reported","evidence":evidence[:12]}
    if check["kind"]=="inspection":
        seen=[r for r in evidence if r["operation"] in ("list_files","read_text")]
        native = [r["command_receipt"]["inspection"] for r in receipts if (r.get("command_receipt") or {}).get("inspection")]
        if native:
            worker = await db.get(Agent,job.assignee_agent_id)
            if worker and worker.owner_id==job.owner_id and worker.role_path:
                seen.extend(item for item in native if Path(item["root"])==Path(worker.role_path))
        if seen:
            return {"state":"inspected","evidence":seen[:12]}
        return {"state":"missing","detail":"没有本轮查阅项目文件或目录的成功记录。"}
    thread=await db.get(Thread,job.assignee_thread_id)
    agent=await db.get(Agent,job.assignee_agent_id)
    try:
        root=feature_files.checked(Path(agent.role_path))
        project=Path(thread.workspace_path)==root
        fs=None if project else feature_files.FeatureFiles(Path(thread.workspace_path))
        if (agent.owner_id!=job.owner_id or thread.agent_id!=agent.id or
                fs and (fs.role!=root or fs.task.name!=str(job.task_id))):
            raise ValueError("任务归属已变更。")
        from app.services.feature_tasks import check_employee_root
        await check_employee_root(db,job.owner_id,agent,root)
    except (OSError,ValueError,TypeError,AttributeError):
        return {"state":"blocked","detail":"工作目录绑定已变更，无法核对交付文件。"}
    if check["kind"]=="commands":
        verified=[]
        for expected in check["commands"]:
            match=next(((r["command_receipt"],r) for r in receipts if r.get("command_receipt")
                        and Path(r["command_receipt"]["root"])==root and r["command_receipt"]["cwd"]==expected["cwd"]
                        and _matches_command(r["command_receipt"]["command"],expected["command"])),None)
            if not match:
                return {"state":"missing","detail":"尚未取得本轮运行回执："+expected["command"]}
            cmd,event=match
            if cmd["exit_code"]!=0 or cmd["status"]!="completed":
                return {"state":"blocked","detail":"最后一次运行未通过："+expected["command"],"evidence":[cmd]}
            if expected.get("contains") and expected["contains"] not in event.get("output",""):
                return {"state":"missing","detail":"运行输出缺少验收要求的结果："+expected["contains"]}
            verified.append({**cmd,**({"matched":expected["contains"]} if expected.get("contains") else {})})
        if check.get("files"):
            files=await assess(db,job,turn,{"kind":"files","files":check["files"]})
            if files["state"]!="files_verified":
                return files
            verified.extend(files["evidence"])
        return {"state":"commands_verified","evidence":verified}
    verified=[]
    for item in check["files"]:
        actual=next((r for r in evidence if (r["scope"]==item["scope"] or project and r["scope"]=="role") and r["path"].casefold()==item["path"].casefold()
                    and "sha256" in r),None)
        try:
            if project:
                path,_=_project_path(item["path"],root)
                digest=_content_digest(path)
            else:
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
