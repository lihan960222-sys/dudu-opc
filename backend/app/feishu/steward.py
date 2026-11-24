"""One tool-free steward inference from a DM and the local task index."""
import asyncio
import json
import tempfile
from pathlib import Path

from app.core import host_exec
from app.services.local_claude_runner import child_env


def validate_choice(proposal, roles, tasks):
    if not isinstance(proposal,dict):
        return None
    intent, role = proposal.get('intent'), proposal.get('role')
    title, task = proposal.get('title'), proposal.get('task')
    if intent=='chat' and task is None:
        return 'chat','','',None
    if role not in roles or not isinstance(title,str) or not 0 < len(title.strip()) <= 160:
        return None
    if intent in ('append','query'):
        allowed = next((t for t in tasks if t['task']==task and t['role']==role),None)
        if allowed:
            return intent,role,allowed['title'],task
        if intent=='query' and task is None:
            return intent,role,title.strip(),None
    elif intent in ('deliver','independent') and task is None:
        return intent,role,title.strip(),None
    return None


async def infer(model, instructions, roles, tasks, message, *, private_env=()):
    from app.services.codex_adapter import resolve_codex_executable
    from app.services.cli_probe import kill_group
    import sys
    exe = resolve_codex_executable()
    if not exe:
        return None
    schema = {'type':'object','additionalProperties':False,'properties':{
        'intent':{'type':'string','enum':['deliver','independent','append','query','clarify','chat']},
        'role':{'type':'string','enum':['',*roles]},'title':{'type':'string'},
        'task':{'type':['integer','null']}},'required':['intent','role','title','task']}
    role_details = roles if isinstance(roles, dict) and all(isinstance(v, str) for v in roles.values()) else list(roles)
    prompt = json.dumps({'职责':instructions,'可用角色':role_details,'现有任务':tasks,
        '用户来信':message,'本轮要求':'只判断意图和归属。查询用query，追加或修正用append，独立交付用deliver，明确另开才用independent。引用或查询现有任务必须选其编号。普通聊天、问候、向嘟嘟本人提问选chat，role留空。工作目标有多个合理候选或依据不足选clarify。'},ensure_ascii=False)
    host_exec.ensure_allowed('steward intent classification')
    with tempfile.TemporaryDirectory(prefix='suparagent-steward-') as working:
        folder = Path(working)
        (folder/'schema.json').write_text(json.dumps(schema),'utf-8')
        args = [str(exe),'exec','--ignore-user-config','--ignore-rules','--ephemeral',
                '--skip-git-repo-check','--sandbox','read-only','--model',model,
                '-c','web_search="disabled"','-c','project_doc_max_bytes=0']
        for feature in ('shell_tool','unified_exec','view_image','multi_agent','apps','plugins','code_mode'):
            args.extend(['--disable',feature])
        args.extend(['--output-schema','schema.json','--output-last-message','choice.json','-'])
        secret_names = {n.upper() for n in private_env if n}
        env = {k:v for k,v in child_env().items() if k.upper() not in secret_names}
        proc = await asyncio.create_subprocess_exec(*args,cwd=working,env=env,stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,start_new_session=sys.platform!='win32')
        try:
            await asyncio.wait_for(proc.communicate(prompt.encode('utf-8')),120)
        finally:
            if proc.returncode is None:
                await asyncio.to_thread(kill_group,proc)
                await proc.wait()
        path = folder/'choice.json'
        if proc.returncode or not path.is_file() or path.stat().st_size > 16384:
            return None
        return validate_choice(json.loads(path.read_text('utf-8')),roles,tasks)


async def classify(cfg, store, event_key, message):
    from app.db.session import AsyncSessionLocal
    from app.services import agent_store, role_workspace
    from app.team.service import team_for
    with store:
        store.execute('create table if not exists decisions(event text primary key, choice text, model text, digest text)')
    cached = store.execute('select choice from decisions where event=?',(event_key,)).fetchone()
    if cached:
        return json.loads(cached[0])
    async with AsyncSessionLocal() as db:
        team = await team_for(db,cfg['owner_id'])
        if not team or team.lead_agent_id != cfg['steward']['agent_id']:
            return None
        lead = await agent_store.get_agent(db,team.lead_agent_id)
        if not lead or lead.owner_id != cfg['owner_id'] or not lead.role_path:
            return None
        role = role_workspace.load(Path(lead.role_path))
        roster = {}
        for label, entry in cfg['roles'].items():
            member = await agent_store.get_agent(db, entry['agent_id'])
            if member and member.owner_id == cfg['owner_id']:
                roster[label] = ' '.join(filter(None, (member.name, member.title, member.description)))[:1000]
        if not roster:
            return None
    tasks = [dict(row) for row in store.execute('select task,role,title from bindings order by task desc limit 120')]
    private = [r.get('secret_env') for r in [cfg['steward'],*cfg['roles'].values()]]
    private.append(cfg.get('backend_token_env'))
    choice = await infer(role.model,role.instructions,roster,tasks,message,private_env=private)
    with store:
        store.execute('insert into decisions values(?,?,?,?)',(event_key,json.dumps(choice),role.model,role.digest))
    return choice
