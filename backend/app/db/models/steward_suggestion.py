from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import BigIntPK, UTCDateTime


class StewardSuggestion(Base):
    """An owner decision about a dated observation. The source task remains authoritative."""
    __tablename__ = "steward_suggestion"
    __table_args__ = (
        UniqueConstraint("owner_id", "task_id", "source_key"),
        Index("ix_steward_suggestion_inbox", "owner_id", "state", "id"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(BigIntPK, Identity(always=True), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String)
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("work_item.id", ondelete="CASCADE"))
    source_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String, default="pending")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    option: Mapped[str | None] = mapped_column(String)
    report: Mapped[str | None] = mapped_column(Text)
    followup_task_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())
