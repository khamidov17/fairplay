from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint, false, text)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .timeutil import utcnow

# BIGINT autoincrement on Postgres, INTEGER PRIMARY KEY (rowid) on SQLite.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # phase 1a: bumped on password change / "sign out everywhere"; tokens carry the version
    # they were issued with and older ones are refused (app.security.current_principal).
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class Exam(Base):
    __tablename__ = "exams"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True, nullable=False)
    start_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    questions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_points: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # slice 2 (server_default so app.db can ALTER TABLE ADD COLUMN on an existing database)
    course: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    room: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    shuffle_questions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                    server_default=false())
    grades_released: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                  server_default=false())
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # slice 3: external-website exams (SEB-style). mode "questions" keeps the external
    # fields null/[]; mode "external" has no questions and opens external_url in the app.
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="questions",
                                      server_default="questions")
    external_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    allowed_domains: Mapped[list] = mapped_column(JSON, nullable=False, default=list,
                                                  server_default=text("'[]'"))
    finish_url_contains: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # slice 5: LMS sync. Plain (non-FK) columns so db._add_missing_columns can ALTER them in.
    course_link_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    lms_assignment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # slice 6: "computer" | "paper" | "website". NULL on rows from before slice 6: derived
    # from mode by services.exam_delivery (external -> website, else computer).
    delivery: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # phase 1a: per-exam invigilator exit code (teacher-only, issued at open), its offline
    # verifier for the Mac app (JSON {alg,iterations,salt,hash}), and the offline-package
    # content key (base64, 32 bytes; see app.offline).
    exit_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    exit_check: Mapped[str | None] = mapped_column(String(300), nullable=True)
    package_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("exam_id", "student_id", name="uq_attempt_exam_student"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    exam_id: Mapped[str] = mapped_column(ForeignKey("exams.id"), index=True, nullable=False)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="in_progress", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    answers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    client_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=-1)
    receipt: Mapped[str | None] = mapped_column(String(16), nullable=True)
    auto_submitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # slice 2: teacher grades {qid: {points, comment, rubric_scores, graded_at}} + overall comment.
    grades: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    overall_comment: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Denormalised count of pending essay answers (kept in sync on submit/close/grade) so
    # exam lists and the grading overview can aggregate without loading every answer.
    needs_grading: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # slice 6 (Scan & Grade): "computer" or "paper" (answers then hold scan crops, see
    # app.scan) and the AI drafts per question {qid: {points, comment, confidence, ...}}.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="computer",
                                        server_default="computer")
    ai_drafts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict,
                                            server_default=text("'{}'"))


class IntegrityEvent(Base):
    __tablename__ = "integrity_events"
    __table_args__ = (Index("ix_events_exam_attempt", "exam_id", "attempt_id"),
                      Index("ix_events_exam_review", "exam_id", "review"))
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"), index=True,
                                            nullable=False)
    exam_id: Mapped[str] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True,
                                         nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    client_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    server_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    review: Mapped[str] = mapped_column(String(16), nullable=False, default="open", server_default="open")
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="")


# ---------------------------------------------------------------- slice 5: LMS sync
class LmsConnection(Base):
    """A teacher's link to an LMS. `credential` is the API token sealed with
    app.lms.secretbox (never returned by the API)."""
    __tablename__ = "lms_connections"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    credential: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    lms_user_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    lms_user_name: Mapped[str] = mapped_column(String(200), nullable=False, default="",
                                               server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class CourseLink(Base):
    __tablename__ = "lms_course_links"
    __table_args__ = (UniqueConstraint("connection_id", "lms_course_id", name="uq_link_conn_course"),)
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("lms_connections.id", ondelete="CASCADE"),
                                               index=True, nullable=False)
    lms_course_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    code: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class CourseMember(Base):
    __tablename__ = "lms_course_members"
    __table_args__ = (UniqueConstraint("course_link_id", "user_id", name="uq_member_link_user"),
                      UniqueConstraint("course_link_id", "lms_user_id", name="uq_member_link_lms"))
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    course_link_id: Mapped[int] = mapped_column(
        ForeignKey("lms_course_links.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    lms_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
