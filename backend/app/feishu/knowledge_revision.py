"""Versioned topic summaries of an employee's authorized Feishu conversation."""
import hashlib
import json
from difflib import SequenceMatcher
from uuid import NAMESPACE_URL, uuid5

from . import knowledge_citations
from .private_summary import model_result
from .publisher import API, checked

TOPICS={"type":"object","properties":{"topics":{"type":"array","items":{
    "type":"object","properties":{"title":{"type":"string"},"summary":{"type":"string"},
    "sources":{"type":"array","items":{"type":"integer"}}},"required":["title","summary","sources"],
    "additionalProperties":False}}},"required":["topics"],"additionalProperties":False}


def text_block(kind, text, sources=(), chat="", records=None):
    tags={"text":2,"heading1":3,"heading2":4}
    elements=[{"text_run":{"content":text}}]
    for n,mid in enumerate(sources,1):
        elements.append(knowledge_citations.element(n, mid, records[mid], chat))
    return {"block_type":tags[kind],kind:{"elements":elements}}


def employee_blocks(topics, records, chat):
    """Turn the employee's own draft into Docx blocks with checked source links."""
    if not isinstance(topics, list) or not 1 <= len(topics) <= 100:
        raise ValueError("请提交你已整理的主题、摘要和对应的消息编号。")
    blocks = [text_block("heading1", "知识综述"),
              text_block("text", f"本版涵盖 {len(records)} 条可见消息。")]
    sections = {}
    covered = set()
    for topic in topics:
        if not isinstance(topic, dict) or set(topic) != {"title", "summary", "sources"}:
            raise ValueError("每个主题需要标题、摘要和来源。")
        title, summary, sources = (topic[k] for k in ("title", "summary", "sources"))
        if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 80
                or not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 2000
                or not isinstance(sources, list) or not sources
                or any(not isinstance(mid, str) or mid not in records for mid in sources)
                or len(sources) != len(set(sources))):
            raise ValueError("请核对主题、摘要和来源，只能引用本次读取的消息编号。")
        covered.update(sources)
        notes = sections.setdefault(title.strip(), [])
        for offset in range(0, len(sources), 32):
            notes.append(text_block("text", summary.strip() if not offset else "更多来源", sources[offset:offset+32], chat, records))
    if covered != set(records):
        raise ValueError("整理稿尚未覆盖本次所有消息，请补齐来源后重试。")
    for title, notes in sections.items():
        blocks.extend([text_block("heading2", title), *notes])
    return blocks


def batches(records):
    pending=[]
    size=0
    for mid,item in records.items():
        value=json.dumps(item.get("body"),ensure_ascii=False,sort_keys=True)
        prefix=f"{item.get('create_time','')} {item.get('msg_type','')} "
        for offset in range(0,len(value),3500):
            piece=prefix+value[offset:offset+3500]
            if pending and (size+len(piece)>10000 or len(pending)>=12):
                yield pending
                size,pending=0,[]
            pending.append((mid,piece))
            size+=len(piece)
    if pending:
        yield pending


async def summarize(records,profile,authorized,chat,instructions=""):
    sections={}
    for batch in batches(records):
        prompt=("为这批对话撰写知识库，提炼可复用事实、决策和待办，用具体主题作标题。"
            "相同主题合并，已有合适主题就沿用。每个输入序号至少归入一项，来源从1开始；"
            "无正文附件仅说明类型，不能猜内容。已有标题："+json.dumps(list(sections)[-100:],ensure_ascii=False))
        if instructions:
            prompt+="\n老板对本次整理的要求："+instructions
        result=await model_result(profile,[piece for _,piece in batch],authorized,prompt,TOPICS)
        covered=set()
        for topic in result["topics"]:
            name=topic["title"].strip()
            note=topic["summary"].strip()
            indices=topic["sources"]
            if (not 0<len(name)<=80 or not 0<len(note)<=2000 or not indices
                    or any(type(n)!=int or not 1<=n<=len(batch) for n in indices)):
                raise ValueError("模型返回的主题或来源无效，文档未更新。")
            covered.update(indices)
            sources=list(dict.fromkeys(batch[n-1][0] for n in indices))
            sections.setdefault(name,[]).append(text_block("text",note,sources,chat,records))
        if covered!=set(range(1,len(batch)+1)):
            raise ValueError("部分消息没有归入主题，文档未更新。")
    blocks=[text_block("heading1","知识综述"),text_block("text",f"本版涵盖 {len(records)} 条当前可见消息，按主题整理。")]
    for title,notes in sections.items():
        blocks.extend([text_block("heading2",title),*notes])
    return blocks


