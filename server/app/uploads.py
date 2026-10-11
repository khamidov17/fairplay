"""Slice 6 B: uploaded images (question pictures; also usable for scan crops).

Files live on local disk under Settings.uploads_dir as `<id>.<ext>` where id is 128 random
bits (hex). `GET /api/uploads/{id}` needs no auth (ids are unguessable) and is cached for a
year (content never changes for an id). Only png / jpeg / gif / webp are accepted, checked
by their bytes (never by the file name or the browser's content type); SVG is rejected
because it can carry scripts.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import secrets

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .security import Principal, require_teacher

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
UPLOAD_URL_PREFIX = "/api/uploads/"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
_uploads_dir: str | None = None


def set_uploads_dir(path: str) -> None:
    global _uploads_dir
    _uploads_dir = path


def uploads_dir() -> str:
    if _uploads_dir is None:
        from .config import Settings
        set_uploads_dir(Settings().uploads_dir)
    return _uploads_dir  # type: ignore[return-value]


def sniff_image(data: bytes) -> str | None:
    """File extension for png/jpeg/gif/webp bytes, else None (SVG, PDF, anything else)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _image_size(data: bytes) -> tuple[int, int]:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        im.verify()  # structural check; raises on a broken file
    with Image.open(io.BytesIO(data)) as im:
        return int(im.width), int(im.height)


def save_image_bytes(data: bytes) -> dict:
    """Validate and store image bytes (blocking: call through asyncio.to_thread).
    Returns {"id","url","width","height"}; raises ValueError with a readable reason."""
    if not data:
        raise ValueError("The file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Images can be at most 5 MB")
    ext = sniff_image(data)
    if ext is None:
        raise ValueError("Use a PNG, JPEG, GIF or WebP image")
    try:
        width, height = _image_size(data)
    except Exception:  # noqa: BLE001 - Pillow raises many types for broken files
        raise ValueError("This image file is damaged or not supported") from None
    uid = secrets.token_hex(16)
    d = uploads_dir()
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{uid}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, os.path.join(d, f"{uid}.{ext}"))
    return {"id": uid, "url": f"{UPLOAD_URL_PREFIX}{uid}", "width": width, "height": height}


def find_upload(uid: str) -> tuple[str, str] | None:
    """(path, media type) of a stored upload, or None."""
    if not ID_RE.match(uid):
        return None
    d = uploads_dir()
    for ext, media in MEDIA_TYPES.items():
        path = os.path.join(d, f"{uid}.{ext}")
        if os.path.isfile(path):
            return path, media
    return None


def read_upload(uid: str) -> tuple[bytes, str] | None:
    """Bytes + media type of an upload (blocking)."""
    found = find_upload(uid)
    if found is None:
        return None
    with open(found[0], "rb") as f:
        return f.read(), found[1]


@router.post("", status_code=201)
async def upload(file: UploadFile = File(...),
                 p: Principal = Depends(require_teacher)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        return await asyncio.to_thread(save_image_bytes, data)
    except ValueError as e:
        status = 413 if len(data) > MAX_UPLOAD_BYTES else 422
        raise HTTPException(status, str(e)) from None


@router.get("/{uid}")
async def get_upload(uid: str):
    found = await asyncio.to_thread(find_upload, uid)
    if found is None:
        raise HTTPException(404, "Not found")
    path, media = found
    return FileResponse(path, media_type=media, headers={
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    })
