"""Phase 3 tables: grading benchmark, benchmark runs, routing policies, second marking.

Created by db.create_all (new tables). Plain (non-FK) id columns so the forward-only
migration in app.db can add columns later; every new column needs a server_default.
No student names are stored anywhere in these tables.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, Integer, String, Text,
                        UniqueConstraint, false, text)
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import BigIntPK
from ..timeutil import utcnow


class BenchmarkSet(Base):
    """A teacher's set of permissioned, professor-marked answers."""
    __tablename__ = "benchmark_sets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    # Why we may use these answers (e.g. "students opted in on the consent form, Fall 2026").
    consent_note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    consent_basis: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class BenchmarkItem(Base):
    __tablename__ = "benchmark_items"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    set_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    question_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    question_type: Mapped[str] = mapped_column(String(16), nullable=False, default="essay")
    subject: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    max_points: Mapped[float] = mapped_column(Float, nullable=False, default=1)
    rubric: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("'[]'"))
    # Answer key for numeric/short/mcq questions (number, accepted string, option index) or null.
    answer_key: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    answer_image: Mapped[str | None] = mapped_column(String(200), nullable=True)  # /api/uploads/<id>
    answer_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    human_marks: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("'[]'"))
    human_points: Mapped[float] = mapped_column(Float, nullable=False, default=0)  # reference mark
    human_comment: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    source_exam_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source_attempt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_question_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    consent_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    set_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    grader: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="")
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")  # running|done|failed
    severe_fraction: Mapped[float] = mapped_column(Float, nullable=False, default=0.25)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BenchmarkRunItem(Base):
    __tablename__ = "benchmark_run_items"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    item_id: Mapped[str] = mapped_column(String(32), nullable=False)
    question_type: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    subject: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    max_points: Mapped[float] = mapped_column(Float, nullable=False, default=1)
    human_points: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    ai_points: Mapped[float | None] = mapped_column(Float, nullable=True)  # null = AI failed
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class RoutingPolicy(Base):
    """Review-routing settings: exam_id "" = the teacher's default for all exams."""
    __tablename__ = "quality_routing_policies"
    __table_args__ = (UniqueConstraint("teacher_id", "exam_id", name="uq_routing_teacher_exam"),)
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    exam_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="")
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class SecondMark(Base):
    """One answer chosen for blind second marking, and its moderation history."""
    __tablename__ = "quality_second_marks"
    __table_args__ = (UniqueConstraint("exam_id", "attempt_id", "question_id", name="uq_second_mark_item"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    exam_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(32), nullable=False)
    question_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lead_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    marker_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    tolerance: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    reason: Mapped[str] = mapped_column(String(32), nullable=False, default="selected", server_default="selected")
    second_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    second_comment: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    second_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # snapshot of the first mark when moderation happened (the live first mark is attempt.grades)
    first_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_comment: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    first_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    first_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ai_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    moderated_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    moderated_comment: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    moderated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
