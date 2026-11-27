"""Signed-in owner controls for exact shared releases and project pins."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from app.routers.agents import _agent_or_404, get_runtime
from app.routers.feishu_owner_oauth import signed_in_owner_browser
from app.services import shared_library as library
from app.services.thread_runtime import ThreadRuntime

router = APIRouter(prefix="/shared-library", dependencies=[Depends(signed_in_owner_browser)])


class Import(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release: library.ReleaseRequest
    approval: str = "official"
    from_employee: int | None = None


class Pin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: str | None


class Decision(BaseModel):
    choice: str


@router.get("/reviews/pending")
def pending_reviews(rt: ThreadRuntime = Depends(get_runtime)):
    return library.pending_reviews(rt.owner_id)


@router.post("/reviews/{review_id}")
def decide(review_id: str, decision: Decision, rt: ThreadRuntime = Depends(get_runtime)):
    try:
        library.decide_review(rt.owner_id, review_id, decision.choice)
        return {"choice":decision.choice}
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/{agent_id}")
async def index(agent_id: int, rt: ThreadRuntime = Depends(get_runtime)):
    agent = await _agent_or_404(rt, agent_id)
    try:
        pins = library.project_entries(rt.owner_id, Path(agent.role_path)) if agent.role_path else []
        return {"releases": library.catalog(rt.owner_id), "pins": pins, "publishers": library.publishers(rt.owner_id)}
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("")
async def publish(body: Import, rt: ThreadRuntime = Depends(get_runtime)):
    if body.approval not in ("official", "version", "publisher"):
        raise HTTPException(422, "请选择来源授权范围。")
    trial_root = None
    if body.from_employee is not None:
        agent = await _agent_or_404(rt, body.from_employee)
        if not agent.role_path:
            raise HTTPException(422, "员工没有项目目录。")
        trial_root = Path(agent.role_path)
    try:
        release, fetched = await run_in_threadpool(library.resolve_request, rt.owner_id, body.release)
        return await run_in_threadpool(library.publish, rt.owner_id, release, fetched=fetched,
                                       approve_external=body.approval != "official",
                                       trust_publisher=body.approval == "publisher", trial_project=trial_root)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from None


@router.delete("/publisher/{org}")
async def forget(org: str, rt: ThreadRuntime = Depends(get_runtime)):
    library.forget_publisher(rt.owner_id, "github/" + org)
    return {"publishers": library.publishers(rt.owner_id)}


@router.put("/{agent_id}")
async def update_pin(agent_id: int, body: Pin, rt: ThreadRuntime = Depends(get_runtime)):
    agent = await _agent_or_404(rt, agent_id)
    if not agent.role_path:
        raise HTTPException(422, "请先保存员工项目目录。")
    try:
        library.pin(rt.owner_id, Path(agent.role_path), body.name, body.version)
        return {"pins": library.project_entries(rt.owner_id, Path(agent.role_path))}
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from None
