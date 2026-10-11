"""Classes: join codes, finding students, and the exam eligibility rule.

Eligibility (who may see and start an exam), in order of precedence:
  1. explicit roster (scan_exam_roster rows, PUT /api/exams/{id}/roster) -> only those students
  2. assigned classes (exam_classes rows)  -> only members of any assigned class
  3. linked LMS course (exams.course_link_id) -> only members of that course (slice 5, unchanged)
  4. none of these -> open: any student with the start code (behaviour before classes)
The same order decides the paper-exam roster (routes_scan.roster_of), where step 4 means
"all students".
"""
from __future__ import annotations

import csv
import io
import re
import secrets
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User
from ..scan.models import ExamRoster
from .models import ClassMember, ExamClass, SchoolClass

# No 0/O, 1/I/L: easy to read aloud and to type from a projector.
JOIN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
JOIN_CODE_LEN = 7
NOT_IN_CLASS = "You are not in a class for this exam"
MAX_ENTRIES = 5000


def new_join_code() -> str:
    return "".join(secrets.choice(JOIN_ALPHABET) for _ in range(JOIN_CODE_LEN))


def normalize_code(code: str) -> str:
    return re.sub(r"[\s\-]", "", code or "").upper()


async def unique_join_code(session: AsyncSession) -> str:
    for _ in range(20):
        code = new_join_code()
        taken = (await session.execute(
            select(SchoolClass.id).where(SchoolClass.join_code == code))).first()
        if not taken:
            return code
    raise HTTPException(503, "Could not make a class code, try again")


# ---------------------------------------------------------------- eligibility
async def exam_gate(session: AsyncSession, exam_id: str, user_id: int) -> bool | None:
    """True/False when the exam has an explicit roster or assigned classes (allowed or
    not), None when neither exists (caller falls back to the LMS course gate / open).
    One round trip of indexed EXISTS lookups."""
    has_roster = exists().where(ExamRoster.exam_id == exam_id)
    has_classes = exists().where(ExamClass.exam_id == exam_id)
    in_roster = exists().where(ExamRoster.exam_id == exam_id, ExamRoster.user_id == user_id)
    in_class = (select(ClassMember.id)
                .join(ExamClass, ExamClass.class_id == ClassMember.class_id)
                .where(ExamClass.exam_id == exam_id, ClassMember.user_id == user_id)
                .exists())
    r_has, c_has, r_in, c_in = (await session.execute(
        select(has_roster, has_classes, in_roster, in_class))).one()
    if r_has:
        return bool(r_in)
    if c_has:
        return bool(c_in)
    return None


async def require_exam_access(session: AsyncSession, exam: dict, user_id: int) -> None:
    """Student start / offline package gate. Raises 403 when the student is not in the
    exam's audience (see module docstring)."""
    allowed = await exam_gate(session, exam["id"], user_id)
    if allowed is False:
        raise HTTPException(403, NOT_IN_CLASS)
    if allowed is None and exam.get("course_link_id"):
        from ..routes_lms import course_gate  # slice 5 rule, unchanged
        await course_gate(session, exam["course_link_id"], user_id)


async def student_exam_visibility(session: AsyncSession, user_id: int, exam_ids: list[str]
                                  ) -> tuple[set[str], dict[str, list[str]]]:
    """For the student exam list: (exam ids this student may NOT see, class names per exam
    for the student's classes). A handful of indexed queries over the listed exams."""
    if not exam_ids:
        return set(), {}
    with_roster = set((await session.execute(
        select(ExamRoster.exam_id).where(ExamRoster.exam_id.in_(exam_ids)).distinct())).scalars())
    with_classes = set((await session.execute(
        select(ExamClass.exam_id).where(ExamClass.exam_id.in_(exam_ids)).distinct())).scalars())
    my_roster = set((await session.execute(
        select(ExamRoster.exam_id).where(ExamRoster.exam_id.in_(exam_ids),
                                         ExamRoster.user_id == user_id))).scalars())
    names: dict[str, list[str]] = {}
    for eid, name in (await session.execute(
            select(ExamClass.exam_id, SchoolClass.name)
            .join(SchoolClass, SchoolClass.id == ExamClass.class_id)
            .join(ClassMember, ClassMember.class_id == ExamClass.class_id)
            .where(ExamClass.exam_id.in_(exam_ids), ClassMember.user_id == user_id)
            .order_by(SchoolClass.name))).all():
        names.setdefault(eid, []).append(name)
    hidden = set()
    for eid in exam_ids:
        if eid in with_roster:
            ok = eid in my_roster
        elif eid in with_classes:
            ok = eid in names
        else:
            ok = True
        if not ok:
            hidden.add(eid)
    return hidden, names


