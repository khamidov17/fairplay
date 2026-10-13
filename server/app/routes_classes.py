"""Audit 2026-10-12 S-03: classes. Teachers create a class and share its join code (or add
students by username); students join; every exam belongs to one class."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import ClassMember, Exam, SchoolClass, User
from .security import Principal, require_student, require_teacher
from .store import get_store
from .timeutil import iso

teacher_router = APIRouter(prefix="/api/classes", tags=["classes"])
student_router = APIRouter(prefix="/api/student/classes", tags=["classes"])

# No 0/O/1/I/L: read aloud or copied from a board.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
MAX_WRONG_JOINS, WRONG_JOIN_WINDOW_S = 10, 600
MAX_ADD = 2000


def new_join_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def normalize_code(raw: str) -> str:
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def show_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) == CODE_LEN else code


class ClassIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    term: str | None = Field(default=None, max_length=80)   # "Fall 2026 · Section 2"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("name must not be empty")
        return v

    @field_validator("term")
    @classmethod
    def _term(cls, v: str | None) -> str | None:
        v = " ".join((v or "").split())
        return v or None


class MembersIn(BaseModel):
    usernames: list[str] = Field(min_length=1, max_length=MAX_ADD)


class JoinIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)


async def _own_class(session: AsyncSession, class_id: int, p: Principal) -> SchoolClass:
    c = await session.get(SchoolClass, class_id)
    if c is None or c.teacher_id != p.user_id:
        raise HTTPException(404, "Class not found")
    return c


async def _counts(session: AsyncSession, ids: list[int]) -> tuple[dict, dict]:
    if not ids:
        return {}, {}
    members = dict((await session.execute(
        select(ClassMember.class_id, func.count(ClassMember.id))
        .where(ClassMember.class_id.in_(ids)).group_by(ClassMember.class_id))).all())
    exams = dict((await session.execute(
        select(Exam.class_id, func.count(Exam.id))
        .where(Exam.class_id.in_(ids)).group_by(Exam.class_id))).all())
    return members, exams


def _out(c: SchoolClass, members: int = 0, exams: int = 0, teacher_name: str = "") -> dict:
    return {"id": c.id, "name": c.name, "term": c.term or "", "teacher_name": teacher_name,
            "join_code": show_code(c.join_code),
            "member_count": int(members), "exam_count": int(exams), "created_at": iso(c.created_at)}


async def _teacher_name(session: AsyncSession, teacher_id: int) -> str:
    u = await session.get(User, teacher_id)
    return (u.name or u.username) if u else ""


async def _insert_class(session: AsyncSession, teacher_id: int, name: str,
                        term: str | None = None) -> SchoolClass:
    for _ in range(5):                   # join codes are unique; retry the rare clash
        c = SchoolClass(teacher_id=teacher_id, name=name, term=term, join_code=new_join_code())
        session.add(c)
        try:
            await session.commit()
            return c
        except IntegrityError:
            await session.rollback()
    raise HTTPException(500, "Could not make a class code, try again")


# ---------------------------------------------------------------- teacher
@teacher_router.get("")
async def list_classes(p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    rows = list((await session.execute(
        select(SchoolClass).where(SchoolClass.teacher_id == p.user_id)
        .order_by(SchoolClass.name, SchoolClass.id))).scalars())
    members, exams = await _counts(session, [c.id for c in rows])
    who = await _teacher_name(session, p.user_id)
    return [_out(c, members.get(c.id, 0), exams.get(c.id, 0), who) for c in rows]


@teacher_router.post("", status_code=201)
async def create_class(body: ClassIn, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    c = await _insert_class(session, p.user_id, body.name, body.term)
    return _out(c, teacher_name=await _teacher_name(session, p.user_id))


@teacher_router.get("/{class_id}")
async def get_class(class_id: int, p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    c = await _own_class(session, class_id, p)
    rows = (await session.execute(
        select(User.username, User.name, ClassMember.joined_at)
        .join(ClassMember, ClassMember.user_id == User.id)
        .where(ClassMember.class_id == c.id).order_by(User.name, User.username))).all()
    _, exams = await _counts(session, [c.id])
    return {**_out(c, len(rows), exams.get(c.id, 0), await _teacher_name(session, p.user_id)),
            "members": [{"username": u, "name": n, "joined_at": iso(j)} for u, n, j in rows]}


@teacher_router.put("/{class_id}")
async def rename_class(class_id: int, body: ClassIn, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    c = await _own_class(session, class_id, p)
    c.name = body.name
    if "term" in body.model_fields_set:
        c.term = body.term
    await session.commit()
    members, exams = await _counts(session, [c.id])
    return _out(c, members.get(c.id, 0), exams.get(c.id, 0), await _teacher_name(session, p.user_id))


@teacher_router.post("/{class_id}/join-code")
async def new_code(class_id: int, p: Principal = Depends(require_teacher),
                   session: AsyncSession = Depends(get_session)):
    """New join code (the old one stops working; members stay)."""
    c = await _own_class(session, class_id, p)
    for _ in range(5):
        c.join_code = new_join_code()
        try:
            await session.commit()
            break
        except IntegrityError:
            await session.rollback()
            c = await _own_class(session, class_id, p)
    members, exams = await _counts(session, [c.id])
    return _out(c, members.get(c.id, 0), exams.get(c.id, 0))


@teacher_router.post("/{class_id}/members")
async def add_members(class_id: int, body: MembersIn, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    """Add students by username (pasted list). Unknown names and teachers are reported."""
    c = await _own_class(session, class_id, p)
    wanted = list(dict.fromkeys(u.strip() for u in body.usernames if u.strip()))
    found = dict((await session.execute(
        select(User.username, User.id).where(User.username.in_(wanted), User.role == "student"))).all())
    have = set((await session.execute(
        select(ClassMember.user_id).where(ClassMember.class_id == c.id))).scalars())
    new = [found[u] for u in wanted if u in found and found[u] not in have]
    for uid in new:
        session.add(ClassMember(class_id=c.id, user_id=uid))
    await session.commit()
    return {"added": len(new), "already": sum(1 for u in wanted if u in found) - len(new),
            "unknown": [u for u in wanted if u not in found]}


@teacher_router.delete("/{class_id}/members/{username}", status_code=204)
async def remove_member(class_id: int, username: str, p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    c = await _own_class(session, class_id, p)
    uid = (await session.execute(select(User.id).where(User.username == username))).scalar()
    if uid is not None:
        await session.execute(delete(ClassMember).where(ClassMember.class_id == c.id,
                                                        ClassMember.user_id == uid))
        await session.commit()
    return Response(status_code=204)


@teacher_router.post("/{class_id}/members/{username}/sign-out")
async def sign_out_member(class_id: int, username: str, p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    """Audit S-15: sign a student of this class out on every device (lost laptop,
    shared password). They sign in again with their password."""
    c = await _own_class(session, class_id, p)
    uid = (await session.execute(
        select(User.id).join(ClassMember, ClassMember.user_id == User.id)
        .where(ClassMember.class_id == c.id, User.username == username))).scalar()
    if uid is None:
        raise HTTPException(404, "No such student in this class")
    from .routes_auth import bump_token_version
    await bump_token_version(session, uid)
    return {"ok": True}


@teacher_router.delete("/{class_id}", status_code=204)
async def delete_class(class_id: int, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    c = await _own_class(session, class_id, p)
    n = (await session.execute(select(func.count(Exam.id)).where(Exam.class_id == c.id))).scalar_one()
    if n:
        raise HTTPException(409, f"{n} exam(s) belong to this class. Move or delete them first.")
    await session.execute(delete(ClassMember).where(ClassMember.class_id == c.id))
    await session.delete(c)
    await session.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------- student
@student_router.get("")
async def my_classes(p: Principal = Depends(require_student),
                     session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(SchoolClass.id, SchoolClass.name, SchoolClass.term, User.name, ClassMember.joined_at)
        .join(ClassMember, ClassMember.class_id == SchoolClass.id)
        .join(User, User.id == SchoolClass.teacher_id)
        .where(ClassMember.user_id == p.user_id).order_by(SchoolClass.name))).all()
    return [{"id": i, "name": n, "term": term or "", "teacher": t, "joined_at": iso(j)}
            for i, n, term, t, j in rows]


@student_router.post("/join")
async def join_class(body: JoinIn, p: Principal = Depends(require_student),
                     session: AsyncSession = Depends(get_session)):
    store = get_store()
    wrong_key = f"joinwrong:{p.user_id}"
    if int(await store.get(wrong_key) or 0) >= MAX_WRONG_JOINS:
        raise HTTPException(429, "Too many wrong class codes. Wait 10 minutes.")
    code = normalize_code(body.code)
    c = (await session.execute(select(SchoolClass).where(SchoolClass.join_code == code))).scalar_one_or_none()
    if c is None:
        await store.incr(wrong_key, WRONG_JOIN_WINDOW_S)
        raise HTTPException(404, "No class has this code. Check it with your teacher.")
    out = {"id": c.id, "name": c.name,
           "teacher": (await session.execute(select(User.name).where(User.id == c.teacher_id))).scalar()}
    already = (await session.execute(select(ClassMember.id).where(
        ClassMember.class_id == c.id, ClassMember.user_id == p.user_id))).first()
    if not already:
        session.add(ClassMember(class_id=c.id, user_id=p.user_id))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()      # joined twice at the same moment: fine
    return out


@student_router.delete("/{class_id}", status_code=204)
async def leave_class(class_id: int, p: Principal = Depends(require_student),
                      session: AsyncSession = Depends(get_session)):
    await session.execute(delete(ClassMember).where(ClassMember.class_id == class_id,
                                                    ClassMember.user_id == p.user_id))
    await session.commit()
    return Response(status_code=204)


async def check_class_for_exam(session: AsyncSession, class_id: int | None, p: Principal) -> None:
    """ExamIn.class_id must be one of the teacher's classes."""
    if class_id is None:
        return
    c = await session.get(SchoolClass, class_id)
    if c is None or c.teacher_id != p.user_id:
        raise HTTPException(422, "Pick one of your classes for this exam")

