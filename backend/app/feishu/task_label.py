import re

from app.db.models import WorkItem


async def task_label(db, job):
    item = await db.get(WorkItem, job.task_id) if job.task_id else None
    title = item.title if item and item.owner_id == job.owner_id else job.brief
    subject = re.split(r"[，。；！？\n]", (title or "").strip(), maxsplit=1)[0]
    subject = " ".join(subject.split())
    if not subject:
        return "当前任务"
    return "工单·" + (subject if len(subject) <= 24 else subject[:23] + "…")
