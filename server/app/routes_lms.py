"""Slice 5 — LMS sync: connections, course links + roster sync, exam link, grade push.

Credentials are sealed server-side (app.lms.secretbox) and never returned. Every
outbound LMS call runs through app.lms providers (httpx async, 15 s timeout, retries),
only on teacher requests — never on the student hot path.
"""
from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .grading import exam_questions, grade_attempt, num
from .lms import (PROVIDERS, GradeRow, LmsAuthError, LmsError, LmsProvider, make_provider,
                  normalize_base_url, provider_list, username_from)
from .lms.base import short
from .lms.secretbox import open_, seal
from .models import Attempt, CourseLink, CourseMember, Exam, LmsConnection, User
from .security import Principal, require_teacher
from .services import exam_out_full, invalidate_exam
from .store import get_store
from .timeutil import iso, utcnow

router = APIRouter(tags=["lms"])

IMPORTED_PASSWORD = "!lms-import"   # not a pbkdf2 hash: verify_password() always fails
LOCK_TTL_S = 300


# ---------------------------------------------------------------- schemas
class ConnectionIn(BaseModel):
    provider: Literal["canvas", "moodle", "blackboard", "brightspace"]
    base_url: str = Field(min_length=1, max_length=500)
    token: str = Field(min_length=1, max_length=4000)
    label: str = Field(default="", max_length=120)


class ExamLmsIn(BaseModel):
    course_link_id: int | None = None


# ---------------------------------------------------------------- helpers
def _allow_private() -> bool:
    return os.environ.get("PROCTORA_LMS_ALLOW_PRIVATE") == "1"


def _secret(request: Request) -> str:
    return request.app.state.settings.secret


def connection_out(c: LmsConnection, course_count: int = 0) -> dict:
    cls = PROVIDERS.get(c.provider)
    return {"id": c.id, "provider": c.provider, "provider_name": cls.info.name if cls else c.provider,
            "status": cls.info.status if cls else "beta",
            "base_url": c.base_url, "label": c.label or "", "lms_user_name": c.lms_user_name or "",
            "created_at": iso(c.created_at), "last_sync_at": iso(c.last_sync_at),
            "last_error": c.last_error, "course_count": course_count}


def link_out(link: CourseLink, conn: LmsConnection, member_count: int, exam_count: int = 0) -> dict:
    cls = PROVIDERS.get(conn.provider)
    return {"id": link.id, "connection_id": conn.id, "provider": conn.provider,
            "provider_name": cls.info.name if cls else conn.provider,
            "lms_course_id": link.lms_course_id, "name": link.name, "code": link.code or "",
            "member_count": member_count, "exam_count": exam_count,
            "last_sync_at": iso(link.last_sync_at), "last_error": link.last_error}


async def _own_connection(session: AsyncSession, cid: int, p: Principal) -> LmsConnection:
    c = await session.get(LmsConnection, cid)
    if c is None or c.teacher_id != p.user_id:
        raise HTTPException(404, "Connection not found")
    return c


async def _own_link(session: AsyncSession, link_id: int, p: Principal
                    ) -> tuple[CourseLink, LmsConnection]:
    row = (await session.execute(
        select(CourseLink, LmsConnection).join(LmsConnection, LmsConnection.id == CourseLink.connection_id)
        .where(CourseLink.id == link_id))).first()
    if row is None or row[1].teacher_id != p.user_id:
        raise HTTPException(404, "Linked course not found")
    return row[0], row[1]


async def _own_exam(session: AsyncSession, exam_id: str, p: Principal) -> Exam:
    e = await session.get(Exam, exam_id)
    if e is None or e.teacher_id != p.user_id:
        raise HTTPException(404, "Exam not found")
    return e


def _provider_for(c: LmsConnection, secret: str) -> LmsProvider:
    try:
        token = open_(secret, c.credential)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(409, "The saved token can no longer be read. Remove this "
                                 "connection and connect again.")
    return make_provider(c.provider, c.base_url, token)


