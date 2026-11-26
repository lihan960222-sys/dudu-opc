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


async def consume_allowed_approval(db, turn_id, owner_token, tool, arguments):
    await begin_write(db)
    turn = await db.get(Turn, turn_id)
    from app.services.feature_tasks import turn_assignment_current
    if (turn is None or turn.status != 'running' or turn.owner_token != owner_token
            or not await turn_assignment_current(db, turn.thread_id, turn.id, lock=True)):
        await db.rollback()
        return False
    threshold = service.now() - timedelta(minutes=15)
    approvals = (await db.execute(select(Approval).where(
        Approval.turn_id == turn_id, Approval.tool == tool, Approval.status == 'allowed',
        Approval.consumed_at.is_(None), Approval.decided_at >= threshold).order_by(Approval.created_at))).scalars()
    expected = canonical(arguments)
    for approval in approvals:
        if canonical(approval.input) != expected:
            continue
        claimed = await db.execute(update(Approval).where(Approval.id == approval.id, Approval.consumed_at.is_(None), Approval.status == 'allowed').values(consumed_at=service.now()))
        await db.commit()
        return claimed.rowcount == 1
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
        if getattr(self.run.ctx.team, 'feature_consult', False):
            extra.append('--project-consult')
        if getattr(self.run.ctx.team, 'feature_consult', False) and not self.run.ctx.team.delegation_enabled:
            extra.append('--employee-only')
        if getattr(self.run.ctx.team, 'scheduled_knowledge', False):
            extra.append('--feishu-only')
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
                allowed = await consume_allowed_approval(db, self.run.turn_id, self.run.owner_token, 'mcp__team__' + name, args)
            if not allowed:
                result = {'error': 'This call has no matching, unused approval.', 'code': 'not_approved'}
            else:
                result = await self.execute(name, args)
        except service.TeamError as exc:
            result = {'error': str(exc), 'code': exc.code}
        except Exception:
            result = {'error': 'The team call could not be completed.', 'code': 'team_error'}
        response = self.folder / path.name.replace('req-', 'resp-').replace('.working', '.json')
        tmp = response.with_suffix('.tmp')
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, response)
        path.unlink(missing_ok=True)
        if getattr(self.run,"recovery_call",None) is not None and "error" in result:
            self.run.ctx.abort()

    async def execute(self, name, args):
        rt, run = self.runtime, self.run
        if name in ("employee_capabilities","enable_local_skill"):
            from app import composio_tools
            from app.services import agent_skills
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
                    management.strict(args,set())
                    connector_tools = run.ctx.composio_exact_tools
                    if connector_tools is None and run.ctx.composio_launch is not None:
                        connector_tools = [item["name"] for item in composio_tools.mcp_tools(run.ctx.composio_apps or {})]
                    saved = [{"template_id":"employee", "name":skill.name, "description":skill.description,
                              "source":skill.source, "revision":skill.revision}
                             for skill in agent_skills.list_skills(caller.id) if skill.enabled]
                    return {**agent_skills.bundled_catalog(caller.id), "saved_skills":saved,
                            "project_skills":[s for s in agent_skills.project_skill_catalog(project.root, caller.id) if s["enabled"]] if project else [],
                            "granted_tools":sorted(
                        set(run.ctx.team.feishu_tools)|set(run.ctx.community_tools)|set(connector_tools or []))}
                management.strict(args,{"template_id","name"})
                try:
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
                rows = (await db.execute(select(Agent).where(Agent.team_id == caller.team_id, Agent.owner_id == rt.owner_id, Agent.archived_at.is_(None)).order_by(Agent.id))).scalars()
                return {'agents': [{'id': a.id, 'name': fence.line(a.name), 'title': fence.line(a.title), 'can_delegate': a.delegation_enabled} for a in rows]}
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
