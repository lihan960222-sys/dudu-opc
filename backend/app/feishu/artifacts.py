"""Copy only deliverables explicitly named in this turn's Feishu handoff."""
import hashlib
import json
import os
import tempfile
from pathlib import Path, PureWindowsPath

from app.services.feature_files import FeatureFiles, checked


REPORT_REQUEST = """
请在最后回复一个 JSON 对象。summary 写给用户，group_update 用不超过 300 字概括可在工作群公开的操作和验证结果，不含原始日志、密钥或私聊原文。artifacts 是本轮交付且可向授权工作群和用户公开的文件相对路径数组；请把这类文件写入当前任务的「产物/飞书」子目录。不需要发附件时用空数组。
"""


def parse_report(result):
    raw = result.strip()
    if raw.startswith('```json\n') and raw.endswith('```'):
        raw = raw[8:-3].strip()
    try:
        data = json.loads(raw)
    except (TypeError,ValueError):
        data = None
    if not isinstance(data,dict) or not isinstance(data.get('summary'),str):
        return result, '', []
    summary = data['summary']
    public = data.get('group_update')
    names = data.get('artifacts')
    return summary, public[:300] if isinstance(public,str) else '', names[:8] if isinstance(names,list) else []


def snapshot(root, task, relative, state):
    if not isinstance(relative,str):
        raise ValueError('无效的产物路径')
    name = PureWindowsPath(relative)
    if name.parts[:2] != ('产物','飞书') or len(name.parts)<3:
        raise ValueError('只能发送当前任务中明确列出的公开产物')
    path = FeatureFiles(root/'任务'/str(int(task))).path('task',relative)
    if not path.is_file():
        raise ValueError('产物未找到')
    kind = {'.png':'image','.jpg':'image','.jpeg':'image','.webp':'image',
            '.opus':'audio','.mp4':'media'}.get(path.suffix.lower(),'file')
    limit = (10 if kind=='image' else 30)*1024*1024
    folder = state/'outgoing'/str(int(task))
    folder.mkdir(parents=True,exist_ok=True)
    with path.open('rb') as src, tempfile.NamedTemporaryFile(dir=folder,delete=False) as dest:
        tmp = Path(dest.name)
        try:
            ident = os.fstat(src.fileno())
            if (not os.path.samestat(ident,checked(path).stat()) or ident.st_nlink != 1
                    or ident.st_size > limit):
                raise ValueError('文件已变更、经过链接或超过发送上限')
            data = src.read(limit+1)
            if len(data)!=ident.st_size:
                raise ValueError('文件在读取时发生变化')
            dest.write(data)
        except BaseException:
            dest.close()
            tmp.unlink(missing_ok=True)
            raise
    digest = hashlib.sha256(data).hexdigest()
    target = folder/digest/path.name
    target.parent.mkdir(exist_ok=True)
    os.replace(tmp,target)
    return {'kind':kind,'snapshot':str(target.relative_to(folder))}