def block_signature(block):
    kind={2:"text",3:"heading1",4:"heading2",5:"heading3"}.get(block.get("block_type"))
    if not kind:
        return None
    return (kind,[(run.get("content"),knowledge_citations.decoded_link(run.get("text_element_style",{}).get("link",{}).get("url")))
                  for el in block.get(kind,{}).get("elements",[]) for run in [el.get("text_run",{})]])


def removable(block):
    # Text equality alone does not cover nested human notes or manual formatting.
    if block.get("children"):
        return False
    kind={2:"text",3:"heading1",4:"heading2"}.get(block.get("block_type"))
    if not kind:
        return False
    body=block.get(kind,{})
    if any(v for k,v in body.get("style",{}).items() if k!="align" or v!=1):
        return False
    for el in body.get("elements",[]):
        if set(el)!={"text_run"}:
            return False
        style=el["text_run"].get("text_element_style",{})
        archived = knowledge_citations.archived_ids(el["text_run"])
        if any(v for k,v in style.items() if k!="link" and not (k=="italic" and archived)):
            return False
    return True


def root_blocks(client,auth,doc):
    params={"page_size":500}
    items=[]
    seen=set()
    while True:
        data=checked(client.get(API+f"/docx/v1/documents/{doc}/blocks/{doc}/children",
                                headers=auth(),params=params,timeout=20))["data"]
        items.extend(data["items"])
        if not data.get("has_more"):
            return items
        cursor=data.get("page_token")
        if not isinstance(cursor,str) or not cursor or cursor in seen:
            raise ValueError("飞书未返回可继续读取的文档分页。")
        seen.add(cursor)
        params["page_token"]=cursor


def matches(actual,expected):
    return len(actual)>=len(expected) and all(block_signature(a)==block_signature(b)
                                               for a,b in zip(actual,expected))


