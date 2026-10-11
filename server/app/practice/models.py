"""Practice assignments (slice 6 H). Formative only: these tables are never read by the
gradebook, results, averages or grade release."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, false, text, true
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import BigIntPK
from ..timeutil import utcnow


class PracticeSet(Base):
    __tablename__ = "practice_sets"
    __table_args__ = (Index("ix_practice_sets_student_status", "student_id", "status"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft|approved|sent
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    topics: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("'[]'"))
    items: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("'[]'"))
    formative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PracticeAttempt(Base):
    """The student's latest answer to one practice item (upsert per set+item)."""
    __tablename__ = "practice_attempts"
    __table_args__ = (UniqueConstraint("set_id", "item_id", name="uq_practice_attempt_item"),)
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    set_id: Mapped[str] = mapped_column(ForeignKey("practice_sets.id", ondelete="CASCADE"), index=True,
                                        nullable=False)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    item_id: Mapped[str] = mapped_column(String(32), nullable=False)
    answer: Mapped[object] = mapped_column(JSON, nullable=True)
    correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tries: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    ever_correct: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                               server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
