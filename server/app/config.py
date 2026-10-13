"""Settings read from the environment (at app creation time, so tests can override)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("proctora")

DEV_SECRET = "proctora-dev-secret-change-me"   # never accepted any more (audit 2026-10-12 #12)
# Shared with dev builds of the Mac app (mac/build.sh without FAIRPLAY_APP_KEY). Only
# accepted when FAIRPLAY_DEV=1; production must set FAIRPLAY_APP_KEY.
DEV_APP_KEY = "fairplay-dev-app-key-not-secret"


# Parts of FairPlay that are switched off unless FAIRPLAY_FEATURES lists them.
OPTIONAL_FEATURES = ("scan", "lms", "practice")


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get(
        "DATABASE_URL", "sqlite+aiosqlite:///./data/proctora.db"))
    redis_url: str | None = field(default_factory=lambda: os.environ.get("REDIS_URL") or None)
    # Token/LMS sealing secret. Empty -> resolved at startup by check_secure(): a random
    # secret persisted under data/ in dev (FAIRPLAY_DEV=1), otherwise the server refuses to start.
    secret: str = field(default_factory=lambda: os.environ.get("PROCTORA_SECRET")
                        or os.environ.get("FAIRPLAY_SECRET") or "")
    # FAIRPLAY_DEV=1: local laptop demo (Run Server.command). Relaxes the startup checks only.
    dev: bool = field(default_factory=lambda: _flag("FAIRPLAY_DEV", False))
    # Audit #1-#3: exams can only be started from the FairPlay app (signed proof) and every
    # attempt is bound to one app session. Off only for local tests / browser demos.
    require_app: bool = field(default_factory=lambda: _flag("FAIRPLAY_REQUIRE_APP", True))
    # HMAC keys compiled into the Mac app (comma-separated; several = key rotation).
    app_keys: list[str] = field(default_factory=lambda: _list("FAIRPLAY_APP_KEY"))
    # Extra browser origins allowed to call the API (the UI itself is same-origin).
    cors_origins: list[str] = field(default_factory=lambda: _list("FAIRPLAY_CORS_ORIGINS"))
    token_ttl_s: int = 43200                 # teachers: 12 h
    # Audit S-15: student tokens travel through the exam page and the app; shorter life.
    student_token_ttl_s: int = field(default_factory=lambda: _int("FAIRPLAY_STUDENT_TOKEN_TTL_S", 8 * 3600))
    db_pool_size: int = field(default_factory=lambda: _int("DB_POOL_SIZE", 10))
    db_max_overflow: int = field(default_factory=lambda: _int("DB_MAX_OVERFLOW", 20))
    pbkdf2_iterations: int = field(default_factory=lambda: _int("PROCTORA_PBKDF2_ITERATIONS", 200000))
    seed_loadtest_users: int = field(default_factory=lambda: _int("PROCTORA_SEED_LOADTEST_USERS", 0))
    # Audit 2026-10-12 S-01: the demo logins (teacher/teacher123, alice/alice123 ...) exist
    # only on a dev server (FAIRPLAY_DEV=1) or when asked for explicitly (tests, a demo box).
    seed_demo: bool = field(default_factory=lambda: _flag("FAIRPLAY_SEED_DEMO", False))
    # Audit S-08: without Redis every worker has its own memory store (exam-cache
    # invalidation, nonces, lockouts, beat replay guard all break with >1 process). A real
    # server needs REDIS_URL, or must say it runs one process (FAIRPLAY_SINGLE_PROCESS=1).
    single_process: bool = field(default_factory=lambda: _flag("FAIRPLAY_SINGLE_PROCESS", False))
    workers: int = field(default_factory=lambda: _int("WEB_CONCURRENCY", 1))
    # Audit S-17: Postgres max_connections and how many API containers share it (0 = unchecked).
    db_max_connections: int = field(default_factory=lambda: _int("FAIRPLAY_DB_MAX_CONNECTIONS", 0))
    replicas: int = field(default_factory=lambda: _int("FAIRPLAY_REPLICAS", 1))
    # Sign-in tries per IP per minute (audit S-05). Raise it for load tests from one machine.
    login_ip_limit: int = field(default_factory=lambda: _int("FAIRPLAY_LOGIN_IP_LIMIT", 300))
    static_dir: str = field(default_factory=lambda: os.environ.get(
        "PROCTORA_STATIC_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")))
    sql_echo: bool = field(default_factory=lambda: os.environ.get("SQL_ECHO") == "1")
    # 2026-10-13: optional parts, off unless listed (FAIRPLAY_FEATURES=scan,lms,practice).
    # Off = their API routes are not mounted and the teacher UI hides them.
    features: list[str] = field(default_factory=lambda: [
        f for f in _list("FAIRPLAY_FEATURES") if f in OPTIONAL_FEATURES])
    # slice 6: question images and other uploads (local disk; S3-compatible later)
    uploads_dir: str = field(default_factory=lambda: os.environ.get(
        "FAIRPLAY_UPLOADS_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "uploads")))

    def feature(self, name: str) -> bool:
        return name in self.features

    @property
    def demo_logins(self) -> bool:
        """Demo accounts are created and the login page may show their passwords."""
        return self.dev or self.seed_demo

    def check_secure(self) -> None:
        """Startup checks. Raises RuntimeError instead of running with public defaults."""
        if self.secret in ("", DEV_SECRET) or len(self.secret) < 16:
            if not self.dev:
                raise RuntimeError(
                    "PROCTORA_SECRET is missing, too short (<16 chars) or the old public default. "
                    "Set it to a long random string (python3 -c 'import secrets;print(secrets.token_urlsafe(48))'), "
                    "or set FAIRPLAY_DEV=1 for a local demo.")
            self.secret = _dev_secret(os.path.dirname(self.uploads_dir))
            log.warning("FAIRPLAY_DEV=1: using a random local secret stored in data/secret.key")
        if not self.app_keys:
            if self.dev:
                self.app_keys = [DEV_APP_KEY]
                log.warning("FAIRPLAY_DEV=1: accepting the public dev app key; set FAIRPLAY_APP_KEY for real exams")
            elif self.require_app:
                raise RuntimeError(
                    "FAIRPLAY_APP_KEY is not set. Use the same value you pass to mac/build.sh "
                    "(or FAIRPLAY_REQUIRE_APP=0 to allow exams in a normal browser).")
        elif DEV_APP_KEY in self.app_keys and not self.dev:
            raise RuntimeError("FAIRPLAY_APP_KEY contains the public dev key")
        if not self.require_app:
            log.warning("FAIRPLAY_REQUIRE_APP=0: exams can be taken in a normal browser")
        need = max(1, self.replicas) * max(1, self.workers) * (self.db_pool_size + self.db_max_overflow)
        if self.db_max_connections and need > self.db_max_connections:
            raise RuntimeError(
                f"Database pools can open {need} connections ({self.replicas} containers x "
                f"{self.workers} workers x {self.db_pool_size}+{self.db_max_overflow}) but Postgres "
                f"allows {self.db_max_connections}. Lower DB_POOL_SIZE / DB_MAX_OVERFLOW.")
        if not self.redis_url and not self.dev and (self.workers > 1 or not self.single_process):
            raise RuntimeError(
                "REDIS_URL is not set. With more than one worker the in-memory store breaks "
                "exam-cache invalidation, single-use challenges, code lockouts and the beat "
                "replay guard. Set REDIS_URL, or run ONE process with FAIRPLAY_SINGLE_PROCESS=1 "
                "(and WEB_CONCURRENCY=1).")
        self.warn_insecure()

    def warn_insecure(self) -> None:
        if not self.redis_url:
            log.warning("REDIS_URL is not set; using in-memory store (single process, dev only)")


def _dev_secret(data_dir: str) -> str:
    """Random secret kept in <data>/secret.key so dev tokens survive restarts."""
    import secrets as _secrets
    path = os.path.join(data_dir or ".", "secret.key")
    try:
        with open(path) as f:
            v = f.read().strip()
            if len(v) >= 32:
                return v
    except OSError:
        pass
    v = _secrets.token_urlsafe(48)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(v)
    except OSError:
        log.warning("could not store %s; dev tokens end when the server stops", path)
    return v