async def _fail(session: AsyncSession, exc: LmsError, *objs) -> HTTPException:
    """Record the error on the connection/link rows and turn it into a 502 for the UI.
    (Never 401: that would log the teacher out of FairPlay.)"""
    msg = short(exc.message, 480)
    if isinstance(exc, LmsAuthError):
        msg = short(msg + " Remove the connection and connect again with a new token.", 480)
    for o in objs:
        if o is not None:
            o.last_error = msg
    await session.commit()
    return HTTPException(502, msg)


async def _lock(key: str) -> None:
    store = get_store()
    if await store.incr(key, LOCK_TTL_S) > 1:
        raise HTTPException(409, "Already running — wait a moment and try again")


async def _unlock(key: str) -> None:
    await get_store().delete(key)


async def _member_counts(session: AsyncSession, link_ids: list[int]) -> dict[int, int]:
    if not link_ids:
        return {}
    rows = await session.execute(
        select(CourseMember.course_link_id, func.count(CourseMember.id))
        .where(CourseMember.course_link_id.in_(link_ids)).group_by(CourseMember.course_link_id))
    return {lid: int(n) for lid, n in rows.all()}


async def course_gate(session: AsyncSession, link_id: int, user_id: int) -> None:
    """Student start check for an exam linked to an LMS course (one indexed lookup)."""
    ok = (await session.execute(
        select(CourseMember.id).where(CourseMember.course_link_id == link_id,
                                      CourseMember.user_id == user_id).limit(1))).first()
    if ok:
        return
    name = (await session.execute(
        select(CourseLink.name).where(CourseLink.id == link_id))).scalar_one_or_none()
    raise HTTPException(403, f"This exam is for {name or 'the linked course'} students")


# ---------------------------------------------------------------- roster sync
async def sync_roster(session: AsyncSession, provider: LmsProvider, conn: LmsConnection,
                      link: CourseLink) -> dict:
    students = await provider.list_students(link.lms_course_id)
    lms_ids = [s.lms_user_id for s in students]

    members = {m.lms_user_id: m for m in (await session.execute(
        select(CourseMember).where(CourseMember.course_link_id == link.id))).scalars()}

    # Same person already imported through another course of the same LMS -> same user.
    known: dict[str, int] = {}
    if lms_ids:
        rows = await session.execute(
            select(CourseMember.lms_user_id, CourseMember.user_id)
            .join(CourseLink, CourseLink.id == CourseMember.course_link_id)
            .join(LmsConnection, LmsConnection.id == CourseLink.connection_id)
            .where(LmsConnection.provider == conn.provider, LmsConnection.base_url == conn.base_url,
                   CourseMember.lms_user_id.in_(lms_ids)))
        known = {lid: uid for lid, uid in rows.all()}
    claimed_uids = set(known.values())  # local users already bound to some LMS identity

    wanted = {s.lms_user_id: username_from(s.login_id, s.email, f"{conn.provider}{s.lms_user_id}")
              for s in students}
    by_name = {u.username: u for u in (await session.execute(
        select(User).where(User.username.in_(set(wanted.values()))))).scalars()} if wanted else {}
    user_ids = {m.user_id for m in members.values()} | set(known.values())
    users = {u.id: u for u in (await session.execute(
        select(User).where(User.id.in_(user_ids)))).scalars()} if user_ids else {}

    added = updated = 0
    taken: set[str] = set()
    for s in students:
        m = members.get(s.lms_user_id)
        uid = m.user_id if m else known.get(s.lms_user_id)
        if uid is None:
            base_name = wanted[s.lms_user_id]
            u = by_name.get(base_name)
            if (u is not None and u.role == "student" and u.id not in claimed_uids
                    and base_name not in taken):
                uid = u.id   # existing FairPlay student with the same login id
            else:
                username = await _free_username(session, base_name, taken)
                u = User(username=username, name=(s.name or username)[:200], role="student",
                         password_hash=IMPORTED_PASSWORD, created_at=utcnow())
                session.add(u)
                await session.flush()
                uid = u.id
                users[uid] = u
            taken.add(base_name)
            claimed_uids.add(uid)
        if m is None:
            session.add(CourseMember(course_link_id=link.id, user_id=uid, lms_user_id=s.lms_user_id))
            added += 1
        u = users.get(uid)
        if (u is not None and u.password_hash == IMPORTED_PASSWORD and s.name
                and u.name != s.name[:200]):
            u.name = s.name[:200]
            updated += 1

    gone = [m.id for lid, m in members.items() if lid not in set(lms_ids)]
    if gone:
        await session.execute(delete(CourseMember).where(CourseMember.id.in_(gone)))
    now = utcnow()
    link.last_sync_at = conn.last_sync_at = now
    link.last_error = conn.last_error = None
    await session.commit()
    return {"added": added, "updated": updated, "removed": len(gone), "total": len(students)}


