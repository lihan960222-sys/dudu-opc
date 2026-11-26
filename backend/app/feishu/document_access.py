"""Resolve owner- and source-provided links; keep document edits in the configured folder."""
import json
import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from . import documents, knowledge_document
from .publisher import API, checked

LINK=re.compile(r'https?://[^\s<>"\x27\x60\[\]（）【】《》。，；！？]+')
DOC_PATH=re.compile(r"/docx/([A-Za-z0-9]{1,128})/?")
FOLDER_SCOPE="space:document:retrieve"
READ_SCOPE="docx:document:readonly"


def urls(text):
    return list(dict.fromkeys(m.group().rstrip(").,;:!?") for m in LINK.finditer(text)))


def message_urls(item):
    try:
        body=json.loads(item["body"]["content"])
    except (KeyError,TypeError,ValueError):
        return []
    def values(value):
        if isinstance(value,str):
            yield value
        elif isinstance(value,list):
            for entry in value:
                yield from values(entry)
        elif isinstance(value,dict):
            for entry in value.values():
                yield from values(entry)
    return list(dict.fromkeys(url for text in values(body) for url in urls(text)))


def doc_token(client,identity,parsed):
    doc=DOC_PATH.fullmatch(parsed.path)
    if doc:
        return doc[1]
    wiki=re.fullmatch(r"/wiki/([A-Za-z0-9]{1,128})/?",parsed.path)
    if wiki:
        data=checked(client.get(API+"/wiki/v2/spaces/get_node",headers=documents.user_headers(identity,required={"wiki:node:read"}),
            params={"token":wiki[1]},timeout=15))["data"]["node"]
        token=data.get("obj_token")
        if data.get("obj_type")=="docx" and isinstance(token,str) and documents.TOKEN.fullmatch(token):
            return token
        raise ValueError("此知识库节点指向的内容不是新版文档。")
    raise ValueError("当前支持飞书 /docx/ 文档和指向文档的 /wiki/ 节点。")


def request(text):
    first,_,body=text.strip().partition("\n")
    links=urls(first)
    if len(links)!=1:
        return None
    url=links[0]
    verb=first.replace(url," ").strip()
    # Only a direct affirmative instruction may authorize a mutation.
    match=re.fullmatch(r"(?:请|麻烦)?(?:让|叫)?(?:(?P<staff>[\w·\-]{1,24}?)(?:帮我|来))?(?:帮我)?(?:把|将)?(?:这份?|这个|该)?(?:飞书)?(?:云)?(?:文档|文件)?\s*(?P<verb>删除|移入回收站|替换|修改|追加|补写|追加到|阅读|读取|查看|总结|读一下|看一下|读|看看)(?:一下)?\s*(?:这份?|这个|该)?(?:飞书)?(?:云)?(?:文档|文件|链接|网页|论文)?(?:里|内|中)?[ ：:。]*",verb)
    # A named employee prefix is common in direct messages.
    named=re.fullmatch(r"(?:请)?(?:让|叫)([\w·\-]{1,24}?)(删除|替换|修改|追加|阅读|读取|查看|总结|看看|读一下|看一下)(?:这份?|这个|该)?(?:飞书)?(?:文档|链接|网页|论文)?[ ：:。]*",verb)
    if not match and not named:
        # Leave ambiguous operations to the lead's semantic delivery contract.
        return None
    staff=(match["staff"] or "二狗") if match else named[1]
    op=match["verb"] if match else named[2]
    action="delete_document" if op in ("删除","移入回收站") else "edit_document" if op in ("替换","修改","追加","补写","追加到") else "read_document"
    if action!="read_document" and not (DOC_PATH.fullmatch(urlsplit(url).path) or action=="edit_document" and re.fullmatch(r"/wiki/[A-Za-z0-9]+/?",urlsplit(url).path)):
        return None
    if action=="delete_document" and (body.strip() or re.search(r"不要|别删|不删|是否|能否|[？?]",verb)):
        return None
    return staff,action,url,body.strip()


