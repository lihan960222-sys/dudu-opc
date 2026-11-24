"""Feishu owner chat bindings. Each model gets an independent native conversation."""
from sqlalchemy import BigInteger,Boolean,ForeignKey,String,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from app.db.base import Base


class FeishuLeadChat(Base):
    __tablename__='feishu_lead_chat'
    __table_args__=(UniqueConstraint('channel','selection',name='uq_feishu_lead_selection'),)
    thread_id:Mapped[int]=mapped_column(BigInteger,ForeignKey('thread.id',ondelete='CASCADE'),primary_key=True,autoincrement=False)
    channel:Mapped[str]=mapped_column(String(64))
    selection:Mapped[str]=mapped_column(String(64))
    model:Mapped[str|None]=mapped_column(String)
    project:Mapped[str]=mapped_column(String)
    isolated:Mapped[bool]=mapped_column(Boolean)
