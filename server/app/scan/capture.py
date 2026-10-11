"""Phone capture: the teacher shows a QR code / short link on the upload step; the phone opens
/teacher/capture/<token> (no login), photographs pages one after another and sends them.
The photos become an ordinary scan batch of that exam (origin "phone").

Tokens: 144 random bits, only their sha256 is stored, valid 30 minutes, scoped to one exam,
can be revoked. Several sends are allowed while the link is valid.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import secrets
from datetime import datetime, timedelta
from typing import Callable

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Exam
from ..security import Principal, require_teacher
from ..services import invalidate_exam, new_id
from ..store import get_store
from ..timeutil import iso, utcnow
from .answer_key import _own_exam
from .config_models import CaptureToken
from .models import PrintLayout, ScanBatch
from .photo import heic_supported, is_heic
from .storage import sources_dir

router = APIRouter(prefix="/api", tags=["scan-ai"])

TOKEN_TTL = timedelta(minutes=30)
MAX_PHOTOS = 60
MAX_BYTES = 150 * 1024 * 1024

_clock: Callable[[], datetime] | None = None


def real_now() -> datetime:
    return utcnow()


def set_clock(fn: Callable[[], datetime] | None) -> None:
    """Tests move time forward."""
    global _clock
    _clock = fn


def now() -> datetime:
    return _clock() if _clock else utcnow()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def qr_svg(text: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):] if "<svg" in svg else svg


class CaptureIn(BaseModel):
    unprinted: bool = False  # answer sheets not printed by FairPlay (mapped by hand later)


def _public_base(request: Request) -> str:
    base = os.environ.get("FAIRPLAY_PUBLIC_URL") or str(request.base_url)
    return base.rstrip("/")


@router.post("/exams/{exam_id}/capture")
async def new_capture(exam_id: str, request: Request, body: CaptureIn | None = None,
                      p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    body = body or CaptureIn()
    if e.mode == "external":
        raise HTTPException(409, "This exam is taken on another website")
    if not body.unprinted and await session.get(PrintLayout, e.id) is None:
        raise HTTPException(409, "Print the booklets from FairPlay first — or choose “not printed by FairPlay”")
    token = secrets.token_urlsafe(18)
    t = now()
    session.add(CaptureToken(token_hash=_hash(token), exam_id=e.id, teacher_id=p.user_id,
                             unprinted=body.unprinted, created_at=t, expires_at=t + TOKEN_TTL))
    await session.commit()
    path = f"/teacher/capture/{token}"
    url = _public_base(request) + path
    return {"token": token, "path": path, "url": url, "expires_at": iso(t + TOKEN_TTL),
            "unprinted": body.unprinted, "qr_svg": await asyncio.to_thread(qr_svg, url)}


@router.post("/exams/{exam_id}/capture/revoke")
async def revoke_captures(exam_id: str, p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    res = await session.execute(update(CaptureToken).where(
        CaptureToken.exam_id == e.id, CaptureToken.revoked.is_(False), CaptureToken.expires_at > now())
        .values(revoked=True))
    await session.commit()
    return {"revoked": int(res.rowcount or 0)}


async def _valid(session: AsyncSession, token: str) -> tuple[CaptureToken, Exam]:
    if not token or len(token) > 64:
        raise HTTPException(404, "This link is not valid")
    row = await session.get(CaptureToken, _hash(token))
    if row is None:
        raise HTTPException(404, "This link is not valid")
    if row.revoked or row.expires_at <= now():
        raise HTTPException(410, "This link has expired — open a new one from the upload page")
    e = await session.get(Exam, row.exam_id)
    if e is None:
        raise HTTPException(404, "This link is not valid")
    return row, e


@router.get("/capture/{token}")
async def capture_info(token: str, session: AsyncSession = Depends(get_session)):
    row, e = await _valid(session, token)
    return {"exam_title": e.title, "course": e.course or "", "expires_at": iso(row.expires_at),
            "photos": row.photos, "uploads": row.uploads, "unprinted": bool(row.unprinted)}


def _kind(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if is_heic(head):
        return "heic"
    return None


@router.post("/capture/{token}/photos", status_code=202)
async def capture_photos(token: str, files: list[UploadFile] = File(...),
                         session: AsyncSession = Depends(get_session)):
    from ..routes_scan import _spawn, run_batch  # late import: routes_scan imports app.scan
    row, e = await _valid(session, token)
    if not files or len(files) > MAX_PHOTOS:
        raise HTTPException(422, f"Send 1 to {MAX_PHOTOS} photos at a time")
    batch_id = new_id("b")
    paths: list[str] = []
    total = 0
    try:
        for i, f in enumerate(files):
            head = await f.read(16)
            kind = _kind(head)
            if kind is None:
                raise HTTPException(422, f"{f.filename or 'A file'} is not a photo (JPEG, PNG, WebP or HEIC)")
            if kind == "heic" and not heic_supported():
                raise HTTPException(422, "HEIC photos are not supported on this server yet — on the iPhone choose "
                                         "Settings › Camera › Formats › Most Compatible, or send JPEG")
            path = os.path.join(sources_dir(), f"{batch_id}_{i}.{kind}")
            with open(path, "wb") as out:
                out.write(head)
                total += len(head)
                while chunk := await f.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise HTTPException(413, "Photos can be at most 150 MB per send")
                    await asyncio.to_thread(out.write, chunk)
            paths.append(path)
    except HTTPException:
        for pth in paths + [os.path.join(sources_dir(), x) for x in os.listdir(sources_dir()) if x.startswith(batch_id)]:
            try:
                os.remove(pth)
            except OSError:
                pass
        raise
    session.add(ScanBatch(id=batch_id, exam_id=e.id, teacher_id=row.teacher_id, status="queued",
                          filename=f"Phone photos ({len(paths)})", sources=paths, created_at=utcnow(),
                          mode="unprinted" if row.unprinted else "printed", origin="phone"))
    await session.execute(update(CaptureToken).where(CaptureToken.token_hash == row.token_hash).values(
        uploads=CaptureToken.uploads + 1, photos=CaptureToken.photos + len(paths)))
    if e.status != "closed":
        t = utcnow()
        e.status, e.start_code, e.closed_at, e.updated_at = "closed", None, t, t
    await session.commit()
    await invalidate_exam(get_store(), e.id)
    _spawn(run_batch(batch_id))
    return {"batch_id": batch_id, "photos": len(paths)}

