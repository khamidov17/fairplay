"""Phase 2 (AI paper grading) tables: per-exam answer key / rubric / page mapping, and
short-lived phone-capture tokens. Plain columns with server defaults so app.db's tiny
forward-only migration can add them to an existing database."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, false, text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..timeutil import utcnow


class ScanExamConfig(Base):
    """One row per exam: answer key + per-question rubric, and the manual page mapping
    for answer sheets that were not printed by FairPlay."""
    __tablename__ = "scan_exam_config"
    exam_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # see app.scan.answer_key for the shape
    answer_key: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    # {"pages_per_student": int, "question_pages": {qid: page_no}}
    mapping: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class CaptureToken(Base):
    """Phone capture link: the phone uploads photos for ONE exam without logging in.
    Only the sha256 of the token is stored."""
    __tablename__ = "scan_capture_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    exam_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    teacher_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unprinted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    uploads: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    photos: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