def indexed(db):
    db.execute("""create table if not exists employee_source_links(
        app text,employee integer,job text,chat text,url text,
        primary key(app,employee,job,chat,url))""")


def in_folder(client,headers,folder,doc):
    params={"folder_token":folder,"page_size":200}
    seen=set()
    while True:
        data=checked(client.get(API+"/drive/v1/files",headers=headers(),params=params,timeout=15))["data"]
        if any(item.get("type")=="docx" and item.get("token")==doc and item.get("parent_token")==folder for item in data["files"]):
            return True
        if not data.get("has_more"):
            return False
        cursor=data.get("next_page_token")
        if not cursor or cursor in seen:
            raise ValueError("飞书未能核对文档所在文件夹。")
        seen.add(cursor)
        params["page_token"]=cursor


async def read(client,identity,url,offset):
    if type(offset)!=int or offset<0:
        raise ValueError("读取位置无效。")
    parsed=urlsplit(url)
    if parsed.scheme=="https" and parsed.hostname and parsed.hostname.endswith((".feishu.cn",".larksuite.com")):
        doc=doc_token(client,identity,parsed)
        auth=documents.user_headers(identity,required={READ_SCOPE})
        value=checked(client.get(API+f"/docx/v1/documents/{doc}/raw_content",headers=auth,timeout=20))["data"]["content"]
        end=min(offset+8000,len(value))
        if offset>len(value):
            raise ValueError("读取位置超过文档长度。")
        return {"url":url,"text":value[offset:end],"next_offset":end if end<len(value) else None,"complete":end==len(value)}
    from . import public_reference
    return await public_reference.read(url,offset)


def replacement(body):
    match=re.fullmatch(r"原文[：:]([^\n]+)\n(?:改为|替换为)[：:]([^\n]+)",body.strip())
    return match.groups() if match else None


def replace_paragraph(client,headers,db,token,doc,url,old,new):
    from .knowledge_revision import root_blocks,removable
    if not old.strip() or not new.strip() or old==new or len(new)>1200:
        raise ValueError("请提供不同的原文与新段落，每段最多1200字。")
    def content(block):
        elements=block.get("text",{}).get("elements",[])
        return elements[0].get("text_run",{}).get("content") if len(elements)==1 else None
    db.execute("create table if not exists employee_doc_replacements(token text primary key,block text)")
    db.execute("create table if not exists verified_doc_replacements(token text primary key)")
    confirmed=db.execute("select 1 from verified_doc_replacements where token=?",(token,)).fetchone()
    row=db.execute("select block from employee_doc_replacements where token=?",(token,)).fetchone()
    version=checked(client.get(API+f"/docx/v1/documents/{doc}",headers=headers(),timeout=20))["data"]["document"]["revision_id"]
    found=[b for b in root_blocks(client,headers,doc) if b.get("block_id")==row[0]] if row else [
        b for b in root_blocks(client,headers,doc) if content(b)==old]
    if len(found)!=1 or not removable(found[0]) or found[0].get("text",{}).get("elements",[{}])[0].get("text_run",{}).get("text_element_style",{}).get("link"):
        raise ValueError("找不到唯一匹配的纯文本段落，请核对原文。")
    block=found[0]["block_id"]
    if not row:
        with db:
            db.execute("insert or ignore into employee_doc_replacements values(?,?)",(token,block))
    endpoint=API+f"/docx/v1/documents/{doc}/blocks/{block}"
    if content(found[0])!=new:
        if confirmed or content(found[0])!=old:
            raise ValueError("目标段落已由他人修改。")
        checked(client.patch(endpoint,headers=headers(),params={"document_revision_id":version,"client_token":token},
            json={"update_text_elements":{"elements":[{"text_run":{"content":new}}]}},timeout=20))
    result=checked(client.get(endpoint,headers=headers(),timeout=15))["data"]["block"]
    if result.get("parent_id")!=doc or content(result)!=new:
        raise ValueError("替换已提交，读回内容与预期不符。")
    with db:
        db.execute("insert or ignore into verified_doc_replacements values(?)",(token,))
    return {"confirmed":True,"url":url,"message":"已替换并核对指定段落。\n"+url}


