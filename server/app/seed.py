from __future__ import annotations

import logging

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .db import Database
from .models import User
from .security import hash_password

log = logging.getLogger("proctora")

SEED_USERS = [
    ("teacher", "teacher123", "Demo Teacher", "teacher"),
    ("alice", "alice123", "Alice Chen", "student"),
    ("bob", "bob123", "Bob Karimov", "student"),
    ("carol", "carol123", "Carol Wang", "student"),
]


async def upgrade_external_exams(db: Database) -> None:
    """Slice 4: exams on another website now carry one hidden grading item so teachers
    can enter scores. Give it to external exams created before (100 points)."""
    from .grading import external_question
    from .models import Exam
    async with db.sessionmaker() as session:
        rows = (await session.execute(
            select(Exam).where(Exam.mode == "external", Exam.question_count == 0))).scalars().all()
        for e in rows:
            e.questions = [external_question(100)]
            e.question_count = 1
            e.total_points = 100.0
        if rows:
            await session.commit()
            log.info("gave %d external exams a grading item", len(rows))


async def seed_demo_class(db: Database) -> None:
    """Audit S-03 on a dev/demo server: the demo teacher has a "Demo class" with the demo
    students, and their older exams without a class move into it (so the local demo keeps
    working after classes became required)."""
    from sqlalchemy import update
    from .models import ClassMember, Exam, SchoolClass
    from .routes_classes import new_join_code
    async with db.sessionmaker() as session:
        ids = dict((await session.execute(
            select(User.username, User.id).where(User.username.in_([u for u, *_ in SEED_USERS])))).all())
        tid = ids.get("teacher")
        if tid is None:
            return
        k = (await session.execute(select(SchoolClass).where(
            SchoolClass.teacher_id == tid, SchoolClass.name == "Demo class"))).scalar_one_or_none()
        if k is None:
            k = SchoolClass(teacher_id=tid, name="Demo class", join_code=new_join_code())
            session.add(k)
            await session.flush()
            for u, _, _, role in SEED_USERS:
                if role == "student" and u in ids:
                    session.add(ClassMember(class_id=k.id, user_id=ids[u]))
        await session.execute(update(Exam).where(
            Exam.teacher_id == tid, Exam.class_id.is_(None), Exam.course_link_id.is_(None))
            .values(class_id=k.id))
        try:
            await session.commit()
        except IntegrityError:            # another worker did it
            await session.rollback()


async def refuse_public_demo_logins(db: Database, settings: Settings) -> None:
    """Audit S-01: a real server must not keep a demo account with its published password
    (a student could sign in as "teacher" and read codes and answer keys)."""
    if settings.demo_logins:
        return
    from .security import verify_password_async
    names = [u for u, *_ in SEED_USERS]
    async with db.sessionmaker() as session:
        rows = (await session.execute(
            select(User.username, User.password_hash).where(User.username.in_(names)))).all()
    defaults = {u: pw for u, pw, *_ in SEED_USERS}
    for username, ph in rows:
        if await verify_password_async(defaults[username], ph):
            raise RuntimeError(
                f"The demo account '{username}' still has its public password "
                f"'{defaults[username]}'. Delete it or change its password before running "
                "without FAIRPLAY_DEV=1 (or set FAIRPLAY_SEED_DEMO=1 for a demo-only server).")


async def seed(db: Database, settings: Settings) -> None:
    await upgrade_external_exams(db)
    wanted = [(u, pw, n, r) for u, pw, n, r in SEED_USERS] if settings.demo_logins else []
    lt_hash = None
    if settings.seed_loadtest_users > 0:
        # Same password for every load-test user: hash once to keep startup fast.
        lt_hash = hash_password("loadtest", settings.pbkdf2_iterations)
        for i in range(1, settings.seed_loadtest_users + 1):
            wanted.append((f"lt{i:04d}", None, f"Load Test {i:04d}", "student"))
    async with db.sessionmaker() as session:
        existing = set((await session.execute(select(User.username))).scalars())
        rows = []
        for username, pw, name, role in wanted:
            if username in existing:
                continue
            ph = lt_hash if pw is None else hash_password(pw, settings.pbkdf2_iterations)
            rows.append({"username": username, "name": name, "role": role, "password_hash": ph})
        if not rows:
            return
        try:
            for i in range(0, len(rows), 1000):
                await session.execute(insert(User), rows[i:i + 1000])
            await session.commit()
            log.info("seeded %d users", len(rows))
        except IntegrityError:  # another worker seeded concurrently
            await session.rollback()
