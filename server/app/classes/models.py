"""Tables for classes (Google-Classroom style). Created by db.create_all; every column
added later must carry a server_default so app.db._add_missing_columns can ALTER it in."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, false, true
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import BigIntPK
from ..timeutil import utcnow


class SchoolClass(Base):
    """A teacher's class. Students join with `join_code` or are added by the teacher."""
    __tablename__ = "classes"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    section: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    term: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    join_code: Mapped[str] = mapped_column(String(8), unique=True, nullable=False)
    join_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                               server_default=true())
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                           server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ClassMember(Base):
    __tablename__ = "class_members"
    __table_args__ = (UniqueConstraint("class_id", "user_id", name="uq_class_member"),)
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id", ondelete="CASCADE"), index=True,
                                          nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # "teacher" (added by username/email/CSV) | "code" (joined with the class code) | "lms"
    added_by: Mapped[str] = mapped_column(String(8), nullable=False, default="teacher",
                                          server_default="teacher")


class ExamClass(Base):
    """Exam <-> class assignment (many-to-many)."""
    __tablename__ = "exam_classes"
    __table_args__ = (UniqueConstraint("exam_id", "class_id", name="uq_exam_class"),)
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    exam_id: Mapped[str] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True,
                                         nullable=False)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id", ondelete="CASCADE"), index=True,
                                          nullable=False)