async def class_filter(session: AsyncSession, teacher_id: int, class_id: int
                       ) -> tuple[set[int], set[str]]:
    """(member user ids, assigned exam ids) of a teacher's class, for report filters.
    404 when the class is not this teacher's."""
    c = await session.get(SchoolClass, class_id)
    if c is None or c.teacher_id != teacher_id:
        raise HTTPException(404, "Class not found")
    members = set((await session.execute(
        select(ClassMember.user_id).where(ClassMember.class_id == class_id))).scalars())
    exams = set((await session.execute(
        select(ExamClass.exam_id).where(ExamClass.class_id == class_id))).scalars())
    return members, exams


async def class_member_users(session: AsyncSession, exam_id: str) -> list[User] | None:
    """Union of members of the exam's assigned classes (None when no class is assigned)."""
    if not (await session.execute(
            select(ExamClass.id).where(ExamClass.exam_id == exam_id).limit(1))).first():
        return None
    sub = (select(ClassMember.user_id)
           .join(ExamClass, ExamClass.class_id == ClassMember.class_id)
           .where(ExamClass.exam_id == exam_id))
    return list((await session.execute(
        select(User).where(User.id.in_(sub)).order_by(User.name, User.username))).scalars())


# ---------------------------------------------------------------- finding students
def split_entries(text: str) -> list[str]:
    """One student per line (commas / semicolons / tabs also separate). Order kept,
    duplicates dropped (case-insensitive)."""
    out, seen = [], set()
    for raw in re.split(r"[\n\r,;\t]+", text or ""):
        e = raw.strip().strip("<>\"'").strip()
        if e and e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


def _has_email() -> bool:
    return hasattr(User, "email")


async def find_students(session: AsyncSession, entries: Iterable[str]
                        ) -> tuple[dict[str, User], list[str]]:
    """Match entries (username, or email when User has an `email` column) to student
    accounts, case-insensitively. Returns ({entry: user}, [entries not found])."""
    entries = list(entries)
    if not entries:
        return {}, []
    lowered = {e.lower() for e in entries}
    conds = [func.lower(User.username).in_(lowered)]
    if _has_email():
        conds.append(func.lower(getattr(User, "email")).in_(lowered))
    users = list((await session.execute(
        select(User).where(User.role == "student", or_(*conds)))).scalars())
    by_key: dict[str, User] = {}
    for u in users:
        by_key.setdefault(u.username.lower(), u)
    if _has_email():
        for u in users:
            em = (getattr(u, "email", None) or "").lower()
            if em:
                by_key.setdefault(em, u)
    found, missing = {}, []
    for e in entries:
        u = by_key.get(e.lower())
        if u is None:
            missing.append(e)
        else:
            found[e] = u
    return found, missing


_ID_HEADERS = ("username", "user", "login", "login_id", "user_id", "student_id", "id", "email",
               "e-mail", "email address", "mail")
_NAME_HEADERS = ("name", "full name", "student", "student name", "display name")


def parse_csv(text: str) -> list[tuple[list[str], str]]:
    """Rows of a roster CSV -> [(candidate identifiers, label for "not found")].
    With a header row naming username/email columns those are used; otherwise every cell
    of each row is tried (so a plain one-column list works too)."""
    text = (text or "").lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    head = [c.lower() for c in rows[0]]
    id_cols = [i for i, h in enumerate(head) if h in _ID_HEADERS]
    name_cols = [i for i, h in enumerate(head) if h in _NAME_HEADERS]
    out = []
    if id_cols:
        for r in rows[1:]:
            ids = [r[i] for i in id_cols if i < len(r) and r[i]]
            name = next((r[i] for i in name_cols if i < len(r) and r[i]), "")
            if not ids:
                if name:
                    out.append(([], name))
                continue
            label = f"{name} ({ids[0]})" if name else ids[0]
            out.append((ids, label))
    else:
        for r in rows:
            ids = [c for c in r if c]
            out.append((ids, ", ".join(ids)))
    return out[:MAX_ENTRIES]