async def _free_username(session: AsyncSession, base_name: str, taken: set[str]) -> str:
    base_name = base_name[:58] or "student"
    for i in range(1, 1000):
        cand = base_name if i == 1 else f"{base_name}-{i}"
        if cand in taken:
            continue
        exists = (await session.execute(select(User.id).where(User.username == cand))).first()
        if not exists:
            taken.add(cand)
            return cand
    raise HTTPException(500, "Could not pick a username")


# ---------------------------------------------------------------- providers / connections
@router.get("/api/lms/providers")
async def providers(p: Principal = Depends(require_teacher)):
    return provider_list()


@router.get("/api/lms/connections")
async def list_connections(p: Principal = Depends(require_teacher),
                           session: AsyncSession = Depends(get_session)):
    conns = (await session.execute(
        select(LmsConnection).where(LmsConnection.teacher_id == p.user_id)
        .order_by(LmsConnection.created_at, LmsConnection.id))).scalars().all()
    counts = dict((await session.execute(
        select(CourseLink.connection_id, func.count(CourseLink.id))
        .where(CourseLink.connection_id.in_([c.id for c in conns]))
        .group_by(CourseLink.connection_id))).all()) if conns else {}
    return [connection_out(c, int(counts.get(c.id, 0))) for c in conns]


@router.post("/api/lms/connections", status_code=201)
async def create_connection(body: ConnectionIn, request: Request,
                            p: Principal = Depends(require_teacher),
                            session: AsyncSession = Depends(get_session)):
    try:
        base_url = normalize_base_url(body.base_url, _allow_private())
    except LmsError as exc:
        raise HTTPException(422, exc.message)
    token = body.token.strip()
    if not token:
        raise HTTPException(422, "Paste the access token")
    try:
        async with make_provider(body.provider, base_url, token) as prov:
            me = await prov.whoami()
    except LmsError as exc:
        raise HTTPException(422, exc.message)
    secret = _secret(request)
    existing = (await session.execute(
        select(LmsConnection).where(LmsConnection.teacher_id == p.user_id,
                                    LmsConnection.provider == body.provider,
                                    LmsConnection.base_url == base_url))).scalars().first()
    c = existing or LmsConnection(teacher_id=p.user_id, provider=body.provider, base_url=base_url,
                                  created_at=utcnow())
    c.credential = seal(secret, token)
    c.label = body.label.strip() or (PROVIDERS[body.provider].info.name + " — "
                                     + base_url.split("://", 1)[-1])[:120]
    c.lms_user_id = me.id[:64]
    c.lms_user_name = (me.name or me.login)[:200]
    c.last_error = None
    if existing is None:
        session.add(c)
    await session.commit()
    n = (await session.execute(select(func.count(CourseLink.id))
                               .where(CourseLink.connection_id == c.id))).scalar_one()
    return connection_out(c, int(n))


