"""Files for Scan & Grade.

Crops, page previews and deskewed page images are stored as PRIVATE uploads (audit L-18):
served at /api/uploads/<id> only with an expiring signature, which API responses add on
the way out (app.http_guard.SignPrivateUploads), so <img> tags work everywhere (grading
queue, feedback) without anyone holding a permanent link to a student's handwriting. Uploaded scan files (PDF/images)
are kept privately in a sibling folder `scan-sources/` while they are processed and are
never served.
"""
from __future__ import annotations

import base64
import os
import re

from .. import uploads

UPLOAD_RE = re.compile(r"^/api/uploads/([A-Za-z0-9_-]{16,64})$")


def sources_dir() -> str:
    root = os.environ.get("FAIRPLAY_SCAN_SOURCES_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(uploads.uploads_dir())), "scan-sources")
    os.makedirs(root, exist_ok=True)
    return root


def save_file(data: bytes, ext: str = "png") -> str:
    """Store PNG/JPEG bytes as an upload (blocking); returns its URL."""
    return uploads.save_image_bytes(data, private=True)["url"]


def _uid(url: str | None) -> str | None:
    m = UPLOAD_RE.match(url or "")
    return m.group(1) if m else None


def path_for_url(url: str | None) -> str | None:
    uid = _uid(url)
    if uid is None:
        return None
    found = uploads.find_upload(uid)
    return found[0] if found else None


def read_url(url: str | None) -> bytes | None:
    uid = _uid(url)
    if uid is None:
        return None
    got = uploads.read_upload(uid)
    return got[0] if got else None


def resolve_image(url: str) -> bytes | None:
    """Bytes of a question image for printing (uploads or data: URLs)."""
    if not url:
        return None
    if url.startswith("data:"):
        try:
            return base64.b64decode(url.split(",", 1)[1])
        except Exception:
            return None
    return read_url(url)
