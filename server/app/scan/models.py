"""Tables for Scan & Grade (created by db.create_all; plain columns so the tiny
forward-only migration in app.db can add new ones later)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import BigIntPK
from ..timeutil import utcnow


class PrintLayout(Base):
    """Latest printed layout of an exam (same geometry for every student's booklet)."""
    __tablename__ = "scan_print_layouts"
    exam_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    layout: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    questions_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ExamRoster(Base):
    """Students picked for a paper exam. No rows = default roster (LMS course or all)."""
    __tablename__ = "scan_exam_roster"
    __table_args__ = (UniqueConstraint("exam_id", "user_id", name="uq_roster_exam_user"),)
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    exam_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)


class ScanBatch(Base):
    __tablename__ = "scan_batches"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    exam_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    # queued -> reading -> grading -> done | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    filename: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    sources: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # file paths on disk
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Audit L-06: the worker running this batch refreshes heartbeat_at; a batch whose
    # heartbeat went stale (server restarted or crashed) is picked up again. source_counts
    # holds the pages per uploaded file (-1 = unreadable) so a resumed run skips pages it
    # already stored.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_counts: Mapped[list | None] = mapped_column(JSON, nullable=True)


class ScanPage(Base):
    __tablename__ = "scan_pages"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    exam_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # order in the upload
    # matched (QR read) | assigned (by hand) | unmatched | ignored
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unmatched")
    student_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    qr_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rotation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    preview: Mapped[str | None] = mapped_column(String(200), nullable=True)
    name_crop: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # canonical page image (deskewed, A4 at the scan dpi) kept so a hand assignment can crop
    source_image: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # {qid: {"image": url, "mcq": idx|None, "marks": ..., "blank": bool, "fill": [...]}}
    crops: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