@router.delete("/api/lms/connections/{cid}", status_code=204)
async def delete_connection(cid: int, p: Principal = Depends(require_teacher),
                            session: AsyncSession = Depends(get_session)):
    c = await _own_connection(session, cid, p)
    link_ids = list((await session.execute(
        select(CourseLink.id).where(CourseLink.connection_id == c.id))).scalars())
    exam_ids: list[str] = []
    if link_ids:
        exam_ids = list((await session.execute(
            select(Exam.id).where(Exam.course_link_id.in_(link_ids)))).scalars())
        await session.execute(update(Exam).where(Exam.course_link_id.in_(link_ids))
                              .values(course_link_id=None, lms_assignment_id=None))
        await session.execute(delete(CourseMember).where(CourseMember.course_link_id.in_(link_ids)))
        await session.execute(delete(CourseLink).where(CourseLink.id.in_(link_ids)))
    await session.delete(c)
    await session.commit()
    store = get_store()
    for eid in exam_ids:
        await invalidate_exam(store, eid)
    return Response(status_code=204)


# ---------------------------------------------------------------- courses
@router.get("/api/lms/connections/{cid}/courses")
async def connection_courses(cid: int, request: Request, p: Principal = Depends(require_teacher),
                             session: AsyncSession = Depends(get_session)):
    c = await _own_connection(session, cid, p)
    try:
        async with _provider_for(c, _secret(request)) as prov:
            courses = await prov.list_courses()
    except LmsError as exc:
        raise await _fail(session, exc, c)
    links = {l.lms_course_id: l.id for l in (await session.execute(
        select(CourseLink).where(CourseLink.connection_id == c.id))).scalars()}
    if c.last_error:
        c.last_error = None
        await session.commit()
    return [{**x.as_dict(), "link_id": links.get(x.id)} for x in courses]


