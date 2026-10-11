"""Slice 6 A + phase 1a: encrypted offline exam packages (format version 2).

Threat model and design: CONTRACT.md "Phase 1a — offline package security". In short:

- `GET /api/student/exams/{id}/package` (student; exam live, or scheduled to start within
  2 h): the student exam view encrypted with a random per-exam 256-bit content key K:
  AES-256-GCM(K, iv, plaintext, aad=exam_id) -> `{"exam_id","version":2,"alg":"AES-GCM",
  "iv","ciphertext"}`. Without K it reveals nothing, however long a student works on it, so
  it is safe to hand out before the exam opens.
- `GET /api/student/exams/{id}/package-key` (student; only while the exam is LIVE):
  K wrapped with the start code: KEK = PBKDF2-SHA256(start_code + exam_id, salt, 100 000,
  32 bytes); `wrapped_key` = AES-GCM(KEK, iv, K, aad=exam_id). The check-in page fetches
  it once the exam is open and keeps it, so a student whose network then drops can still
  start: the typed code unwraps K (wrong code = GCM failure = "Wrong start code").
- K is created on the first package request, kept while the exam is open and dropped at
  close, so the next opening uses a new key. Every package / key hand-out is audited.

Both responses are cached in the store (one PBKDF2 per room, not per student).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from datetime import datetime, timedelta

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db import get_session
from .models import Exam
from .security import Principal, require_student
from .services import get_exam_cached
from .store import get_store
from .timeutil import iso, utcnow
from .uploads import UPLOAD_URL_PREFIX, read_upload

router = APIRouter(prefix="/api/student", tags=["offline"])

PACKAGE_VERSION = 2
PBKDF2_ITERATIONS = 100_000
MAX_INLINE_IMAGE_BYTES = 2 * 1024 * 1024
PACKAGE_CACHE_TTL = 300
KEY_CACHE_TTL = 300
SCHEDULE_WINDOW = timedelta(hours=2)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def derive_kek(start_code: str, exam_id: str, salt: bytes,
               iterations: int = PBKDF2_ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive((start_code.strip() + exam_id).encode("utf-8"))


def new_content_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def encrypt_content(plain: dict, key: bytes, exam_id: str) -> dict:
    iv = os.urandom(12)
    data = json.dumps(plain, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ct = AESGCM(key).encrypt(iv, data, exam_id.encode("utf-8"))
    return {"exam_id": exam_id, "version": PACKAGE_VERSION, "alg": "AES-GCM",
            "iv": _b64(iv), "ciphertext": _b64(ct)}


def decrypt_content(pkg: dict, key: bytes) -> dict:
    data = AESGCM(key).decrypt(_unb64(pkg["iv"]), _unb64(pkg["ciphertext"]),
                               pkg["exam_id"].encode("utf-8"))
    return json.loads(data.decode("utf-8"))


def wrap_key(key: bytes, start_code: str, exam_id: str,
             iterations: int = PBKDF2_ITERATIONS) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    kek = derive_kek(start_code, exam_id, salt, iterations)
    wrapped = AESGCM(kek).encrypt(iv, key, exam_id.encode("utf-8"))
    return {"exam_id": exam_id, "version": PACKAGE_VERSION, "alg": "PBKDF2-SHA256/AES-GCM",
            "iterations": iterations, "salt": _b64(salt), "iv": _b64(iv),
            "wrapped_key": _b64(wrapped)}


def unwrap_key(release: dict, start_code: str) -> bytes:
    """Wrong code -> cryptography InvalidTag."""
    kek = derive_kek(start_code, release["exam_id"], _unb64(release["salt"]),
                     int(release.get("iterations") or PBKDF2_ITERATIONS))
    return AESGCM(kek).decrypt(_unb64(release["iv"]), _unb64(release["wrapped_key"]),
                               release["exam_id"].encode("utf-8"))


def encrypt_package(plain: dict, start_code: str, exam_id: str,
                    iterations: int = PBKDF2_ITERATIONS) -> tuple[dict, dict]:
    """(package, key release) for a fresh key (tests and tools)."""
    key = new_content_key()
    return encrypt_content(plain, key, exam_id), wrap_key(key, start_code, exam_id, iterations)


def decrypt_package(pkg: dict, release: dict, start_code: str) -> dict:
    """What the browser does offline: unwrap K with the typed code, then open the package."""
    if release["exam_id"] != pkg["exam_id"]:
        raise ValueError("package and key are for different exams")
    return decrypt_content(pkg, unwrap_key(release, start_code))


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


def _scheduled_soon(exam: dict) -> bool:
    if not exam.get("scheduled_at") or exam["status"] == "closed":
        return False
    try:
        at = datetime.fromisoformat(exam["scheduled_at"].replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    return at - SCHEDULE_WINDOW <= utcnow()


async def _offline_exam(session: AsyncSession, exam_id: str, p: Principal) -> dict:
    """The cached student exam view, after the checks every package request shares."""
    exam = await get_exam_cached(get_store(), session, exam_id)
    if exam is None or exam.get("delivery") == "paper":
        raise HTTPException(404, "Exam not found")
    if (exam.get("mode") or "questions") == "external":
        raise HTTPException(409, "This exam runs on another website and needs the internet")
    if exam.get("course_link_id"):
        from .routes_lms import course_gate
        await course_gate(session, exam["course_link_id"], p.user_id)
    return exam


def key_cache_key(exam_id: str) -> str:
    return f"pkgk:{exam_id}"


async def content_key(session: AsyncSession, exam_id: str) -> bytes:
    """The exam's package key K, created on first use (race-safe across workers)."""
    store = get_store()
    raw = await store.get(key_cache_key(exam_id))
    if raw:
        return _unb64(raw)
    cur = (await session.execute(select(Exam.package_key).where(Exam.id == exam_id))).scalar_one_or_none()
    if not cur:
        await session.execute(update(Exam).where(Exam.id == exam_id, Exam.package_key.is_(None))
                              .values(package_key=_b64(new_content_key())))
        await session.commit()
        cur = (await session.execute(select(Exam.package_key).where(Exam.id == exam_id))).scalar_one()
    await store.set(key_cache_key(exam_id), cur, KEY_CACHE_TTL)
    return _unb64(cur)


