from typing import Literal
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.routers.feishu_owner_oauth import signed_in_owner_browser
from app.services import history_reset_control as control

router = APIRouter(prefix="/history-reset", dependencies=[Depends(signed_in_owner_browser)])


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=1, max_length=128)
    confirmation: Literal["清空历史记录"]


@router.get("")
def status():
    try:
        return control.view()
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        raise HTTPException(503, "暂时无法核对本机历史记录，请稍后刷新。") from None


@router.post("", status_code=202)
def submit(body: Confirmation):
    try:
        return {"operation": control.enqueue(body.revision)}
    except control.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        raise HTTPException(503, "暂时无法提交清理，请刷新查看状态。") from None