@router.post("/api/lms/connections/{cid}/courses/{lms_course_id}/link")
async def link_course(cid: int, lms_course_id: str, request: Request,
                      p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    c = await _own_connection(session, cid, p)
    lms_course_id = lms_course_id.strip()[:64]
    lock = f"lmslink:{c.id}:{lms_course_id}"
    await _lock(lock)
    try:
        prov = _provider_for(c, _secret(request))
        async with prov:
            link = (await session.execute(
                select(CourseLink).where(CourseLink.connection_id == c.id,
                                         CourseLink.lms_course_id == lms_course_id))).scalars().first()
            try:
                courses = await prov.list_courses()
            except LmsError as exc:
                raise await _fail(session, exc, c, link)
            course = next((x for x in courses if x.id == lms_course_id), None)
            if course is None:
                raise HTTPException(404, "That course is not in your LMS course list")
            if link is None:
                link = CourseLink(connection_id=c.id, lms_course_id=lms_course_id,
                                  created_at=utcnow())
                session.add(link)
            link.name = course.name[:300]
            link.code = course.code[:120]
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                link = (await session.execute(
                    select(CourseLink).where(CourseLink.connection_id == c.id,
                                             CourseLink.lms_course_id == lms_course_id))).scalar_one()
            try:
                result = await sync_roster(session, prov, c, link)
            except LmsError as exc:  # raised before any roster change is made
                raise await _fail(session, exc, c, link)
    finally:
        await _unlock(lock)
    n = (await _member_counts(session, [link.id])).get(link.id, 0)
    return {**link_out(link, c, n), "sync": result}


@router.post("/api/lms/courses/{link_id}/sync")
async def resync_course(link_id: int, request: Request, p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    link, c = await _own_link(session, link_id, p)
    lock = f"lmslink:{c.id}:{link.lms_course_id}"
    await _lock(lock)
    try:
        async with _provider_for(c, _secret(request)) as prov:
            try:
                result = await sync_roster(session, prov, c, link)
            except LmsError as exc:  # raised before any roster change is made
                raise await _fail(session, exc, c, link)
    finally:
        await _unlock(lock)
    n = (await _member_counts(session, [link.id])).get(link.id, 0)
    return {**link_out(link, c, n), "sync": result}


@router.get("/api/lms/courses")
async def linked_courses(p: Principal = Depends(require_teacher),
                         session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(CourseLink, LmsConnection).join(LmsConnection, LmsConnection.id == CourseLink.connection_id)
        .where(LmsConnection.teacher_id == p.user_id)
        .order_by(CourseLink.name, CourseLink.id))).all()
    ids = [l.id for l, _ in rows]
    counts = await _member_counts(session, ids)
    exams = dict((await session.execute(
        select(Exam.course_link_id, func.count(Exam.id)).where(Exam.course_link_id.in_(ids))
        .group_by(Exam.course_link_id))).all()) if ids else {}
    return [link_out(l, c, counts.get(l.id, 0), int(exams.get(l.id, 0))) for l, c in rows]


# ---------------------------------------------------------------- exams
@router.put("/api/exams/{exam_id}/lms")
async def link_exam(exam_id: str, body: ExamLmsIn, p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    if body.course_link_id is not None:
        await _own_link(session, body.course_link_id, p)
    if e.course_link_id != body.course_link_id:
        e.course_link_id = body.course_link_id
        e.lms_assignment_id = None   # a different course needs its own assignment
        e.updated_at = utcnow()
        await session.commit()
        await invalidate_exam(get_store(), exam_id)
    return await exam_out_full(session, e)


@router.post("/api/exams/{exam_id}/lms/push")
async def push_grades(exam_id: str, request: Request, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    if not e.course_link_id:
        raise HTTPException(409, "Link this exam to an LMS course first")
    if not e.grades_released:
        raise HTTPException(409, "Release the grades first")
    link, c = await _own_link(session, e.course_link_id, p)
    qs = exam_questions(e)
    total = float(sum(float(q.get("points", 0) or 0) for q in qs)) or float(e.total_points or 0)
    if total <= 0:
        raise HTTPException(409, "This exam has no points to send")

    rows = (await session.execute(
        select(Attempt, User.name, CourseMember.lms_user_id)
        .join(User, User.id == Attempt.student_id)
        .outerjoin(CourseMember, (CourseMember.course_link_id == link.id)
                   & (CourseMember.user_id == Attempt.student_id))
        .where(Attempt.exam_id == e.id).order_by(User.name))).all()
    grade_rows: list[GradeRow] = []
    names: dict[str, str] = {}
    skipped = 0
    for a, name, lms_uid in rows:
        if a.status != "submitted" or not lms_uid:
            skipped += 1
            continue
        score, _pending, _ = grade_attempt(qs, a.answers, a.grades)
        grade_rows.append(GradeRow(lms_user_id=lms_uid, score=num(score),
                                   comment=(a.overall_comment or "").strip()))
        names[lms_uid] = name

    lock = f"lmspush:{e.id}"
    await _lock(lock)
    try:
        async with _provider_for(c, _secret(request)) as prov:
            try:
                aid = await prov.ensure_assignment(link.lms_course_id, e.title, num(total),
                                                   e.lms_assignment_id)
            except LmsError as exc:
                raise await _fail(session, exc, c)
            if aid != e.lms_assignment_id:
                e.lms_assignment_id = aid
                await session.commit()
            try:
                errs = await prov.post_grades(link.lms_course_id, aid, grade_rows) if grade_rows else []
            except LmsError as exc:
                raise await _fail(session, exc, c)
    finally:
        await _unlock(lock)
    failed = sum(1 for x in errs if not x.sent)
    c.last_sync_at = utcnow()
    c.last_error = None
    await session.commit()
    return {"pushed": len(grade_rows) - failed, "skipped": skipped,
            "errors": [{"student": names.get(x.lms_user_id, x.lms_user_id), "detail": x.detail}
                       for x in errs],
            "lms_assignment_id": aid}