def _fingerprint(*parts: bytes) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part)
        h.update(b"\0")
    return h.hexdigest()[:24]


@router.get("/exams/{exam_id}/package")
async def package(exam_id: str, request: Request, p: Principal = Depends(require_student),
                  session: AsyncSession = Depends(get_session)):
    exam = await _offline_exam(session, exam_id, p)
    if not (exam["status"] == "live" or _scheduled_soon(exam)):
        raise HTTPException(409, "Exam is not open")
    store = get_store()
    key = await content_key(session, exam_id)
    plain = {k: exam.get(k) for k in ("id", "title", "course", "duration_min", "mode",
                                       "shuffle_questions", "questions")}
    fp = _fingerprint(key, json.dumps(plain, sort_keys=True).encode())
    if await audit.once(store, f"aud:pkd:{exam_id}:{p.user_id}:{fp}"):
        await audit.record("package_downloaded", request=request, actor_id=p.user_id,
                           actor_role="student", exam_id=exam_id)
    ck = f"pkg2:{exam_id}:{fp}"
    raw = await store.get(ck)
    if raw:
        return json.loads(raw)
    pkg = await asyncio.to_thread(encrypt_content, package_plaintext(exam), key, exam_id)
    await store.set(ck, json.dumps(pkg), PACKAGE_CACHE_TTL)
    return pkg


@router.get("/exams/{exam_id}/package-key")
async def package_key(exam_id: str, request: Request, p: Principal = Depends(require_student),
                      session: AsyncSession = Depends(get_session)):
    exam = await _offline_exam(session, exam_id, p)
    code = exam.get("start_code")
    if exam["status"] != "live" or not code:
        raise HTTPException(409, "Exam is not open")
    store = get_store()
    key = await content_key(session, exam_id)
    fp = _fingerprint(key, code.encode())
    if await audit.once(store, f"aud:pkr:{exam_id}:{p.user_id}:{fp}"):
        await audit.record("package_key_released", request=request, actor_id=p.user_id,
                           actor_role="student", exam_id=exam_id)
    ck = f"pkgkr:{exam_id}:{fp}"
    raw = await store.get(ck)
    if raw:
        return json.loads(raw)
    rel = await asyncio.to_thread(wrap_key, key, code, exam_id)
    await store.set(ck, json.dumps(rel), PACKAGE_CACHE_TTL)
    return rel
