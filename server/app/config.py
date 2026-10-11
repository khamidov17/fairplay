"""Settings read from the environment (at app creation time, so tests can override)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("proctora")

DEV_SECRET = "proctora-dev-secret-change-me"
MIN_SECRET_LEN = 32


class ConfigError(RuntimeError):
    """Unsafe production configuration: the server refuses to start."""


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_mode() -> str:
    v = (os.environ.get("FAIRPLAY_ENV") or "development").strip().lower()
    return "production" if v in ("production", "prod") else "development"


def _csv(name: str) -> list[str] | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return [x.strip() for x in raw.split(",") if x.strip()]
def _teacher_signup_default() -> str:
    v = (os.environ.get("FAIRPLAY_TEACHER_SIGNUP") or "").strip().lower()
    if v in ("open", "approval", "closed"):
        return v
    # Unset or invalid: open in development, approval in production.
    return "approval" if os.environ.get("FAIRPLAY_ENV", "").lower() == "production" else "open"


@dataclass
class Settings:
    # "development" (default: demo users, permissive CORS, dev secret allowed) or "production"
    env: str = field(default_factory=_env_mode)
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

    # CORS: None = default for the mode ("*" in development, same-origin only in production).
    cors_origins: list[str] | None = field(default_factory=lambda: _csv("FAIRPLAY_CORS_ORIGINS"))
    # Demo users (teacher/teacher123, alice/alice123 ...): development only, never in production.
    seed_demo_users: bool | None = None
    # Production also needs this second, explicit switch before load-test users are created.
    allow_loadtest_users_in_production: bool = field(
        default_factory=lambda: os.environ.get("FAIRPLAY_ALLOW_LOADTEST_USERS") == "1")

    @property
    def production(self) -> bool:
        return self.env == "production"

    def allowed_origins(self) -> list[str]:
        if self.cors_origins is not None:
            return self.cors_origins
        return [] if self.production else ["*"]

    def should_seed_demo_users(self) -> bool:
        if self.production:
            return False
        return True if self.seed_demo_users is None else self.seed_demo_users

    def loadtest_users_to_seed(self) -> int:
        if self.production and not self.allow_loadtest_users_in_production:
            return 0
        return max(0, self.seed_loadtest_users)

    def validate(self) -> None:
        """Production refuses to start with unsafe settings (raises ConfigError)."""
        if not self.production:
            return
        problems = []
        if not self.secret or self.secret == DEV_SECRET:
            problems.append("PROCTORA_SECRET is not set (or is the dev value)")
        elif len(self.secret) < MIN_SECRET_LEN:
            problems.append(f"PROCTORA_SECRET is shorter than {MIN_SECRET_LEN} characters "
                            "(use: openssl rand -hex 32)")
        if "*" in (self.cors_origins or []):
            problems.append("FAIRPLAY_CORS_ORIGINS must list origins, not '*', in production")
        if problems:
            raise ConfigError("Refusing to start in production: " + "; ".join(problems))
    # Teacher self-registration: open | approval | closed (students can always sign up).
    teacher_signup: str = field(default_factory=lambda: _teacher_signup_default())

    def warn_insecure(self) -> None:
        self.validate()
        if self.secret == DEV_SECRET:
            log.warning("PROCTORA_SECRET is not set; using an insecure dev secret")
        if not self.redis_url:
            if self.production:
                log.warning("!!! REDIS_URL is not set in production: rate limits, live room and "
                            "token revocation live in this process only. Run ONE worker, or set "
                            "REDIS_URL. !!!")
            else:
                log.warning("REDIS_URL is not set; using in-memory store (single process, dev only)")
        if self.production and self.seed_loadtest_users:
            if self.allow_loadtest_users_in_production:
                log.warning("Creating %d load-test users in production (password 'loadtest')",
                            self.seed_loadtest_users)
            else:
                log.warning("PROCTORA_SEED_LOADTEST_USERS ignored in production "
                            "(also set FAIRPLAY_ALLOW_LOADTEST_USERS=1 to really create them)")
