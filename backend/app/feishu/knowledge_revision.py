"""Versioned topic summaries of an employee's authorized Feishu conversation."""
import hashlib
import json
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from .private_summary import model_result
from .publisher import API, checked

TOPICS={"type":"object","properties":{"topics":{"type":"array","items":{
    "type":"object","properties":{"title":{"type":"string"},"summary":{"type":"string"},
    "sources":{"type":"array","items":{"type":"integer"}}},"required":["title","summary","sources"],
    "additionalProperties":False}}},"required":["topics"],"additionalProperties":False}


def text_block(kind, text, sources=(), chat=""):
    tags={"text":2,"heading1":3,"heading2":4}
    elements=[{"text_run":{"content":text}}]
    for n,mid in enumerate(sources,1):
        url="https://applink.feishu.cn/client/chat/open?openChatId="+quote(chat,safe="")+"&messageId="+quote(mid,safe="")
        elements.append({"text_run":{"content":f"　来源{n}","text_element_style":{"link":{"url":url}}}})
    return {"block_type":tags[kind],kind:{"elements":elements}}


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
            sections.setdefault(name,[]).append(text_block("text",note,sources,chat))
        if covered!=set(range(1,len(batch)+1)):
            raise ValueError("部分消息没有归入主题，文档未更新。")
    blocks=[text_block("heading1","知识综述"),text_block("text",f"本版涵盖 {len(records)} 条当前可见消息，按主题整理。")]
    for title,notes in sections.items():
        blocks.extend([text_block("heading2",title),*notes])
    return blocks


def block_signature(block):
    kind={2:"text",3:"heading1",4:"heading2"}.get(block.get("block_type"))
    if not kind:
        return None
    return (kind,[(run.get("content"),run.get("text_element_style",{}).get("link",{}).get("url"))
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
        if any(v for k,v in style.items() if k!="link"):
            return False
    return True


def root_blocks(client,auth,doc):
    params={"page_size":500}
    items=[]
    while True:
        data=checked(client.get(API+f"/docx/v1/documents/{doc}/blocks/{doc}/children",
                                headers=auth(),params=params,timeout=20))["data"]
        items.extend(data["items"])
        if not data.get("has_more"):
            return items
        params["page_token"]=data["page_token"]


def matches(actual,expected):
    return len(actual)>=len(expected) and all(block_signature(a)==block_signature(b)
                                               for a,b in zip(actual,expected))


async def revise_document(db,client,auth,authorized,profile,key,doc,origin,title,chat,records,*,stage=None,instructions="",layout_key=None):
    snapshot=[(mid,item.get("update_time"),item.get("create_time"),item.get("msg_type"),item.get("body"))
              for mid,item in sorted(records.items())]
    db.execute("create table if not exists knowledge_layouts(binding text primary key,instructions text not null)")
    if not instructions:
        layout=db.execute("select instructions from knowledge_layouts where binding=?",(layout_key or key,)).fetchone()
        if layout:
            instructions=layout[0]
    if instructions:
        snapshot=[instructions,snapshot]
    version=hashlib.sha256(json.dumps(snapshot,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    db.execute("""create table if not exists knowledge_revisions(
        binding text,version text,plan text,remaining integer,status text,
        primary key(binding,version))""")
    row=db.execute("select plan,remaining,status from knowledge_revisions where binding=? and version=?",
                   (key,version)).fetchone()
    link=origin+"/docx/"+doc
    if not await authorized():
        raise ValueError("请核对该员工的私聊整理和模型摘要授权。")
    if row and row[2]=="done":
        if not matches(root_blocks(client,auth,doc),json.loads(row[0])):
            raise ValueError("飞书现有综述已变化，需要核对后再更新。")
        return f"全部 {len(records)} 条消息与已写入的综述版本一致。\n《{title}》{link}",len(records)
    if db.execute("select 1 from knowledge_revisions where binding=? and status='sending'",(key,)).fetchone():
        raise ValueError("上次写入结果待核对，请先检查已有文档："+link)
    if row:
        blocks,remaining=json.loads(row[0]),row[1]
    else:
        if stage:
            stage("summarize",len(records))
        blocks=await summarize(records,profile,authorized,chat,instructions)
        remaining=len(blocks)
        with db:
            db.execute("insert into knowledge_revisions values(?,?,?,?,'planned')",
                (key,version,json.dumps(blocks,ensure_ascii=False),remaining))
    while remaining:
        start=max(0,remaining-30)
        if not await authorized():
            raise ValueError("员工的摘要授权已变更，已停止写入。")
        headers=auth()
        if stage:
            stage("write_blocks",remaining)
        with db:
            db.execute("update knowledge_revisions set status='sending' where binding=? and version=?",(key,version))
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
    if not instructions:
        if not matches(root_blocks(client,auth,doc),blocks):
            raise ValueError("飞书读回的正文与整理稿不一致，尚未完成。")
        with db:
            db.execute("update knowledge_revisions set status='done' where binding=? and version=?",(key,version))
    if instructions:
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
