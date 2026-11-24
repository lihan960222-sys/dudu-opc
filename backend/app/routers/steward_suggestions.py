from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.auth.middleware import principal_of
from app.db.models import StewardSuggestion
from app.routers.threads import get_runtime
from app.services import steward_suggestions as suggestions
from app.services.local_access_policy import require_local_access


def require_boss(request: Request):
    p = principal_of(request.scope)
    if p is not None and (p.person_id is not None and p.role != "owner" or
                          not (p.is_admin or p.kind == "device" and "manage" in p.scopes)):
        raise HTTPException(403, "请用老板账户或已配对的管理设备处理建议。")


router = APIRouter(prefix="/steward/suggestions", tags=["Steward"],
                   dependencies=[Depends(require_local_access), Depends(require_boss)])


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: Literal["approve", "defer", "dismiss"]
    option: Literal["brief", "followup"] | None = None
    revision: int = Field(ge=1)


@router.get("")
async def pending(state: Literal["pending", "deferred", "approved", "dismissed"] = "pending", rt=Depends(get_runtime)):
    async with rt.session() as db:
        rows = await db.scalars(select(StewardSuggestion).where(
            StewardSuggestion.owner_id == rt.owner_id, StewardSuggestion.state == state)
            .order_by(StewardSuggestion.id).limit(100))
        return {"items":[suggestions.public(r) for r in rows]}


@router.get("/{ident}")
async def get(ident: int, rt=Depends(get_runtime)):
    async with rt.session() as db:
        return suggestions.public(await suggestions.owned(db, rt.owner_id, ident))


@router.post("/{ident}/decision")
async def decide(ident: int, body: Decision, rt=Depends(get_runtime)):
    async with rt.session() as db:
        return await suggestions.decide(db, rt.owner_id, ident, body.choice, body.option, body.revision)
