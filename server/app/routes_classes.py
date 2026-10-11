"""Classes API (Google-Classroom style): teacher class management, exam assignment,
student join-by-code. Eligibility rule: app.classes.service (module docstring)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .classes.models import ClassMember, ExamClass, SchoolClass
from .classes.service import (MAX_ENTRIES, class_member_users, find_students, normalize_code,
                              parse_csv, split_entries, unique_join_code)
from .db import get_session
from .models import Attempt, CourseLink, CourseMember, Exam, LmsConnection, User
from .scan.models import ExamRoster
from .security import Principal, require_student, require_teacher
from .services import exam_delivery, invalidate_exam
from .store import get_store
from .timeutil import iso, utcnow

router = APIRouter(tags=["classes"])

MAX_WRONG_JOIN = 10
WRONG_JOIN_WINDOW_S = 600


# ---------------------------------------------------------------- schemas
def _clean(v: str | None) -> str | None:
    return v.strip() if isinstance(v, str) else v


class ClassIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    section: str = Field(default="", max_length=64)
    term: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=2000)

    _strip = field_validator("name", "section", "term", "description", mode="before")(_clean)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v:
            raise ValueError("Give the class a name")
        return v


class ClassPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    section: str | None = Field(default=None, max_length=64)
    term: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    join_enabled: bool | None = None
    archived: bool | None = None

    _strip = field_validator("name", "section", "term", "description", mode="before")(_clean)


class MembersIn(BaseModel):
    """Usernames or emails: a list, or pasted text with one per line."""
    entries: list[str] = Field(default_factory=list, max_length=MAX_ENTRIES)
    text: str = Field(default="", max_length=500_000)


class CsvIn(BaseModel):
    csv: str = Field(min_length=1, max_length=2_000_000)


class LmsImportIn(BaseModel):
    course_link_id: int


class ExamClassesIn(BaseModel):
    class_ids: list[int] = Field(default_factory=list, max_length=200)


class JoinIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)


# ---------------------------------------------------------------- helpers
def class_out(c: SchoolClass, member_count: int = 0, exam_count: int = 0) -> dict:
    return {"id": c.id, "name": c.name, "section": c.section or "", "term": c.term or "",
            "description": c.description or "", "join_code": c.join_code,
            "join_enabled": bool(c.join_enabled), "archived": bool(c.archived),
            "created_at": iso(c.created_at), "member_count": int(member_count),
            "exam_count": int(exam_count)}


def student_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "name": u.name,
            "email": getattr(u, "email", None) or None}


async def _counts(session: AsyncSession, ids: list[int]) -> tuple[dict[int, int], dict[int, int]]:
    if not ids:
        return {}, {}
    members = dict((await session.execute(
        select(ClassMember.class_id, func.count(ClassMember.id))
        .where(ClassMember.class_id.in_(ids)).group_by(ClassMember.class_id))).all())
    exams = dict((await session.execute(
        select(ExamClass.class_id, func.count(ExamClass.id))
        .where(ExamClass.class_id.in_(ids)).group_by(ExamClass.class_id))).all())
    return members, exams


async def _out(session: AsyncSession, c: SchoolClass) -> dict:
    m, e = await _counts(session, [c.id])
    return class_out(c, m.get(c.id, 0), e.get(c.id, 0))


async def _own_class(session: AsyncSession, class_id: int, p: Principal) -> SchoolClass:
    c = await session.get(SchoolClass, class_id)
    if c is None or c.teacher_id != p.user_id:
        raise HTTPException(404, "Class not found")
    return c


async def _own_exam(session: AsyncSession, exam_id: str, p: Principal) -> Exam:
    e = await session.get(Exam, exam_id)
    if e is None or e.teacher_id != p.user_id:
        raise HTTPException(404, "Exam not found")
    return e


async def _add_members(session: AsyncSession, c: SchoolClass, users: list[User], how: str
                       ) -> tuple[list[User], list[User]]:
    """Insert memberships; returns (added, already members)."""
    have = set((await session.execute(
        select(ClassMember.user_id).where(ClassMember.class_id == c.id))).scalars())
    added, already, seen = [], [], set()
    now = utcnow()
    for u in users:
        if u.id in seen:
            continue
        seen.add(u.id)
        if u.id in have:
            already.append(u)
            continue
        session.add(ClassMember(class_id=c.id, user_id=u.id, joined_at=now, added_by=how))
        added.append(u)
    try:
        await session.commit()
    except IntegrityError:  # a concurrent add of the same student: fall back one by one
        await session.rollback()
        added = []
        for u in [u for u in users if u.id not in have]:
            try:
                session.add(ClassMember(class_id=c.id, user_id=u.id, joined_at=now, added_by=how))
                await session.commit()
                added.append(u)
            except IntegrityError:
                await session.rollback()
    return added, already


def _add_result(added: list[User], already: list[User], not_found: list[str], cls: dict) -> dict:
    return {"added": [student_out(u) for u in added], "already": [student_out(u) for u in already],
            "not_found": not_found, "class": cls}


# ---------------------------------------------------------------- teacher: classes
@router.get("/api/classes")
async def list_classes(archived: bool = Query(default=False),
                       p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    rows = list((await session.execute(
        select(SchoolClass).where(SchoolClass.teacher_id == p.user_id,
                                  SchoolClass.archived == archived)
        .order_by(SchoolClass.name, SchoolClass.id))).scalars())
    m, e = await _counts(session, [c.id for c in rows])
    return [class_out(c, m.get(c.id, 0), e.get(c.id, 0)) for c in rows]


@router.post("/api/classes", status_code=201)
async def create_class(body: ClassIn, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    for _ in range(3):
        c = SchoolClass(teacher_id=p.user_id, name=body.name, section=body.section,
                        term=body.term, description=body.description,
                        join_code=await unique_join_code(session), join_enabled=True,
                        archived=False, created_at=utcnow())
        session.add(c)
        try:
            await session.commit()
            return class_out(c)
        except IntegrityError:  # join code taken concurrently
            await session.rollback()
    raise HTTPException(503, "Could not make a class code, try again")


@router.get("/api/classes/{class_id}")
async def get_class(class_id: int, p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    c = await _own_class(session, class_id, p)
    members = (await session.execute(
        select(User, ClassMember.joined_at, ClassMember.added_by)
        .join(ClassMember, ClassMember.user_id == User.id)
        .where(ClassMember.class_id == c.id).order_by(User.name, User.username))).all()
    exams = (await session.execute(
        select(Exam).join(ExamClass, ExamClass.exam_id == Exam.id)
        .where(ExamClass.class_id == c.id).order_by(Exam.created_at.desc(), Exam.id))).scalars().all()
    return {**class_out(c, len(members), len(exams)),
            "students": [{**student_out(u), "joined_at": iso(at), "added_by": how}
                         for u, at, how in members],
            "exams": [{"id": e.id, "title": e.title, "status": e.status, "course": e.course or "",
                       "delivery": exam_delivery(e), "scheduled_at": iso(e.scheduled_at),
                       "created_at": iso(e.created_at)} for e in exams]}


@router.patch("/api/classes/{class_id}")
async def update_class(class_id: int, body: ClassPatch, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    c = await _own_class(session, class_id, p)
    for f in ("name", "section", "term", "description", "join_enabled", "archived"):
        v = getattr(body, f)
        if v is not None:
            setattr(c, f, v)
    await session.commit()
    return await _out(session, c)


@router.post("/api/classes/{class_id}/join-code")
async def regenerate_code(class_id: int, p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    """New join code; the old one stops working at once."""
    c = await _own_class(session, class_id, p)
    for _ in range(3):
        c.join_code = await unique_join_code(session)
        try:
            await session.commit()
            return await _out(session, c)
        except IntegrityError:
            await session.rollback()
            c = await _own_class(session, class_id, p)
    raise HTTPException(503, "Could not make a class code, try again")


# ---------------------------------------------------------------- teacher: members
@router.post("/api/classes/{class_id}/members")
async def add_members(class_id: int, body: MembersIn, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    c = await _own_class(session, class_id, p)
    entries = split_entries("\n".join([*body.entries, body.text]))[:MAX_ENTRIES]
    if not entries:
        raise HTTPException(422, "Type at least one username or email")
    found, missing = await find_students(session, entries)
    added, already = await _add_members(session, c, list(found.values()), "teacher")
    return _add_result(added, already, missing, await _out(session, c))


@router.post("/api/classes/{class_id}/members/csv")
async def add_members_csv(class_id: int, body: CsvIn, p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    """Roster CSV with name + username/email columns (header row), or one id per row."""
    c = await _own_class(session, class_id, p)
    rows = parse_csv(body.csv)
    if not rows:
        raise HTTPException(422, "The file has no rows")
    found, _ = await find_students(session, {i for ids, _ in rows for i in ids})
    users, missing = [], []
    for ids, label in rows:
        u = next((found[i] for i in ids if i in found), None)
        if u is None:
            missing.append(label)
        else:
            users.append(u)
    added, already = await _add_members(session, c, users, "teacher")
    return _add_result(added, already, missing, await _out(session, c))


@router.post("/api/classes/{class_id}/members/lms")
async def import_lms(class_id: int, body: LmsImportIn, p: Principal = Depends(require_teacher),
                     session: AsyncSession = Depends(get_session)):
    """Copy the members of a linked LMS course (already synced; the LMS is not called)."""
    c = await _own_class(session, class_id, p)
    link = (await session.execute(
        select(CourseLink).join(LmsConnection, LmsConnection.id == CourseLink.connection_id)
        .where(CourseLink.id == body.course_link_id,
               LmsConnection.teacher_id == p.user_id))).scalar_one_or_none()
    if link is None:
        raise HTTPException(404, "Linked course not found")
    users = list((await session.execute(
        select(User).join(CourseMember, CourseMember.user_id == User.id)
        .where(CourseMember.course_link_id == link.id, User.role == "student")
        .order_by(User.name, User.username))).scalars())
    added, already = await _add_members(session, c, users, "lms")
    return _add_result(added, already, [], await _out(session, c))


@router.delete("/api/classes/{class_id}/members/{user_id}", status_code=204)
async def remove_member(class_id: int, user_id: int, p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    c = await _own_class(session, class_id, p)
    res = await session.execute(delete(ClassMember).where(ClassMember.class_id == c.id,
                                                          ClassMember.user_id == user_id))
    await session.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "Student is not in this class")
    return Response(status_code=204)


# ---------------------------------------------------------------- teacher: exam assignment
async def _exam_classes_out(session: AsyncSession, e: Exam) -> dict:
    classes = list((await session.execute(
        select(SchoolClass).join(ExamClass, ExamClass.class_id == SchoolClass.id)
        .where(ExamClass.exam_id == e.id).order_by(SchoolClass.name, SchoolClass.id))).scalars())
    m, _ = await _counts(session, [c.id for c in classes])
    members = await class_member_users(session, e.id)
    has_roster = bool((await session.execute(
        select(ExamRoster.id).where(ExamRoster.exam_id == e.id).limit(1))).first())
    if has_roster:
        audience = "roster"
    elif classes:
        audience = "classes"
    elif e.course_link_id:
        audience = "course"
    else:
        audience = "open"
    return {"exam_id": e.id,
            "classes": [{"id": c.id, "name": c.name, "section": c.section or "",
                         "term": c.term or "", "archived": bool(c.archived),
                         "member_count": int(m.get(c.id, 0))} for c in classes],
            "student_count": len(members or []),
            "roster_override": has_roster, "audience": audience}


@router.get("/api/exams/{exam_id}/classes")
async def get_exam_classes(exam_id: str, p: Principal = Depends(require_teacher),
                           session: AsyncSession = Depends(get_session)):
    return await _exam_classes_out(session, await _own_exam(session, exam_id, p))


@router.put("/api/exams/{exam_id}/classes")
async def put_exam_classes(exam_id: str, body: ExamClassesIn, p: Principal = Depends(require_teacher),
                           session: AsyncSession = Depends(get_session)):
    """Replace the exam's classes. Allowed in any status (it is who may start it)."""
    e = await _own_exam(session, exam_id, p)
    wanted = sorted(set(body.class_ids))
    if wanted:
        owned = set((await session.execute(
            select(SchoolClass.id).where(SchoolClass.id.in_(wanted),
                                         SchoolClass.teacher_id == p.user_id))).scalars())
        if owned != set(wanted):
            raise HTTPException(404, "Class not found")
    have = set((await session.execute(
        select(ExamClass.class_id).where(ExamClass.exam_id == e.id))).scalars())
    if have != set(wanted):
        await session.execute(delete(ExamClass).where(ExamClass.exam_id == e.id,
                                                      ExamClass.class_id.not_in(wanted)))
        for cid in set(wanted) - have:
            session.add(ExamClass(exam_id=e.id, class_id=cid))
        e.updated_at = utcnow()
        try:
            await session.commit()
        except IntegrityError:  # concurrent identical PUT
            await session.rollback()
        await invalidate_exam(get_store(), e.id)
    return await _exam_classes_out(session, e)


