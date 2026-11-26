"""An agent's saved skills (docs/SKILLS.md): list, read, write, delete and
preview an import. Text only; nothing here runs a skill. Same local access
check as the agents router.

Errors carry `{"detail": {"error_code": "...", "message": "..."}}`."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.db.models import Thread, Turn
from app.services import skill_learning
from app.routers.agents import _agent_or_404, get_runtime
from app.services import agent_skills as skills
from app.services.local_access_policy import require_local_access
from app.services.thread_runtime import ThreadRuntime

router = APIRouter(
    prefix="/agents/{agent_id}/skills",
    tags=["Agent skills"],
    dependencies=[Depends(require_local_access)],
)


class SkillSummary(BaseModel):
    name: str
    description: str
    enabled: bool
    source: str
    bytes: int
    updated_at: datetime | None
    triggers: list[str] = []


class SkillResponse(BaseModel):
    name: str
    description: str
    body: str
    enabled: bool
    source: str
    revision: str = ""
    redacted: int = 0
    warnings: list[str] = []
    triggers: list[str] = []


class SkillSuggestRequest(BaseModel):
    text: str = Field(max_length=20_000)


class SkillWriteRequest(BaseModel):
    description: str = Field(max_length=2000)
    body: str = Field(max_length=200_000)
    enabled: bool = True
    source: str = Field(default="user", max_length=200)
    # None keeps the skill's current trigger phrases; a list replaces them.
    triggers: list[str] | None = Field(default=None, max_length=50)


class SkillLearnRequest(BaseModel):
    thread_id: int = Field(gt=0)
    turn_id: int = Field(gt=0)


class SkillLearnSaveRequest(SkillLearnRequest):
    description: str = Field(max_length=300)
    body: str = Field(max_length=200_000)
    reviewed: bool


class SkillLearnPreviewResponse(BaseModel):
    name: str
    description: str
    body: str
    warnings: list[str]
    thread_id: int
    turn_id: int
    redacted: int


class SkillUndoRequest(BaseModel):
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class SkillImportRequest(BaseModel):
    markdown: str = Field(max_length=200_000)


class SkillPreviewResponse(BaseModel):
    name: str
    description: str
    body: str
    warnings: list[str]


def _http(exc: skills.SkillError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})


def _response(skill: skills.Skill) -> SkillResponse:
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        body=skill.body,
        enabled=skill.enabled,
        source=skill.source,
        revision=skill.revision,
        redacted=skill.redacted,
        warnings=skill.warnings,
        triggers=skill.triggers,
    )


@router.get("", response_model=list[SkillSummary])
async def list_skills(agent_id: int, rt: ThreadRuntime = Depends(get_runtime)):
    await _agent_or_404(rt, agent_id)
    try:
        return [
            SkillSummary(
                name=s.name, description=s.description, enabled=s.enabled, source=s.source,
                bytes=s.bytes, updated_at=s.updated_at, triggers=s.triggers,
            )
            for s in skills.list_skills(agent_id)
        ]
    except skills.SkillError as exc:
        raise _http(exc) from None


@router.get("/catalog/available")
async def bundled_catalog(agent_id: int, request: Request, rt: ThreadRuntime = Depends(get_runtime)):
    agent = await _agent_or_404(rt, agent_id)
    try:
        from sqlalchemy import select
        from app.db.models.community_mcp import CommunityMcpConnection, CommunityMcpToolGrant
        from app.db.models.custom_mcp import CustomMcpConnection, CustomMcpToolGrant
        from app.db.models.muapi import MuapiToolGrant
        from app.db.models import Event
        from app.routers.staff import local_owner
        from app.services import employee_runtime, role_workspace
        from pathlib import Path
        result = skills.bundled_catalog(agent_id)
        tools, used, verified = [], {}, {}
        result.update(project_skills=[], last_model=None)
        try:
            local_owner(request)
            show_receipts = True
        except HTTPException:
            show_receipts = False
        if show_receipts and agent.role_path and employee_runtime.native_enabled(rt.owner_id, agent_id):
            try:
                folder = role_workspace.checked_root(Path(agent.role_path))
                role_workspace.validate_configured_location(folder)
                result["project_skills"] = skills.project_skill_catalog(folder, agent_id)
            except (OSError, ValueError):
                pass
        async with rt.session() as db:
            if show_receipts:
                from app.db.models.knowledge_weekly import KnowledgeWeeklyGrant
                from app.routers.weekly_knowledge import public as weekly_details
                weekly = await db.get(KnowledgeWeeklyGrant, (rt.owner_id, agent_id))
                if weekly:
                    result["weekly_knowledge"] = weekly_details(weekly)
                recent = await db.scalars(select(Event).join(Thread, Thread.id == Event.thread_id).where(
                    Thread.owner_id == rt.owner_id, Thread.agent_id == agent_id,
                    Event.type.in_(("skill.used", "mcp.verified", "model.receipt"))).order_by(Event.id.desc()).limit(300))
                for event in recent:
                    data = event.data or {}
                    if event.type == "skill.used" and data.get("revision"):
                        for name in data.get("skills", []):
                            used.setdefault((name, data["revision"]), event.created_at)
                    elif event.type == "mcp.verified":
                        key = (data.get("connection"), data.get("revision"), data.get("tool"), data.get("schema_digest"))
                        verified.setdefault(key, event.created_at)
                    elif event.type == "model.receipt" and result["last_model"] is None:
                        result["last_model"] = {**data, "at":event.created_at}
            for grant, connection in ((CommunityMcpToolGrant, CommunityMcpConnection), (CustomMcpToolGrant, CustomMcpConnection)):
                rows = await db.execute(select(grant, connection).join_from(grant, connection, grant.connection_id == connection.id)
                                        .where(grant.agent_id == agent_id, connection.owner_id == rt.owner_id))
                for item, source in rows:
                    custom = isinstance(source, CustomMcpConnection)
                    status = "granted"
                    if custom:
                        if not source.enabled:
                            status = "disabled"
                        elif source.tested_revision != source.revision or source.last_error_code:
                            status = "needs_test"
                        elif not source.shared:
                            status = "private"
                    label = source.name if custom else source.slug
                    tools.append({"name":f"{label} · {item.tool_name}", "description":item.description,
                                  "source":"自定义 MCP" if custom else "MCP 应用市场", "status":status,
                                  "verified_at":verified.get((source.id, source.revision, item.tool_name, item.schema_digest))})
            for item in await db.scalars(select(MuapiToolGrant).where(MuapiToolGrant.agent_id == agent_id)):
                tools.append({"name":f"MuAPI · {item.tool_name}", "description":item.description,
                              "source":"MuAPI 接口", "status":"granted",
                              "verified_at":verified.get(("muapi-direct", 1, item.tool_name, item.schema_digest))})
        result["skill_receipts"] = {s.name:used[s.name, s.revision] for s in skills.list_skills(agent_id) if (s.name, s.revision) in used}
        for item in result["project_skills"]:
            item["verified_at"] = used.get((item["name"], item["revision"]))
        result["tools"]=sorted(tools,key=lambda item:item["name"])
        return result
    except skills.SkillError as exc:
        raise _http(exc) from None


@router.post("/suggest", response_model=list[SkillSummary])
async def suggest_skills(agent_id: int, payload: SkillSuggestRequest, rt: ThreadRuntime = Depends(get_runtime)):
    """Enabled skills whose trigger phrase appears in the text. A suggestion
    only: nothing is chosen, enabled or run."""
    await _agent_or_404(rt, agent_id)
    try:
        return [
            SkillSummary(
                name=s.name, description=s.description, enabled=s.enabled, source=s.source,
                bytes=s.bytes, updated_at=s.updated_at, triggers=s.triggers,
            )
            for s in skills.suggest(skills.list_skills(agent_id), payload.text)
        ]
    except skills.SkillError as exc:
        raise _http(exc) from None


@router.post("/import", response_model=SkillPreviewResponse)
async def preview_import(agent_id: int, payload: SkillImportRequest, rt: ThreadRuntime = Depends(get_runtime)):
    """What a SKILL.md text would save. Saves nothing; the person reviews it,
    then saves it with PUT (turned off until reviewed)."""
    await _agent_or_404(rt, agent_id)
    try:
        return SkillPreviewResponse(**skills.preview(payload.markdown))
    except skills.SkillError as exc:
        raise _http(exc) from None


async def _learn_preview(rt: ThreadRuntime, agent_id: int, payload: SkillLearnRequest) -> dict:
    await _agent_or_404(rt, agent_id)
    async with rt.session() as db:
        thread = await db.get(Thread, payload.thread_id)
        turn = await db.get(Turn, payload.turn_id)
        if thread is None or turn is None:
            raise _http(skills.SkillError(404, "skill_source_not_found", "No such source conversation for this agent."))
        try:
            return skill_learning.draft(thread, turn, owner_id=rt.owner_id, agent_id=agent_id)
        except skills.SkillError as exc:
            raise _http(exc) from None


@router.post("/learn", response_model=SkillLearnPreviewResponse)
async def preview_learn(agent_id: int, payload: SkillLearnRequest, rt: ThreadRuntime = Depends(get_runtime)):
    """Prepare a bounded, redacted editable draft; persist and execute nothing."""
    return await _learn_preview(rt, agent_id, payload)


@router.post("/learn/{name}", response_model=SkillResponse, status_code=201)
async def save_learned(
    agent_id: int, name: str, payload: SkillLearnSaveRequest, rt: ThreadRuntime = Depends(get_runtime)
):
    await _learn_preview(rt, agent_id, payload)
    if not payload.reviewed:
        raise _http(skills.SkillError(422, "skill_review_required", "Review the instructions before saving."))
    try:
        return _response(skills.write_skill(
            agent_id, name, payload.description, payload.body, enabled=False,
            source=f"learned:{payload.thread_id}:{payload.turn_id}", create_only=True,
        ))
    except skills.SkillError as exc:
        raise _http(exc) from None


@router.post("/{name}/undo", status_code=204)
async def undo_learned(
    agent_id: int, name: str, payload: SkillUndoRequest, rt: ThreadRuntime = Depends(get_runtime)
):
    """Remove only the unchanged learned creation named by the saved receipt."""
    await _agent_or_404(rt, agent_id)
    try:
        skills.delete_skill(agent_id, name, expected_revision=payload.revision)
    except skills.SkillError as exc:
        raise _http(exc) from None


@router.get("/{name}", response_model=SkillResponse)
async def read_skill(agent_id: int, name: str, rt: ThreadRuntime = Depends(get_runtime)):
    await _agent_or_404(rt, agent_id)
    try:
        skills.check_name(name)
        return _response(skills.read_skill(agent_id, name))
    except skills.SkillError as exc:
        raise _http(exc) from None


@router.put("/{name}", response_model=SkillResponse)
async def write_skill(
    agent_id: int, name: str, payload: SkillWriteRequest, rt: ThreadRuntime = Depends(get_runtime)
):
    await _agent_or_404(rt, agent_id)
    try:
        return _response(
            skills.write_skill(
                agent_id, name, payload.description, payload.body, enabled=payload.enabled, source=payload.source,
                triggers=payload.triggers,
            )
        )
    except skills.SkillError as exc:
        raise _http(exc) from None


@router.delete("/{name}", status_code=204)
async def delete_skill(agent_id: int, name: str, rt: ThreadRuntime = Depends(get_runtime)):
    await _agent_or_404(rt, agent_id)
    try:
        skills.delete_skill(agent_id, name)
    except skills.SkillError as exc:
        raise _http(exc) from None
