"""Slice 6 A: encrypted offline exam packages.

`GET /api/student/exams/{id}/package` gives the student exam view encrypted with a key
only the room knows: PBKDF2-SHA256(start_code + exam_id, salt, 100 000 iterations, 32 bytes)
-> AES-256-GCM (12-byte IV, 16-byte tag appended to the ciphertext, the WebCrypto layout).
The browser decrypts it with SubtleCrypto when the network is down at start; a wrong code
fails the GCM tag check ("Wrong start code").

Packages are served only while the exam has a start code (it is live). One package per
exam + code + duration is built (in a worker thread: PBKDF2 is CPU work) and cached in the
store, so a room full of students costs one key derivation, not one each.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import datetime, timedelta

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .security import Principal, require_student
from .services import get_exam_cached
from .store import get_store
from .timeutil import iso, utcnow
from .uploads import UPLOAD_URL_PREFIX, read_upload

router = APIRouter(prefix="/api/student", tags=["offline"])

PACKAGE_VERSION = 1
PBKDF2_ITERATIONS = 100_000
MAX_INLINE_IMAGE_BYTES = 2 * 1024 * 1024
PACKAGE_CACHE_TTL = 300
SCHEDULE_WINDOW = timedelta(hours=2)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def derive_key(start_code: str, exam_id: str, salt: bytes,
               iterations: int = PBKDF2_ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive((start_code + exam_id).encode("utf-8"))


def encrypt_package(plain: dict, start_code: str, exam_id: str,
                    iterations: int = PBKDF2_ITERATIONS) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    key = derive_key(start_code, exam_id, salt, iterations)
    data = json.dumps(plain, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ct = AESGCM(key).encrypt(iv, data, None)
    return {"exam_id": exam_id, "version": PACKAGE_VERSION, "alg": "AES-GCM",
            "salt": _b64(salt), "iv": _b64(iv), "ciphertext": _b64(ct)}


def decrypt_package(pkg: dict, start_code: str, iterations: int = PBKDF2_ITERATIONS) -> dict:
    """Inverse of encrypt_package (used by tests). Wrong code -> cryptography InvalidTag."""
    salt = base64.b64decode(pkg["salt"])
    iv = base64.b64decode(pkg["iv"])
    key = derive_key(start_code, pkg["exam_id"], salt, iterations)
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


def _build(exam: dict) -> dict:
    return encrypt_package(package_plaintext(exam), exam["start_code"], exam["id"])


@router.get("/exams/{exam_id}/package")
async def package(exam_id: str, p: Principal = Depends(require_student),
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
    # classes: same audience rule as start (roster > classes > LMS course > open)
    from .classes.service import require_exam_access
    await require_exam_access(session, exam, p.user_id)
    key = f"pkg:{exam_id}:{code}:{exam['duration_min']}"
    raw = await store.get(key)
    if raw:
        return json.loads(raw)
    pkg = await asyncio.to_thread(_build, exam)
    await store.set(key, json.dumps(pkg), PACKAGE_CACHE_TTL)
    return pkg
