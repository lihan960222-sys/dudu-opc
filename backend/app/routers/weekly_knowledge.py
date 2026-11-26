import sqlite3
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db.models.knowledge_weekly import KnowledgeWeeklyGrant
from app.db.transactions import begin_write
from app.feishu import weekly_knowledge
from app.routers.agents import get_runtime
from app.routers.feishu_owner_oauth import owner_browser
from app.services.thread_runtime import ThreadRuntime
from app.team.feishu_assignment import configuration

router = APIRouter(prefix="/weekly-knowledge", dependencies=[Depends(owner_browser)])


class ExistingGrant(BaseModel):
    source_job_id: int = Field(gt=0)


def public(grant):
    if not grant:
        return {"enabled":False, "dispatch_enabled":weekly_knowledge.enabled()}
    scope = grant.scope
    return {"enabled":grant.enabled, "dispatch_enabled":weekly_knowledge.enabled(), "next_due":grant.next_due,
            "employee_id":grant.employee_id, "task_id":grant.task_id, "contact":scope["contact"],
            "document_url":scope["destination"]["tenant_origin"] + "/docx/" + scope["document"],
            "blocked_reason":grant.blocked_reason, "source_job_id":grant.source_job_id}


@router.get("/{employee}")
async def read(employee: int, rt: ThreadRuntime = Depends(get_runtime)):
    async with rt.session() as db:
        return public(await db.get(KnowledgeWeeklyGrant, (rt.owner_id, employee)))


@router.put("/{employee}")
async def import_existing(employee: int, body: ExistingGrant, rt: ThreadRuntime = Depends(get_runtime)):
    cfg = configuration()
    if not cfg or cfg["owner_id"] != rt.owner_id:
        raise HTTPException(409, "当前工作台没有绑定此老板的飞书配置。")
    async with rt.session() as db:
        try:
            return public(await weekly_knowledge.enroll(db, cfg, employee, body.source_job_id))
        except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
            raise HTTPException(409, str(exc)) from None


@router.delete("/{employee}")
async def disable(employee: int, rt: ThreadRuntime = Depends(get_runtime)):
    async with rt.session() as db:
        await begin_write(db)
        grant = await db.get(KnowledgeWeeklyGrant, (rt.owner_id, employee), with_for_update=True)
        if grant:
            grant.enabled, grant.blocked_reason = False, "老板已停用每周知识库更新。"
            await db.commit()
        return public(grant)