def append(cfg,employee,identity,db,client,job,url,body):
    dest,_=knowledge_document.target(cfg,employee)
    parsed=urlsplit(url)
    if parsed.scheme+"://"+parsed.netloc!=dest["tenant_origin"]:
        raise ValueError("请提供当前企业的原始飞书文档链接。")
    doc=doc_token(client,identity,parsed)
    children=documents.blocks(body)
    def headers():
        if knowledge_document.target(cfg,employee)[0]!=dest:
            raise ValueError("目标文件夹已变更。")
        return documents.user_headers(identity,required={FOLDER_SCOPE,READ_SCOPE,"docx:document:write_only"})
    if not in_folder(client,headers,dest["folder_token"],doc):
        raise ValueError("目标文档不在二狗获准编辑的文件夹内。")
    token=str(uuid5(NAMESPACE_URL,json.dumps([identity["steward"]["app_id"],employee,job,url,body])))
    change=replacement(body)
    if change:
        return replace_paragraph(client,headers,db,token,doc,url,*change)
    db.execute("create table if not exists employee_doc_appends(token text primary key,ids text)")
    with db:
        claimed=db.execute("insert or ignore into employee_doc_appends values(?,null)",(token,)).rowcount
    if claimed:
        result=checked(client.post(API+f"/docx/v1/documents/{doc}/blocks/{doc}/children",headers=headers(),
            params={"client_token":token},json={"index":-1,"children":children},timeout=20))["data"]["children"]
        ids=[block["block_id"] for block in result]
        if len(ids)!=len(children):
            raise ValueError("飞书未确认所有新增段落。")
        with db:
            db.execute("update employee_doc_appends set ids=? where token=?",(json.dumps(ids),token))
    else:
        stored=db.execute("select ids from employee_doc_appends where token=?",(token,)).fetchone()[0]
        if not stored:
            raise ValueError("上次追加结果待核对，已暂停重复写入。")
        ids=json.loads(stored)
    for block,expected in zip(ids,children):
        actual=checked(client.get(API+f"/docx/v1/documents/{doc}/blocks/{block}",headers=headers(),timeout=15))["data"]["block"]
        kind=next(key for key in expected if key!="block_type")
        if actual.get("parent_id")!=doc or actual.get("block_type")!=expected["block_type"] or actual.get(kind,{}).get("elements") is None:
            raise ValueError("飞书新增段落读回核对失败。")
        if [r.get("text_run",{}).get("content") for r in actual[kind]["elements"]]!=[r["text_run"]["content"] for r in expected[kind]["elements"]]:
            raise ValueError("飞书新增段落内容与派工不符。")
    return {"confirmed":True,"url":url,"message":f"已追加并核对 {len(ids)} 段。\n"+url}

async def read_page(client,identity,db,employee,job,url,offset):
    if type(offset)!=int or offset<0:
        raise ValueError("读取位置无效。")
    key=identity["steward"]["app_id"],employee,job,url
    db.execute("""create table if not exists employee_doc_pages(app text,employee integer,job text,
        url text,frontier integer,primary key(app,employee,job,url))""")
    cursor=db.execute("select frontier from employee_doc_pages where app=? and employee=? and job=? and url=?",key).fetchone()
    if offset>(cursor[0] if cursor else 0):
        raise ValueError("请按返回的 next_offset 顺序读取文档。")
    result=await read(client,identity,url,offset)
    end=result["next_offset"] if result.get("next_offset") is not None else offset+len(result["text"])
    with db:
        db.execute("""insert into employee_doc_pages values(?,?,?,?,?) on conflict(app,employee,job,url)
            do update set frontier=max(frontier,excluded.frontier)""",(*key,end))
    return result