# ---------------------------------------------------------------- student
def _student_exam(e: Exam, a_status: str | None, a_id: str | None) -> dict:
    return {"id": e.id, "title": e.title, "course": e.course or "", "status": e.status,
            "scheduled_at": iso(e.scheduled_at), "duration_min": e.duration_min,
            "delivery": exam_delivery(e),
            "attempt": {"attempt_id": a_id, "status": a_status} if a_id else None}


@router.get("/api/student/classes")
async def my_classes(p: Principal = Depends(require_student),
                     session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(SchoolClass, ClassMember.joined_at, User.name)
        .join(ClassMember, ClassMember.class_id == SchoolClass.id)
        .join(User, User.id == SchoolClass.teacher_id)
        .where(ClassMember.user_id == p.user_id, SchoolClass.archived.is_(False))
        .order_by(SchoolClass.name, SchoolClass.id))).all()
    ids = [c.id for c, _, _ in rows]
    exams: dict[int, list[dict]] = {}
    if ids:
        # Exams students may know about: live / closed, or drafts with a planned date.
        # Paper exams are printed and never appear in the app.
        for cid, e, a_id, a_status in (await session.execute(
                select(ExamClass.class_id, Exam, Attempt.id, Attempt.status)
                .join(Exam, Exam.id == ExamClass.exam_id)
                .outerjoin(Attempt, and_(Attempt.exam_id == Exam.id,
                                         Attempt.student_id == p.user_id))
                .where(ExamClass.class_id.in_(ids))
                .order_by(Exam.created_at.desc(), Exam.id))).all():
            if exam_delivery(e) == "paper":
                continue
            if e.status == "draft" and e.scheduled_at is None:
                continue
            exams.setdefault(cid, []).append(_student_exam(e, a_status, a_id))
    return [{"id": c.id, "name": c.name, "section": c.section or "", "term": c.term or "",
             "description": c.description or "", "teacher_name": tname,
             "joined_at": iso(joined), "exams": exams.get(c.id, [])}
            for c, joined, tname in rows]


