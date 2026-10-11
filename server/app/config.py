"""Settings read from the environment (at app creation time, so tests can override)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("proctora")

DEV_SECRET = "proctora-dev-secret-change-me"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _teacher_signup_default() -> str:
    v = (os.environ.get("FAIRPLAY_TEACHER_SIGNUP") or "").strip().lower()
    if v in ("open", "approval", "closed"):
        return v
    # Unset or invalid: open in development, approval in production.
    return "approval" if os.environ.get("FAIRPLAY_ENV", "").lower() == "production" else "open"


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get(
        "DATABASE_URL", "sqlite+aiosqlite:///./data/proctora.db"))
    redis_url: str | None = field(default_factory=lambda: os.environ.get("REDIS_URL") or None)
    secret: str = field(default_factory=lambda: os.environ.get("PROCTORA_SECRET") or DEV_SECRET)
    token_ttl_s: int = 43200
    db_pool_size: int = field(default_factory=lambda: _int("DB_POOL_SIZE", 10))
    db_max_overflow: int = field(default_factory=lambda: _int("DB_MAX_OVERFLOW", 20))
    pbkdf2_iterations: int = field(default_factory=lambda: _int("PROCTORA_PBKDF2_ITERATIONS", 200000))
    seed_loadtest_users: int = field(default_factory=lambda: _int("PROCTORA_SEED_LOADTEST_USERS", 0))
    static_dir: str = field(default_factory=lambda: os.environ.get(
        "PROCTORA_STATIC_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")))
    sql_echo: bool = field(default_factory=lambda: os.environ.get("SQL_ECHO") == "1")
    # slice 6: question images and other uploads (local disk; S3-compatible later)
    uploads_dir: str = field(default_factory=lambda: os.environ.get(
        "FAIRPLAY_UPLOADS_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "uploads")))

    # Teacher self-registration: open | approval | closed (students can always sign up).
    teacher_signup: str = field(default_factory=lambda: _teacher_signup_default())

    def warn_insecure(self) -> None:
        if self.secret == DEV_SECRET:
            log.warning("PROCTORA_SECRET is not set; using an insecure dev secret")
        if not self.redis_url:
            log.warning("REDIS_URL is not set; using in-memory store (single process, dev only)")
