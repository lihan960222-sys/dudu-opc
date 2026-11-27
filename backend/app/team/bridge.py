"""One turn's mailbox and atomic approval consumption."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from sqlalchemy import select, update
from app.db.models import Approval, Turn, Agent
from app.db.transactions import begin_write
from app.team import fence, management, notices, policy, service, setup


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


async def consume_allowed_approval(db, turn_id, owner_token, tool, arguments, *, return_id=False):
    await begin_write(db)
    turn = await db.get(Turn, turn_id)
    from app.services.feature_tasks import turn_assignment_current
    if (turn is None or turn.status != 'running' or turn.owner_token != owner_token
            or not await turn_assignment_current(db, turn.thread_id, turn.id, lock=True)):
        await db.rollback()
        return False
    threshold = service.now() - timedelta(minutes=15)
    query = select(Approval).where(
        Approval.turn_id == turn_id, Approval.tool == tool, Approval.status == 'allowed',
        Approval.consumed_at.is_(None), Approval.decided_at >= threshold)
    if tool in ('mcp__team__run_project_command','mcp__team__list_project_files','mcp__team__read_project_text','mcp__team__replace_project_text','mcp__team__create_project_text','mcp__team__inspect_local_path',
                'mcp__team__read_task_reference','mcp__team__download_task_reference','mcp__team__deliver_project_file','mcp__team__use_project_release'):
        query = query.where(Approval.delivered_at.is_not(None))
    approvals = (await db.execute(query.order_by(Approval.created_at))).scalars()
    expected = canonical(arguments)
    for approval in approvals:
        if canonical(approval.input) != expected:
            continue
        claimed = await db.execute(update(Approval).where(Approval.id == approval.id, Approval.consumed_at.is_(None), Approval.status == 'allowed').values(consumed_at=service.now()))
        await db.commit()
        if claimed.rowcount == 1:
            return approval.id if return_id else True
        return False
    await db.rollback()
    return False


class TeamBridge:
    def __init__(self, runtime, run):
        self.runtime, self.run = runtime, run
        # A fresh private folder outside all agent workspaces. Not backed up.
        self.folder = Path(tempfile.mkdtemp(prefix=f'suparagent-team-{run.turn_id}-'))
        self.task = None
        self.calls = set()

    @property
    def launch(self):
        extra = ['--escalations'] if notices.escalations_on() else []
        if getattr(self.run.ctx.team, 'is_lead', False) and getattr(self.run.ctx.team, 'strict_hierarchy', False):
            extra.append('--steward')
        if getattr(self.run.ctx.team, 'feature_consult', False):
            extra.append('--project-consult')
        if getattr(self.run.ctx.team, 'feature_consult', False) and not self.run.ctx.team.delegation_enabled:
            extra.append('--employee-only')
        if getattr(self.run.ctx.team, 'scheduled_knowledge', False):
            extra.append('--feishu-only')
        from app.services import employee_terminal
        flags = {'run_project_command':'--project-command','read_project_text':'--project-read',
                 'replace_project_text':'--project-edit'}
        allowed = employee_terminal.allowed_tools(self.run.ctx)
        if "list_project_files" in allowed and self.run.ctx.project_runtime.writable and self.run.ctx.approval_mode != "plan":
            extra.append("--project-release")
        extra.extend(flag for name, flag in flags.items() if name in allowed)
        if "inspect_local_path" in allowed:
            extra.append("--local-inspection")
        if "create_project_text" in allowed:
            extra.append("--project-create")
        if "list_peer_files" in allowed:
            extra.append("--peer-files")
        for tool in getattr(self.run.ctx.team, 'feishu_tools', ()):
            extra.extend(['--employee-tool', tool])
        if getattr(sys, 'frozen', False):
            return SimpleNamespace(command=sys.executable, args=['--team-mcp', '--mailbox', str(self.folder), *extra], env={})
        root = str(Path(__file__).resolve().parents[2])
        return SimpleNamespace(command=sys.executable, args=['-m', 'app.team.mcp_server', '--mailbox', str(self.folder), *extra], env={'PYTHONPATH': root})

    def start(self):
        self.task = asyncio.create_task(self.poll())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for task in self.calls:
            task.cancel()
        await asyncio.gather(*self.calls, return_exceptions=True)
        shutil.rmtree(self.folder, ignore_errors=True)

    async def poll(self):
        while not self.run.ctx.aborted:
            for path in self.folder.glob('req-*.json'):
                claimed = path.with_suffix('.working')
                try:
                    path.rename(claimed)
                except FileNotFoundError:
                    continue
                task = asyncio.create_task(self.handle(claimed))
                self.calls.add(task)
                task.add_done_callback(self.calls.discard)
            await asyncio.sleep(.1)

    async def handle(self, path):
        try:
            if path.stat().st_size > 64000:
                raise service.TeamError('too_large', 'The team request is too large.')
            request = json.loads(path.read_text(encoding='utf-8'))
            name, args = request['name'], request['arguments']
            if not isinstance(name, str) or not isinstance(args, dict):
                raise ValueError('Invalid request')
            if getattr(self.run,"recovery_call",None) is not None:
                from app.feishu.dispatch_recovery import canonical_call
                args=canonical_call(self.run.recovery_call,"mcp__team__"+name,args)
                if args is None:
                    raise service.TeamError("recovery_scope","仅可续办原工单指定的派工。")
            if followup := getattr(self.run,"recovery_followup",None):
                from app.feishu.dispatch_recovery import permits_followup
                if not permits_followup(followup,"mcp__team__"+name,args):
                    raise service.TeamError("recovery_scope","只可处理原工单当前的问题或审批。")
            async with self.runtime.session() as db:
                allowed = await consume_allowed_approval(db, self.run.turn_id, self.run.owner_token, 'mcp__team__' + name, args, return_id=True)
            if not allowed:
                result = {'error': 'This call has no matching, unused approval.', 'code': 'not_approved'}
            else:
                result = await self.execute(name, args, approval_id=allowed)
        except service.TeamError as exc:
            result = {'error': str(exc), 'code': exc.code}
        except Exception:
            result = {'error': 'The team call could not be completed.', 'code': 'team_error'}
        if getattr(self.run.ctx, "project_runtime", None) or getattr(getattr(self.run.ctx, "team", None), "feishu_tools", ()):
            from app.services.employee_resources import model_result
            result = model_result(result)
        response = self.folder / path.name.replace('req-', 'resp-').replace('.working', '.json')
        tmp = response.with_suffix('.tmp')
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, response)
        path.unlink(missing_ok=True)
        if getattr(self.run,"recovery_call",None) is not None and "error" in result:
            self.run.ctx.abort()

    async def execute(self, name, args, *, approval_id=None):
        rt, run = self.runtime, self.run
        if name in ("use_project_release", "publish_shared_release"):
            from app.services import shared_library
            allowed, _ = policy.automatic("mcp__team__" + name, run.ctx.approval_mode, run.ctx.team, args)
            if allowed != "allowed":
                raise service.TeamError("outside_shared_library", "此回合不能更改依赖。")
            if name == "use_project_release":
                return await shared_library.use_release(rt, run, args)
            async def require_steward(db):
                team = await service.team_for(db, rt.owner_id, lock=True)
                turn = await db.get(Turn, run.turn_id, with_for_update=True)
                if (not team or team.id != run.ctx.team.team_id or team.lead_agent_id != run.agent_id
                        or not turn or turn.owner_token != run.owner_token or turn.status != "running"):
                    raise service.TeamError("outside_shared_library", "只有当前在岗的大管家可以登记共享版本。")
            try:
                async with rt.session() as db:
                    await require_steward(db)
                spec = shared_library.ReleaseRequest(**args)
                release, data = await asyncio.to_thread(shared_library.resolve_request, rt.owner_id, spec)
                if data is None:
                    data = await asyncio.to_thread(shared_library.prefetch, rt.owner_id, release)
                async with rt.session() as db:
                    await begin_write(db)
                    await require_steward(db)
                    return await asyncio.to_thread(shared_library.publish, rt.owner_id, release, fetched=data)
            except (ValueError, OSError) as exc:
                raise service.TeamError("shared_release_unavailable", str(exc)) from None
        from app.services import employee_resources
        if name in (employee_resources.READ, employee_resources.DOWNLOAD, employee_resources.DELIVER):
            return await employee_resources.execute(rt, run, name, args)
        if name in ('list_project_files','read_project_text','replace_project_text','create_project_text','inspect_local_path','list_peer_files','read_peer_text'):
            from app.core.config import settings
            from app.services import employee_project_files, employee_runtime, employee_terminal, team_workspaces, workspace_files
            from app.team.steward_review import current_native_job

            writing = name in ('replace_project_text','create_project_text')
            peer_read = name in employee_terminal.PEER_READ_TOOLS
            async with rt.session() as db:
                await begin_write(db)
                job = await current_native_job(db, run, write=writing, lock=True)
                if (not job or name not in employee_terminal.allowed_tools(run.ctx, live=True)
                        or not getattr(run, 'recorder', None) or run.recorder.lost):
                    raise service.TeamError('outside_scope','工单归属或项目权限已变更。')
                try:
                    project = await employee_runtime.checked_active_project(db, run)
                    root, scope = project.root, 'role'
                    if name == 'inspect_local_path':
                        result = await asyncio.to_thread(employee_project_files.inspect_local,project,args)
                        root, scope = Path(result["path"]).anchor, "local"
                    elif peer_read:
                        listing = name == 'list_peer_files'
                        options = dict(args)
                        agent_id = options.pop('agent_id', None)
                        path = options.pop('path', None)
                        allowed_options = {'limit', 'folders_only'} if listing else set()
                        if type(agent_id) is not int or not isinstance(path, str) or options.keys() - allowed_options:
                            raise ValueError('请指定同事编号和岗位目录内的相对路径。')
                        team = await service.team_for(db, rt.owner_id)
                        peers = await team_workspaces.turn_peers(db, team, project)
                        target = peers.get(agent_id)
                        if not target:
                            raise ValueError('这名同事不在本轮可读的岗位目录中。')
                        root, scope = Path(target['role_path']), f"employee:{target['id']}"
                        result = await asyncio.to_thread(employee_project_files.inspect_workspace, root, path,
                                                         listing=listing, **options)
                    elif name == 'list_project_files':
                        if args.keys() - {'path','limit','folders_only'}:
                            raise ValueError('只可选择岗位内的相对目录、返回条数和是否仅列文件夹。')
                        result = await asyncio.to_thread(employee_project_files.inspect_workspace, root,
                            args.get('path',''), listing=True, own=True, limit=args.get('limit',50),
                            folders_only=args.get('folders_only',False))
                    elif name == 'create_project_text':
                        result = await asyncio.to_thread(employee_project_files.create,project,args)
                    elif writing:
                        result = await asyncio.to_thread(employee_project_files.edit,project,args,storage=settings.SQLITE_FILE.parent)
                    else:
                        result = await asyncio.to_thread(employee_project_files.read,project,args)
                except (OSError, UnicodeError, ValueError, workspace_files.FileAccessError) as exc:
                    raise service.TeamError('project_file_unavailable', str(exc)) from exc
                await db.commit()
            receipt = {'operation':'write_text' if writing else 'read_text', 'scope':scope,
                       'path':result['path'], 'root':str(root)}
            if name in ('list_peer_files','list_project_files'):
                receipt.update(operation='list_files', count=len(result['entries']), truncated=result['truncated'])
            if name == 'inspect_local_path':
                receipt.update(operation={"list":"list_files", "find":"list_files", "read":"read_text", "metadata":"metadata"}[args["operation"]])
                if args["operation"] == "find":
                    receipt.update(count=result["matched"], truncated=result["truncated"], scan_complete=result["scan_complete"])
                if result.get("contents_hidden"):
                    receipt["operation"] = "metadata"
            if writing:
                receipt['sha256'] = result['sha256']
            if not await run.recorder.emit('tool.completed', {'call_id':'project-file-' + os.urandom(16).hex(),
                    'name':name, 'ok':True, 'truncated':False,'output':'','file_receipts':[receipt]}):
                raise service.TeamError('receipt_lost','文件操作已结束，工单回执未保存，请先核对文件。')
            if getattr(project, "local_workstation", False):
                from app.services.agent_memory import redact_json
                result = redact_json(result)
            return result
        if name == 'run_project_command':
            from app.db.models import AgentJobEvent, Event
            from app.services import employee_check_review, employee_runtime, employee_terminal
            from app.team.steward_review import current_native_job
            if not employee_terminal.available(run.ctx, live=True):
                raise service.TeamError('outside_scope','这项派工未授权项目命令。')
            async with rt.session() as db:
                await begin_write(db)
                job = await current_native_job(db, run, write=True, lock=True)
                if not job:
                    raise service.TeamError('outside_scope','工单归属或项目权限已变更。')
                try:
                    await employee_runtime.checked_active_project(db, run)
                except (OSError, ValueError) as exc:
                    raise service.TeamError('outside_scope', str(exc)) from exc
                approval = await db.get(Approval, approval_id) if approval_id else None
                if (not approval or approval.turn_id != run.turn_id or approval.thread_id != run.thread_id
                        or approval.tool != 'mcp__team__run_project_command' or approval.status != 'allowed'
                        or not approval.consumed_at or not approval.delivered_at or canonical(approval.input) != canonical(args)):
                    raise service.TeamError('not_approved', '这次命令没有已送达的对应决定。')
                trace = await db.scalar(select(Event.data).where(Event.turn_id == run.turn_id, Event.type == 'approval.resolved',
                    Event.data['approval_id'].as_string() == approval.id).order_by(Event.id.desc()).limit(1))
                if not trace or trace.get('status') != 'allowed':
                    raise service.TeamError('not_approved', '找不到这次命令的决定来源。')
                scope = None
                if trace.get('by') == employee_check_review.SOURCE:
                    try:
                        scope = await employee_check_review.current(db, run, args)
                    except (OSError, ValueError) as exc:
                        raise service.TeamError('scope_changed', '现在无法核实验收脚本或岗位目录。') from exc
                    if not scope or scope != trace.get('project_check'):
                        raise service.TeamError('scope_changed', '原验收命令、脚本或岗位权限已变更。')
                elif (trace.get('by') != 'person' or trace.get('assignment_epoch') != job.assignment_epoch
                      or trace.get('employee_id') != job.assignee_agent_id):
                    raise service.TeamError('not_approved', '这条命令需要对应的人员决定。')
                check = await db.scalar(select(AgentJobEvent.detail).where(AgentJobEvent.job_id==job.id,
                    AgentJobEvent.kind=='completion_check').limit(1))
                await db.commit()
            try:
                return await rt.adapters['codex'].run_project_command(run.ctx, args, check, scope=scope)
            except ValueError as exc:
                raise service.TeamError('project_command_failed',str(exc)) from exc
        if name in ("employee_capabilities","enable_local_skill"):
            from app import composio_tools
            from app.services import agent_skills, shared_library
            allowed, _ = policy.automatic("mcp__team__"+name,run.ctx.approval_mode,run.ctx.team,args)
            if allowed!="allowed":
                raise service.TeamError("outside_skill_scope","当前任务不能启用技能。")
            async with rt.session() as db:
                caller=await service.member(db,rt.owner_id,run.agent_id,team_id=run.ctx.team.team_id)
                turn=await db.get(Turn,run.turn_id)
                if not turn or turn.owner_token!=run.owner_token or turn.status!="running":
                    raise service.TeamError("parent_gone","当前任务已结束。")
                project = getattr(run.ctx, "project_runtime", None)
                if project:
                    from app.team.steward_review import current_native_job
                    if not await current_native_job(db, run):
                        raise service.TeamError("parent_gone", "这项派工已结束或已交给其他员工。")
                if name=="employee_capabilities":
                    from app.services import employee_terminal, team_workspaces
                    from app.team.feishu_tools_bridge import grant
                    from app.team.mcp_server import FEISHU_TOOLS
                    management.strict(args,set())
                    feishu_tools=set(run.ctx.team.feishu_tools)
                    if feishu_tools:
                        team=await service.team_for(db,rt.owner_id)
                        current=await grant(db,rt.owner_id,run,team)
                        feishu_tools.intersection_update(current.tools if current else ())
                    connector_tools = run.ctx.composio_exact_tools
                    if connector_tools is None and run.ctx.composio_launch is not None:
                        connector_tools = [item["name"] for item in composio_tools.mcp_tools(run.ctx.composio_apps or {})]
                    peers = []
                    peer_tools = set()
                    if project and "list_peer_files" in employee_terminal.allowed_tools(run.ctx, live=True):
                        team = await service.team_for(db, rt.owner_id)
                        peers = list((await team_workspaces.turn_peers(db, team, project)).values())
                        if peers:
                            peer_tools.update(employee_terminal.PEER_READ_TOOLS)
                    elif not project and getattr(run.ctx, "peer_workspaces", None):
                        from app.services.feature_files import FeatureFiles
                        files = FeatureFiles(Path(run.ctx.feature_workspace), False, run.ctx.peer_workspaces)
                        for scope, path in files.peer_roots().items():
                            _, who, display = scope.split(":", 2)
                            peers.append({"id": int(who), "name": fence.line(display), "role_path": str(path),
                                          "scope": scope})
                    saved = [{"template_id":"employee", "name":skill.name, "description":skill.description,
                              "source":skill.source, "revision":skill.revision}
                             for skill in agent_skills.list_skills(caller.id) if skill.enabled]
                    return {**agent_skills.bundled_catalog(caller.id), "saved_skills":saved,
                            "shared_releases": [{**r, **({"template_id":"shared@" + r["sha256"]} if r["kind"] == "skill" else {})}
                                                for r in shared_library.catalog(rt.owner_id)],
                            "project_releases": shared_library.project_entries(rt.owner_id, project.root) if project else [],
                            "role_path":caller.role_path, "read_only_peers":peers,
                            "feishu_tool_details":{t["name"]:t["description"] for t in FEISHU_TOOLS
                                                   if t["name"] in feishu_tools},
                            "project_skills":[s for s in agent_skills.project_skill_catalog(project.root, caller.id) if s["enabled"]] if project else [],
                            "granted_tools":sorted(
                        feishu_tools|peer_tools|set(run.ctx.community_tools)|set(connector_tools or []))}
                management.strict(args,{"template_id","name"})
                try:
                    template = args.get("template_id")
                    if isinstance(template, str) and template.startswith("shared@"):
                        skill = shared_library.skill(rt.owner_id, None, args.get("name"), template[7:])
                    elif template == "shared" and project:
                        skill = shared_library.skill(rt.owner_id, project.root, args.get("name"))
                    else:
                        skill=agent_skills.selected_employee_skill(caller.id,args.get("name"),args.get("template_id"),
                                                                    project.root if project else None)
                except agent_skills.SkillError as exc:
                    raise service.TeamError(exc.code,exc.message) from None
                if getattr(run, "recorder", None):
                    await run.recorder.emit("skill.used", {"skills":[skill.name], "source":skill.source,
                                                          "revision":skill.revision})
                return {"enabled":True,"name":skill.name,"body":skill.body,"source":skill.source,"revision":skill.revision}
        if name.startswith('feishu_'):
            from app.team.feishu_tools_bridge import execute
            return await execute(rt, run, name, args)
        if name in ('propose_direct_report','review_direct_report'):
            from app.team import staff_hiring
            return await staff_hiring.call(rt,run,name,args)
        if name == 'refresh_today':
            async with rt.session() as db:
                caller = await service.member(db, rt.owner_id, run.agent_id, team_id=run.ctx.team.team_id)
                team = await service.team_for(db, rt.owner_id)
                if team is None or team.id != run.ctx.team.team_id or team.lead_agent_id != caller.id:
                    raise service.TeamError('lead_only', 'Only the current team lead can read this snapshot.')
            from app.routers.needs_you import service_for
            return await service_for(rt).read(refresh=True)
        async with rt.session() as db:
            caller = await service.member(db, rt.owner_id, run.agent_id, team_id=run.ctx.team.team_id)
            if run.ctx.team.feature_consult:
                from app.db.models import Thread
                from app.services import feature_tasks
                team = await service.team_for(db, rt.owner_id)
                turn = await db.get(Turn, run.turn_id)
                thread = await db.get(Thread, run.thread_id)
                if not team or not turn or turn.owner_token != run.owner_token:
                    raise service.TeamError('parent_gone', 'This consultation turn is no longer active.')
                _, peers = await feature_tasks.consult_scope(db, rt.owner_id, team, caller, turn, thread)
                if name == 'list_agents':
                    children = await db.scalars(select(Agent).where(Agent.owner_id == rt.owner_id,
                        Agent.team_id == team.id, Agent.manager_agent_id == caller.id, Agent.archived_at.is_(None)))
                    roster = {p.id:p for p in [*peers,*children]}
                    return {'agents':[{'id':p.id, 'name':fence.line(p.name), 'title':fence.line(p.title),
                                       'direct_report':p.manager_agent_id == caller.id} for p in roster.values()]}
                if name not in ('delegate','answer_job_question') and (name != 'ask_agent' or args.get('mode') != 'consult'):
                    raise service.TeamError('consult_only','Only same-project consultation is available.')
            elif name == 'list_agents':
                from app.services import team_workspaces
                team = await service.team_for(db, rt.owner_id)
                directory = await team_workspaces.load(db, team)
                rows = (await db.execute(select(Agent).where(Agent.team_id == caller.team_id, Agent.owner_id == rt.owner_id, Agent.archived_at.is_(None)).order_by(Agent.id))).scalars()
                return {'agents': [{'id': a.id, 'name': fence.line(a.name), 'title': fence.line(a.title),
                                    'can_delegate': a.delegation_enabled, **directory.profile(a, caller.id)} for a in rows]}
        if name in management.TOOLS:
            return await management.call(rt, run, name, args)
        if name in setup.TOOLS:
            return await setup.call(rt, run, name, args)
        if name == 'ask_agent':
            return await rt.team_jobs.ask(run, args)
        if name == 'delegate':
            return await rt.team_jobs.delegate(run, args)
        if name == 'review_employee_write':
            from app.team import steward_review
            return await steward_review.decide(rt,run,args)
        if name == 'answer_job_question':
            return await rt.team_jobs.answer_question(run, args)
        if name == 'job_status':
            return await rt.team_jobs.job_status(run, args)
        if name == 'retry_thread':
            return await rt.notices.retry_thread(run, args)
        if name == 'raise_escalation':
            return await rt.notices.raise_escalation(run, args)
        if name in ('list_rooms', 'create_room', 'say_in_room'):
            return await rt.rooms.tool(run, name, args)
        raise service.TeamError('unknown_tool', 'Unknown team tool.')
