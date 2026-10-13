"""Audit 2026-10-12 S-03: which students may see and take an exam.

An exam belongs to a class (classes / class_members) or to a linked LMS course
(lms_course_members). A linked course decides on its own (its roster is the source of
truth); without a link the class decides. Only members see the exam while it is live,
download its offline package, get the package key and start it. An exam with neither is
not open to anyone (the teacher can't open it, see routes_teacher.open_exam)."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ClassMember, CourseLink, CourseMember, Exam, SchoolClass


def member_of_exam(user_id: int):
    """SQL condition: the student is in the exam's class or linked LMS course."""
    in_class = exists().where(ClassMember.class_id == Exam.class_id, ClassMember.user_id == user_id)
    in_course = exists().where(CourseMember.course_link_id == Exam.course_link_id,
                               CourseMember.user_id == user_id)
    return or_(and_(Exam.course_link_id.is_not(None), in_course),
               and_(Exam.course_link_id.is_(None), in_class))


async def is_member(session: AsyncSession, class_id: int | None, course_link_id: int | None,
                    user_id: int) -> bool:
    if course_link_id is not None:
        return (await session.execute(select(CourseMember.id).where(
            CourseMember.course_link_id == course_link_id, CourseMember.user_id == user_id).limit(1))).first() is not None
    if class_id is not None:
        return (await session.execute(select(ClassMember.id).where(
            ClassMember.class_id == class_id, ClassMember.user_id == user_id).limit(1))).first() is not None
    return False


async def require_member(session: AsyncSession, exam: dict, user_id: int) -> None:
    """403 with the class name unless the student belongs to the exam's class/course.
    `exam` is the cached student view (get_exam_cached)."""
    class_id, link_id = exam.get("class_id"), exam.get("course_link_id")
    if await is_member(session, class_id, link_id, user_id):
        return
    name = None
    if link_id is not None:
        name = (await session.execute(select(CourseLink.name).where(CourseLink.id == link_id))).scalar()
    elif class_id is not None:
        name = (await session.execute(select(SchoolClass.name).where(SchoolClass.id == class_id))).scalar()
    raise HTTPException(403, f"This exam is for {name} students. Ask your teacher for the class code."
                        if name else "This exam is not for your class.")
