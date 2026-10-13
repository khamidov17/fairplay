"""Slice 6 A: encrypted offline exam packages.

`GET /api/student/exams/{id}/package` gives the student exam view encrypted with a key
only the room knows: PBKDF2-SHA256(start_code + exam_id, salt, 600 000 iterations (v3), 32 bytes)
-> AES-256-GCM (12-byte IV, 16-byte tag appended to the ciphertext, the WebCrypto layout).
The browser decrypts it with SubtleCrypto when the network is down at start; a wrong code
fails the GCM tag check ("Wrong start code").

Packages are served only while the exam has a start code (it is live). One package per
exam + code + duration is built (in a worker thread: PBKDF2 is CPU work) and cached in the
store, so a room full of students costs one key derivation, not one each.

Audit 2026-10-12 S-03 (package version 2):
- only students of the exam's class / LMS course get the package (and the key below);
- the key material is start_code + exam_id + PACKAGE KEY, where the package key is 32
  random bytes per exam run (Exam.package_secret), handed out by GET .../package-key only
  while the exam is live. A package fetched early can't be opened by trying all 10^6 start
  codes. Cost: a student must be online once after the exam opens (the exam list fetches
  the key) to start offline later.

Audit 2026-10-13 (package version 3): 600 000 PBKDF2 iterations (OWASP 2023 for
PBKDF2-SHA256), written into the package as "iterations" so the page derives with the
same count and older packages (no field: 100 000) still open. Six times the work per
guessed start code for whoever holds the package and its key before the code is read out.
The package key is made with one conditional UPDATE (two first requests can't hand out
different keys), and a cache miss is built once: the others wait for it (single flight).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
from datetime import datetime, timedelta

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .access import require_member
from .db import get_session
from .models import Exam
from .security import Principal, require_student
from .services import get_exam_cached
from .store import get_store
from .timeutil import iso, utcnow
from .uploads import UPLOAD_URL_PREFIX, read_upload

router = APIRouter(prefix="/api/student", tags=["offline"])

PACKAGE_VERSION = 3
PBKDF2_ITERATIONS = 600_000
LEGACY_ITERATIONS = 100_000      # packages without an "iterations" field (v1 / v2)
BUILD_LOCK_S = 30
BUILD_WAIT_S = 20
MAX_INLINE_IMAGE_BYTES = 2 * 1024 * 1024
PACKAGE_CACHE_TTL = 300
SCHEDULE_WINDOW = timedelta(hours=2)


def package_id(secret: str, exam_id: str, start_code: str | None, duration_min: int) -> str | None:
    """Audit W-02: changes when the start code (or duration) changes, without revealing it.
    The exam list carries it, so a stored package that no longer opens is fetched again."""
    if not start_code:
        return None
    import hashlib
    import hmac as _hmac
    return _hmac.new(secret.encode(), f"pkg|{exam_id}|{start_code}|{duration_min}".encode(),
                     hashlib.sha256).hexdigest()[:16]


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def derive_key(start_code: str, exam_id: str, salt: bytes,
               iterations: int = PBKDF2_ITERATIONS, package_key: str = "") -> bytes:
    if not 1_000 <= iterations <= 10_000_000:
        raise ValueError("iterations out of range")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive((start_code + exam_id + package_key).encode("utf-8"))


def encrypt_package(plain: dict, start_code: str, exam_id: str,
                    iterations: int = PBKDF2_ITERATIONS, package_key: str = "") -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    key = derive_key(start_code, exam_id, salt, iterations, package_key)
    data = json.dumps(plain, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ct = AESGCM(key).encrypt(iv, data, None)
    return {"exam_id": exam_id, "version": PACKAGE_VERSION if package_key else 1, "alg": "AES-GCM",
            "iterations": iterations,
            "salt": _b64(salt), "iv": _b64(iv), "ciphertext": _b64(ct)}


def decrypt_package(pkg: dict, start_code: str, iterations: int | None = None,
                    package_key: str = "") -> dict:
    """Inverse of encrypt_package (used by tests). Wrong code/key -> cryptography InvalidTag.
    The iteration count comes from the package unless given."""
    if iterations is None:
        iterations = int(pkg.get("iterations") or LEGACY_ITERATIONS)
    salt = base64.b64decode(pkg["salt"])
    iv = base64.b64decode(pkg["iv"])
    key = derive_key(start_code, pkg["exam_id"], salt, iterations, package_key)
    data = AESGCM(key).decrypt(iv, base64.b64decode(pkg["ciphertext"]), None)
    return json.loads(data.decode("utf-8"))


def inline_images(questions: list[dict], limit: int = MAX_INLINE_IMAGE_BYTES) -> list[dict]:
    """Copy of the questions with uploaded images as data: URLs when they fit in `limit`
    bytes all together; otherwise (or for https images) the URLs stay as they are."""
    blobs: dict[str, tuple[bytes, str]] = {}
    total = 0
    for q in questions:
        for img in q.get("images") or []:
            url = img.get("url", "")
            if url.startswith(UPLOAD_URL_PREFIX) and url not in blobs:
                got = read_upload(url[len(UPLOAD_URL_PREFIX):])
                if got is None:
                    continue
                blobs[url] = got
                total += len(got[0])
    use = blobs if total <= limit else {}
    out = []
    for q in questions:
        q = dict(q)
        imgs = []
        for img in q.get("images") or []:
            img = dict(img)
            if img.get("url") in use:
                data, media = use[img["url"]]
                img["url"] = f"data:{media};base64,{_b64(data)}"
            imgs.append(img)
        q["images"] = imgs
        out.append(q)
    return out


def package_plaintext(exam: dict) -> dict:
    """The student exam view inside the package (questions without answer keys)."""
    return {
        "exam_id": exam["id"], "title": exam["title"], "course": exam.get("course", ""),
        "duration_min": exam["duration_min"], "mode": exam.get("mode") or "questions",
        "shuffle_questions": bool(exam.get("shuffle_questions")),
        "questions": inline_images(exam.get("questions") or []),
        "generated_at": iso(utcnow()),
    }


def _build(exam: dict, package_key: str) -> dict:
    return encrypt_package(package_plaintext(exam), exam["start_code"], exam["id"],
                           package_key=package_key)


async def _package_secret(session: AsyncSession, exam_id: str) -> str:
    """This exam run's package key, made on first use (cleared when the exam closes).
    Audit 2026-10-13: set only where still empty, then read back, so two first requests
    in parallel agree on one key (before, the later commit replaced the earlier key and
    the students who got the first one could not open the package offline)."""
    e = await session.get(Exam, exam_id)
    if e is None:
        raise HTTPException(404, "Exam not found")
    if e.package_secret:
        return e.package_secret
    await session.execute(update(Exam).where(Exam.id == exam_id, Exam.package_secret.is_(None))
                          .values(package_secret=secrets.token_hex(32)))
    await session.commit()
    got = (await session.execute(select(Exam.package_secret).where(Exam.id == exam_id))).scalar()
    if not got:
        raise HTTPException(409, "Exam is not open")
    return got


@router.get("/exams/{exam_id}/package")
async def package(exam_id: str, request: Request, p: Principal = Depends(require_student),
                  session: AsyncSession = Depends(get_session)):
    store = get_store()
    exam = await get_exam_cached(store, session, exam_id)
    if exam is None or exam.get("delivery") == "paper":
        raise HTTPException(404, "Exam not found")
    if (exam.get("mode") or "questions") == "external":
        raise HTTPException(409, "This exam runs on another website and needs the internet")
    code = exam.get("start_code")
    scheduled_soon = False
    if exam.get("scheduled_at"):
        try:
            at = datetime.fromisoformat(exam["scheduled_at"].replace("Z", "+00:00")).replace(tzinfo=None)
            scheduled_soon = at - SCHEDULE_WINDOW <= utcnow()
        except ValueError:
            scheduled_soon = False
    if not code or not (exam["status"] == "live" or scheduled_soon):
        raise HTTPException(409, "Exam is not open")
    await require_member(session, exam, p.user_id)        # audit S-03
    key = f"pkg:{exam_id}:{code}:{exam['duration_min']}"
    raw = await store.get(key)
    if raw:
        return json.loads(raw)
    # Audit 2026-10-13: single flight. A room opening the exam at once would otherwise
    # start one 600k-iteration build per student; one builds, the rest wait for the cache.
    lock = f"pkgbuild:{key}"
    if not await store.setnx(lock, "1", BUILD_LOCK_S):
        loop = asyncio.get_running_loop()
        until = loop.time() + BUILD_WAIT_S
        while loop.time() < until:
            await asyncio.sleep(0.2)
            raw = await store.get(key)
            if raw:
                return json.loads(raw)
            if not await store.get(lock):
                break      # the builder failed: build here
    try:
        secret = await _package_secret(session, exam_id)
        pkg = await asyncio.to_thread(_build, exam, secret)
        pkg["package_id"] = package_id(request.app.state.settings.secret, exam_id, code,
                                       exam["duration_min"])
        await store.set(key, json.dumps(pkg), PACKAGE_CACHE_TTL)
    finally:
        await store.delete(lock)
    return pkg


@router.get("/exams/{exam_id}/package-key")
async def package_key(exam_id: str, p: Principal = Depends(require_student),
                      session: AsyncSession = Depends(get_session)):
    """Audit S-03: the second half of the package key, only while the exam is live and
    only for its class. The page keeps it next to the package (cleared after the exam)."""
    exam = await get_exam_cached(get_store(), session, exam_id)
    if exam is None or exam.get("delivery") == "paper":
        raise HTTPException(404, "Exam not found")
    if exam["status"] != "live":
        raise HTTPException(409, "Exam is not open")
    await require_member(session, exam, p.user_id)
    return {"exam_id": exam_id, "key": await _package_secret(session, exam_id)}