def remote_digest(blocks):
    return hashlib.sha256(json.dumps(blocks,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()

async def patch_summary(db,client,auth,authorized,key,doc,version,previous,blocks):
    """Apply one changed run at a time, recording both sides of each uncertain write."""
    url=API+f"/docx/v1/documents/{doc}"
    db.execute("""create table if not exists knowledge_patch_steps(
        binding text primary key, version text, managed text, pending text, tail text)""")
    while True:
        step=db.execute("select version,managed,pending,tail from knowledge_patch_steps where binding=?",(key,)).fetchone()
        if step and step[0]!=version:
            raise ValueError("上一版局部更新尚待核对，请先恢复原稿。")
        revision=checked(client.get(url,headers=auth(),timeout=20))["data"]["document"]["revision_id"]
        current=root_blocks(client,auth,doc)
        managed=json.loads(step[1]) if step else previous
        if not step and managed and any(matches(current[i:],managed[:1]) for i in range(len(managed),len(current))):
            raise ValueError("发现重复的综述标题，无法确认应更新哪一版。")
        tail=step[3] if step else remote_digest(current[len(managed):])
        def present(plan):
            return matches(current,plan) and remote_digest(current[len(plan):])==tail
        if step and present(json.loads(step[2])):
            managed=json.loads(step[2])
        elif not present(managed):
            raise ValueError("现有综述已变化，无法安全定位本次修改的段落。")
        def tokens(plan):
            return [json.dumps(block_signature(block),ensure_ascii=False) for block in plan]
        changes=SequenceMatcher(None,tokens(managed),tokens(blocks),autojunk=False).get_opcodes()
        change=next((op for op in changes if op[0]!="equal"),None)
        if not change:
            with db:
                db.execute("update knowledge_revisions set remaining=0,status='done' where binding=? and version=?",(key,version))
                db.execute("delete from knowledge_patch_steps where binding=?",(key,))
            return
        _,start,end,new_start,new_end=change
        if not await authorized():
            raise ValueError("整理授权已变更。")
        params={"document_revision_id":revision}
        if end>start:
            if any(not removable(block) for block in current[start:end]):
                raise ValueError("待更新段落含人工格式或子内容，请先核对。")
            after=managed[:start]+managed[end:]
        else:
            added=blocks[new_start:min(new_end,new_start+30)]
            after=managed[:start]+added+managed[start:]
            params["client_token"]=str(uuid5(NAMESPACE_URL,key+version+remote_digest(managed)+str(start)))
        with db:
            transition=(version,json.dumps(managed),json.dumps(after),tail)
            if step:
                claim=db.execute("""update knowledge_patch_steps set version=?,managed=?,pending=?,tail=?
                    where binding=? and version=? and managed=? and pending=? and tail=?""",
                    (*transition,key,*step))
            else:
                claim=db.execute("insert or ignore into knowledge_patch_steps values(?,?,?,?,?)",(key,*transition))
            if not claim.rowcount:
                raise ValueError("另一次更新正在核对此文档。")
        if end>start:
            checked(client.request("DELETE",url+f"/blocks/{doc}/children/batch_delete",
                    headers=auth(),params=params,json={"start_index":start,"end_index":end},timeout=20))
        else:
            checked(client.post(url+f"/blocks/{doc}/children",headers=auth(),params=params,
                                json={"index":start,"children":added},timeout=20))
        observed=root_blocks(client,auth,doc)
        if not matches(observed,after) or remote_digest(observed[len(after):])!=tail:
            raise ValueError("飞书已接收局部更新，但读回尚未确认，已停止写入。")

async def revise_document(db,client,auth,authorized,profile,key,doc,origin,title,chat,records,*,stage=None,instructions="",layout_key=None,employee_draft=None,existing_document=False):
    snapshot=[(mid,item.get("update_time"),item.get("create_time"),item.get("msg_type"),item.get("body"))
              for mid,item in sorted(records.items())]
    db.execute("create table if not exists knowledge_layouts(binding text primary key,instructions text not null)")
    if not instructions:
        layout=db.execute("select instructions from knowledge_layouts where binding=?",(layout_key or key,)).fetchone()
        if layout:
            instructions=layout[0]
    if instructions:
        snapshot=[instructions,snapshot]
    if employee_draft is not None:
        snapshot=["employee-draft",employee_draft,snapshot]
    version=hashlib.sha256(json.dumps(snapshot,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    db.execute("""create table if not exists knowledge_revisions(
        binding text,version text,plan text,remaining integer,status text,
        primary key(binding,version))""")
    db.execute("""create table if not exists knowledge_write_attempts(
        binding text,version text,remaining integer,prior_digest text,
        primary key(binding,version,remaining))""")
    row=db.execute("select plan,remaining,status from knowledge_revisions where binding=? and version=?",
                   (key,version)).fetchone()
    link=origin+"/docx/"+doc
    if not await authorized():
        raise ValueError("请核对该员工的私聊整理和模型摘要授权。")
    if row and row[2]=="done":
        if not matches(root_blocks(client,auth,doc),json.loads(row[0])):
            raise ValueError("飞书现有综述已变化，需要核对后再更新。")
        return f"全部 {len(records)} 条消息与已写入的综述版本一致。\n《{title}》{link}",len(records)
    pending=db.execute("select version,plan,remaining from knowledge_revisions where binding=? and status='sending'",(key,)).fetchone()
    if pending:
        old_version,old_plan,remaining=pending
        before=db.execute("select prior_digest from knowledge_write_attempts where binding=? and version=? and remaining=?",
                          (key,old_version,remaining)).fetchone()
        if not before:
            raise ValueError("上次写入结果待核对，请先检查已有文档："+link)
        batch=json.loads(old_plan)[max(0,remaining-30):remaining]
        remote=root_blocks(client,auth,doc)
        if matches(remote,batch) and remote_digest(remote[len(batch):])==before[0]:
            remaining-=len(batch)
        elif remote_digest(remote)!=before[0] or old_version!=version:
            raise ValueError("文档在写入中断后发生变化，请核对已有内容："+link)
        with db:
            db.execute("""update knowledge_revisions set remaining=?,status=?
                where binding=? and version=? and status='sending'""",
                (remaining,"planned" if remaining else "verify",key,old_version))
        if old_version!=version:
            raise ValueError("已找回上次写入回执，请先核对上一版，再提交这次的整理稿。")
        row=(old_plan,remaining,"planned" if remaining else "verify")
    if row:
        blocks,remaining=json.loads(row[0]),row[1]
    else:
        if stage:
            stage("summarize",len(records))
        blocks=employee_draft if employee_draft is not None else await summarize(records,profile,authorized,chat,instructions)
        remaining=len(blocks)
        with db:
            db.execute("insert into knowledge_revisions values(?,?,?,?,'planned')",
                (key,version,json.dumps(blocks,ensure_ascii=False),remaining))
    prior=db.execute("select plan from knowledge_revisions where binding=? and version<>? and status='done' order by rowid desc limit 1",(key,version)).fetchone()
    if prior and remaining==len(blocks):
        await patch_summary(db,client,auth,authorized,key,doc,version,json.loads(prior[0]),blocks)
        with db:
            db.execute("insert or replace into knowledge_layouts values(?,?)",(layout_key or key,instructions))
        return f"已核对知识库，本次仅同步有变化的段落。\n《{title}》{link}",len(records)
    if existing_document and not prior and remaining==len(blocks):
        current=root_blocks(client,auth,doc)
        for block in current:
            signature=block_signature(block)
            if not (removable(block) and signature and signature[0]=="text" and
                    all(not text and not link for text,link in signature[1])):
                raise ValueError("现有文档有正文，但没有已核验的综述版本可供比对，已停止写入。")
    while remaining:
        start=max(0,remaining-30)
        if not await authorized():
            raise ValueError("员工的摘要授权已变更，已停止写入。")
        headers=auth()
        if stage:
            stage("write_blocks",remaining)
        before=remote_digest(root_blocks(client,auth,doc)) if employee_draft is not None else None
        with db:
            claimed=db.execute("""update knowledge_revisions set status='sending'
                where binding=? and version=? and status='planned' and remaining=?""",(key,version,remaining))
            if not claimed.rowcount:
                raise ValueError("另一项任务正在核对本次写入，请稍候。")
            if before is not None:
                db.execute("insert or replace into knowledge_write_attempts values(?,?,?,?)",(key,version,remaining,before))
        response=client.post(API+f"/docx/v1/documents/{doc}/blocks/{doc}/children",
            headers=headers,params={"client_token":str(uuid5(NAMESPACE_URL,key+version+str(start)))},
            json={"index":0,"children":blocks[start:remaining]},timeout=20)
        children=checked(response)["data"]["children"]
        if len(children)!=remaining-start or any(not child.get("block_id") for child in children):
            raise ValueError("飞书尚未确认综述写入，请核对文档："+link)
        remaining=start
        with db:
            db.execute("update knowledge_revisions set remaining=?,status=? where binding=? and version=?",
                (remaining,"planned" if remaining else "verify",key,version))
    if not instructions and employee_draft is None:
        if not matches(root_blocks(client,auth,doc),blocks):
            raise ValueError("飞书读回的正文与整理稿不一致，尚未完成。")
        with db:
            db.execute("update knowledge_revisions set status='done' where binding=? and version=?",(key,version))
    if instructions or employee_draft is not None:
        # Only remove adjacent, byte-for-byte equivalent generated sections.
        # A document revision pins deletion indexes against concurrent human edits.
        revision=checked(client.get(API+f"/docx/v1/documents/{doc}",headers=auth(),timeout=20))["data"]["document"]["revision_id"]
        current=root_blocks(client,auth,doc)
        if not matches(current,blocks):
            raise ValueError("已提交写入，但飞书读回内容与本次整理稿不一致。")
        end=len(blocks)
        old_plans=[json.loads(r[0]) for r in db.execute(
            "select plan from knowledge_revisions where binding=? and version<>? and status='done'",(key,version))]
        while end<len(current):
            old=next((plan for plan in old_plans if plan and matches(current[end:],plan)
                      and all(removable(block) for block in current[end:end+len(plan)])),None)
            if not old:
                break
            end+=len(old)
        if end>len(blocks):
            if not await authorized():
                raise ValueError("整理授权已变更。")
            checked(client.request("DELETE",API+f"/docx/v1/documents/{doc}/blocks/{doc}/children/batch_delete",
                headers=auth(),params={"document_revision_id":revision},
                json={"start_index":len(blocks),"end_index":end},timeout=20))
            if not matches(root_blocks(client,auth,doc),blocks):
                raise ValueError("飞书未确认最终整理稿。")
        with db:
            db.execute("insert or replace into knowledge_layouts values(?,?)",(layout_key or key,instructions))
            db.execute("update knowledge_revisions set status='done' where binding=? and version=?",(key,version))
    return f"二狗已按主题重整 {len(records)} 条消息，最新综述在文档开头。\n《{title}》{link}",len(records)
