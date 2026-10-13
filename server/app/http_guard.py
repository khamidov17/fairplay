"""Audit 2026-10-12 L-04 + S-16: request size limits before any parsing, and security
headers on every response. Pure ASGI (no buffering of bodies)."""
from __future__ import annotations

import json
import re

MB = 1024 * 1024
SCAN_UPLOAD = re.compile(r"^/api/exams/[^/]+/scans$")
LIMITS = (                      # (path test, max body bytes); first match wins
    (lambda p: bool(SCAN_UPLOAD.match(p)), 210 * MB),   # routes_scan: 200 MB per upload
    (lambda p: p == "/api/uploads", 6 * MB),             # uploads: 5 MB images
)
DEFAULT_LIMIT = 8 * MB          # answers, exams, everything else

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob: https:; font-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


def limit_for(path: str) -> int:
    for test, n in LIMITS:
        if test(path):
            return n
    return DEFAULT_LIMIT


class GuardMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        limit = limit_for(path)
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = True
            if too_big:
                return await _reject(send, limit)
        https = scope.get("scheme") == "https"

        seen = 0
        stopped = False

        async def send_with_headers(msg):
            if stopped:
                return              # the app answered a cut-off body: our 413 goes instead
            if msg["type"] == "http.response.start":
                h = list(msg.get("headers") or [])
                names = {k.lower() for k, _ in h}
                extra = [(b"x-content-type-options", b"nosniff"),
                         (b"x-frame-options", b"DENY"),
                         (b"referrer-policy", b"no-referrer"),
                         (b"cross-origin-opener-policy", b"same-origin")]
                ctype = next((v for k, v in h if k.lower() == b"content-type"), b"")
                if ctype.startswith(b"text/html"):
                    extra.append((b"content-security-policy", CSP.encode()))
                if https:
                    extra.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                h += [(k, v) for k, v in extra if k not in names]
                msg = {**msg, "headers": h}
            await send(msg)

        # Chunked bodies (no Content-Length): count while streaming; past the limit the app
        # sees a disconnect and its answer is replaced by 413.
        async def counting_receive():
            nonlocal seen, stopped
            if stopped:
                return {"type": "http.disconnect"}
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    stopped = True
                    return {"type": "http.disconnect"}
            return msg

        try:
            await self.app(scope, counting_receive if declared is None else receive, send_with_headers)
        except Exception:
            if not stopped:
                raise
        if stopped:
            await _reject(send, limit)


async def _reject(send, limit: int) -> None:
    body = json.dumps({"detail": f"Request too large (at most {limit // MB} MB)"}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class SignPrivateUploads:
    """Audit L-18: JSON answers carry private upload links (/api/uploads/p<id>) as stored;
    give each one an expiring signature on the way out."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path.startswith("/api/") or path.startswith("/api/uploads"):
            return await self.app(scope, receive, send)
        from .uploads import PRIVATE_URL_RE, sign_url
        start: dict | None = None
        chunks: list[bytes] = []

        async def capture(msg):
            nonlocal start
            if msg["type"] == "http.response.start":
                ctype = next((v for k, v in msg.get("headers") or [] if k.lower() == b"content-type"), b"")
                if not ctype.startswith(b"application/json"):
                    start = None
                    await send(msg)
                    capture.passthrough = True      # type: ignore[attr-defined]
                    return
                start = msg
                return
            if getattr(capture, "passthrough", False):
                await send(msg)
                return
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body"):
                body = b"".join(chunks)
                if b"/api/uploads/p" in body:
                    secret = scope["app"].state.settings.secret
                    text = PRIVATE_URL_RE.sub(lambda m: sign_url(secret, m.group(1)), body.decode("utf-8"))
                    body = text.encode("utf-8")
                headers = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": body})

        capture.passthrough = False                 # type: ignore[attr-defined]
        await self.app(scope, receive, capture)
