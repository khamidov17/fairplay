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
    ("admin", "admin123", "Platform Admin", "superadmin"),  # dev only: /admin dashboard
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


async def seed(db: Database, settings: Settings) -> None:
    await upgrade_external_exams(db)
    wanted = [(u, pw, n, r) for u, pw, n, r in SEED_USERS]
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
