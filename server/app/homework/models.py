"""Tables for homework mode (paper assignments) and the progress dashboard.

A homework is an ordinary `Exam` with `delivery = "paper"` (so the Phase 2 answer key, AI
grader, Scan & Grade review queue, release, feedback and gradebook work unchanged) plus one
`HomeworkAssignment` row that makes it homework: due date, late / resubmit rules,
auto-release and an optional worksheet. Created by db.create_all; every column added later
must carry a server_default (app.db._add_missing_columns).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint, false,
                        text, true)
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import BigIntPK
from ..timeutil import utcnow


class HomeworkAssignment(Base):
    __tablename__ = "homework_assignments"
    exam_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    allow_late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    allow_resubmit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    # release feedback by itself once every submitted answer has a teacher grade
    auto_release: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    # optional worksheet (PDF or image) the teacher shares: stored privately, served to
    # members of the assigned classes only
    worksheet_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    worksheet_name: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")
    worksheet_type: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    auto_released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class HomeworkSubmission(Base):
    """A student's own upload (photos from the student app). The graded answers live on the
    student's paper `Attempt` for the exam; this row keeps the pages and the timeline."""
    __tablename__ = "homework_submissions"
    __table_args__ = (UniqueConstraint("exam_id", "student_id", name="uq_homework_sub"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    exam_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    student_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    # processing (pages being flattened / answers located) -> grading (AI drafts) -> graded
    # | failed (see error; the student may submit again)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="processing")
    # [{"url": flattened page upload url, "flattened": bool}]
    pages: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("'[]'"))
    sources: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("'[]'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    segmented: Mapped[str] = mapped_column(String(16), nullable=False, default="", server_default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FeedbackSeen(Base):
    """In-app notification state: the student opened the released feedback of a homework
    (whether they uploaded it themselves or the teacher scanned the class stack)."""
    __tablename__ = "homework_feedback_seen"
    __table_args__ = (UniqueConstraint("exam_id", "student_id", name="uq_homework_seen"),)
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    exam_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    student_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ProgressNote(Base):
    """Teacher's private note about a student (progress dashboard)."""
    __tablename__ = "progress_notes"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    student_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
