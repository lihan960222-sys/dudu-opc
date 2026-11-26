from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JSONValue, UTCDateTime


class KnowledgeWeeklyGrant(Base):
    __tablename__ = "knowledge_weekly_grant"
    owner_id: Mapped[str] = mapped_column(String, primary_key=True)
    employee_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_job_id: Mapped[int] = mapped_column(BigInteger)
    task_id: Mapped[int] = mapped_column(BigInteger)
    assignment_epoch: Mapped[int] = mapped_column(Integer)
    scope: Mapped[dict] = mapped_column(JSONValue)
    enabled: Mapped[bool] = mapped_column(Boolean)
    generation: Mapped[str] = mapped_column(String(32))
    next_due: Mapped[datetime] = mapped_column(UTCDateTime)
    blocked_reason: Mapped[str | None] = mapped_column(String(300))


class KnowledgeWeeklySlot(Base):
    __tablename__ = "knowledge_weekly_slot"
    event_key: Mapped[str] = mapped_column(String(90), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String)
    employee_id: Mapped[int] = mapped_column(BigInteger)
    job_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    scope_digest: Mapped[str] = mapped_column(String(64))
    cutoff: Mapped[int] = mapped_column(BigInteger)
    since: Mapped[int | None] = mapped_column(BigInteger)
