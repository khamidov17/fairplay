"""Phase 1a: security headers and sign-in brute-force protection.

Security headers (pure ASGI middleware, no per-request allocation beyond the headers):
- X-Content-Type-Options: nosniff, Referrer-Policy: same-origin,
- X-Frame-Options: DENY + CSP frame-ancestors 'none' (nothing in FairPlay is framed:
  the Mac app loads pages top-level in a WKWebView, website exams navigate top-level too),
- a CSP for the SPA: scripts only from this origin (the Vite build has no inline script),
  inline styles allowed (React style props), images from self / data: (offline packages) /
  blob: / https: (question images may be https URLs), connections to self only,
- Strict-Transport-Security in production (browsers ignore it on plain http).
A route that sets one of these headers itself keeps its own value.

Sign-in limits (store-backed, so shared by every worker through Redis):
- per username: LOGIN_MAX_PER_USER failures in LOGIN_WINDOW_S -> locked LOGIN_LOCK_S;
- per IP: LOGIN_MAX_PER_IP failures in LOGIN_WINDOW_S -> locked LOGIN_LOCK_S.
The IP limit is high because a whole classroom often shares one NAT address. A locked
username can be unlocked early with `python -m app.manage unlock-login --username ...`.
"""
from __future__ import annotations

from fastapi import HTTPException

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob: https:; font-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")

LOGIN_WINDOW_S = 900
LOGIN_LOCK_S = 900
LOGIN_MAX_PER_USER = 10
LOGIN_MAX_PER_IP = 100


class SecurityHeadersMiddleware:
    def __init__(self, app, production: bool = False):
        self.app = app
        hdrs = [
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"same-origin"),
            (b"x-frame-options", b"DENY"),
            (b"content-security-policy", CSP.encode()),
            (b"cross-origin-opener-policy", b"same-origin"),
        ]
        if production:
            hdrs.append((b"strict-transport-security", b"max-age=31536000"))
        self.headers = hdrs

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                raw = list(message.get("headers") or [])
                have = {k.lower() for k, _ in raw}
                raw.extend((k, v) for k, v in self.headers if k not in have)
                message["headers"] = raw
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ---------------------------------------------------------------- sign-in limits
def _norm_user(username: str) -> str:
    return username.strip().lower()[:64]


def _keys(username: str, ip: str) -> tuple[str, str, str, str]:
    u = _norm_user(username)
    return f"lf:u:{u}", f"ll:u:{u}", f"lf:ip:{ip}", f"ll:ip:{ip}"


async def check_login_allowed(store, username: str, ip: str) -> None:
    """429 while the username or the IP is locked out."""
    _, ulock, _, iplock = _keys(username, ip)
    for key in (ulock, iplock):
        if await store.get(key):
            left = await store.ttl(key)
            mins = max(1, (left + 59) // 60) if left and left > 0 else LOGIN_LOCK_S // 60
            raise HTTPException(429, f"Too many failed sign-ins. Try again in {mins} minutes.")


async def note_login_failure(store, username: str, ip: str) -> bool:
    """Count a failure; returns True when this failure triggered a lockout."""
    ucount, ulock, ipcount, iplock = _keys(username, ip)
    locked = False
    if await store.incr(ucount, LOGIN_WINDOW_S) >= LOGIN_MAX_PER_USER:
        await store.set(ulock, "1", LOGIN_LOCK_S)
        await store.delete(ucount)
        locked = True
    if ip and await store.incr(ipcount, LOGIN_WINDOW_S) >= LOGIN_MAX_PER_IP:
        await store.set(iplock, "1", LOGIN_LOCK_S)
        await store.delete(ipcount)
        locked = True
    return locked


async def note_login_success(store, username: str) -> None:
    ucount, _, _, _ = _keys(username, "")
    await store.delete(ucount)


async def unlock_login(store, username: str) -> None:
    ucount, ulock, _, _ = _keys(username, "")
    await store.delete(ucount, ulock)
