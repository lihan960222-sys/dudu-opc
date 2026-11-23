"""Routing metadata attached to the planner's durable WorkItem identity."""
from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
from app.db.types import JSONValue


class FeatureScope(Base):
    __tablename__ = "feature_scope"
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("work_item.id"), primary_key=True)
    project: Mapped[str] = mapped_column(String)
    version: Mapped[str] = mapped_column(String)
    acceptance: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list] = mapped_column(JSONValue)
    checkpoint: Mapped[str] = mapped_column(Text, default="")
    related_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("work_item.id"))


class FeatureMessage(Base):
    __tablename__ = "feature_message"
    owner_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_key: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[int] = mapped_column(BigInteger)
    project: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSONValue)


class FeatureSession(Base):
    __tablename__ = "feature_session"
    thread_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("thread.id"), primary_key=True)
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("work_item.id"), index=True)
    previous_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("thread.id"))