@router.post("/api/student/classes/join")
async def join_class(body: JoinIn, p: Principal = Depends(require_student),
                     session: AsyncSession = Depends(get_session)):
    store = get_store()
    lock_key, count_key = f"joinlock:{p.user_id}", f"wrongjoin:{p.user_id}"
    if await store.get(lock_key):
        raise HTTPException(429, "Too many wrong class codes, wait 10 minutes")
    code = normalize_code(body.code)
    c = (await session.execute(
        select(SchoolClass).where(SchoolClass.join_code == code))).scalar_one_or_none()
    if c is None or c.archived:
        if await store.incr(count_key, WRONG_JOIN_WINDOW_S) >= MAX_WRONG_JOIN:
            await store.set(lock_key, "1", WRONG_JOIN_WINDOW_S)
            await store.delete(count_key)
        raise HTTPException(404, "No class has this code. Check it with your teacher.")
    already = (await session.execute(
        select(ClassMember.id).where(ClassMember.class_id == c.id,
                                     ClassMember.user_id == p.user_id))).first()
    if not already:
        if not c.join_enabled:
            raise HTTPException(403, "Joining this class is turned off. Ask your teacher.")
        session.add(ClassMember(class_id=c.id, user_id=p.user_id, joined_at=utcnow(),
                                added_by="code"))
        try:
            await session.commit()
        except IntegrityError:  # double submit
            await session.rollback()
    await store.delete(count_key)
    tname = (await session.execute(select(User.name).where(User.id == c.teacher_id))).scalar()
    return {"id": c.id, "name": c.name, "section": c.section or "", "term": c.term or "",
            "teacher_name": tname or "", "already_member": bool(already)}
