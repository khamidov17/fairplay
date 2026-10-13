"""FairPlay app checks (audit 2026-10-12, fix order #1).

The Mac app has an HMAC key compiled in (mac/build.sh FAIRPLAY_APP_KEY; the server gets
the same value). It proves it is the app, not a browser, by signing:

  start  "start|<nonce>|<device_id>"                     nonce from GET .../challenge
  beat   "beat|<aid>|<session>|<device_id>|<ts>|<0|1>|<checks>"

A key inside an app can be dug out by someone determined, so this stops "open the exam
in Chrome" and scripted fakes, not a reverse engineer. The next step is Apple App Attest
/ a notarized-build check (see CONTRACT.md "App session").

Every attempt is then bound to one device and one session id (X-FairPlay-Session).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import HTTPException, Request
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .models import IntegrityEvent
from .store import Store
from .timeutil import utcnow

SESSION_HEADER = "x-fairplay-session"
CHALLENGE_TTL_S = 120
APP_REQUIRED_DETAIL = "Open this exam in the FairPlay app. It cannot be taken in a web browser."
APP_PROOF_DETAIL = "The FairPlay app could not be verified. Quit and reopen it, then try again."
OTHER_DEVICE_DETAIL = ("This exam is already open on another computer. "
                       "Ask the invigilator to move it to this one.")
SESSION_DETAIL = "This exam was opened somewhere else. Ask the invigilator for help."
# Gaps in the app's own signal that the teacher should see (app killed, Mac asleep, cable).
APP_GAP_S = 20
NO_APP_SIGNAL_S = 45
FLAG_REPEAT_S = 300
LIVE_TTL = 26 * 3600


# Audit S-06: event types a client (the exam page, the Mac app through it, the app's
# watchdog) may report. Types the server itself writes can't be posted: they are stored
# as "client_<type>" so they never look like the server's own flags.
CLIENT_EVENT_TYPES = frozenset({
    # exam page
    "blocked_shortcut", "app_switch", "focus_lost", "window_blur", "paste", "copy",
    "lock_failed", "offline", "fullscreen_exit",
    # Mac app (EventLog in mac/Sources/Proctora/Support.swift)
    "lock_started", "lock_ended", "tap_unavailable", "safety_unlock", "secure_input_on",
    "other_app_foreground", "other_app_launched", "space_changed", "extra_display",
    "blocked_navigation", "external_finished", "process_blocked", "vm_detected",
    "mirrored_display", "remote_session", "tap_lost", "input_source_blocked",
    "synthetic_input", "tls_pin_mismatch", "previous_run_killed",
    "offline_exit", "offline_exit_rejected", "watchdog_app_killed",
})
SERVER_EVENT_TYPES = frozenset({
    "second_device", "foreign_session", "re_entry", "app_gap", "no_app_signal",
    "unlocked_in_exam", "emergency_exit", "emergency_exit_rejected", "device_released",
    "device_bound", "late_submission", "offline_key_requested_twice",
    "offline_exit_code_issued", "event_limit_reached", "off_console", "secure_input_other_app",
})


def client_event_type(raw: str) -> str:
    t = (raw or "").strip()[:56] or "unknown"
    return t if t in CLIENT_EVENT_TYPES else f"client_{t}"


def sign(key: str, message: str) -> str:
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def verify(settings: Settings, message: str, signature: str) -> bool:
    return any(hmac.compare_digest(sign(k, message), signature) for k in settings.app_keys)


def start_message(nonce: str, device_id: str) -> str:
    return f"start|{nonce}|{device_id}"


OFFLINE_CHALLENGE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def normalize_mac_code(raw: str) -> str | None:
    """"k7qm-3xpa" -> "K7QM3XPA"; None unless it is a Mac code the app can show."""
    c = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if len(c) != 8 or any(ch not in OFFLINE_CHALLENGE_ALPHABET for ch in c):
        return None
    return c


def offline_exit_code(key: str | bytes, mac_code: str) -> str:
    """Offline invigilator exit (mac/Sources/Proctora/OfflineExit.swift): the locked Mac
    shows a random Mac code; this 8-digit answer unlocks it without the server.
    `key` is the attempt's offline key (bytes) in real use; the app key (str) only for
    dev builds that never got one."""
    k = key if isinstance(key, bytes) else key.encode()
    mac = hmac.new(k, f"offline-exit|{mac_code}".encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(mac[:4], 'big') % 100_000_000:08d}"


def offline_key(server_secret: str, aid: str, session: str) -> bytes:
    """Per attempt + app session, derived from the server secret (never in the app
    binary). The app fetches it once right after locking (offline_key_message)."""
    return hmac.new(server_secret.encode(), f"offline-exit-key|{aid}|{session}".encode(),
                    hashlib.sha256).digest()


def offline_key_message(aid: str, session: str, device_id: str, ts: int) -> str:
    return f"offkey|{aid}|{session}|{device_id}|{ts}"


def handin_message(aid: str, session: str, status: str, nonce: str) -> str:
    return f"handin|{aid}|{session}|{status}|{nonce}"


def exit_message(aid: str, session: str, nonce: str) -> str:
    """Audit 2026-10-13: the invigilator-code reply is signed like the hand-in status, so a
    man in the middle can't answer {"ok": true} for a code the server never accepted."""
    return f"exit|{aid}|{session}|ok|{nonce}"


def parse_checks(checks: str) -> dict[str, str]:
    """"vm=0,disp=1,console=1" -> {"vm": "0", ...} (audit M-02)."""
    out: dict[str, str] = {}
    for part in (checks or "").split(","):
        k, _, v = part.partition("=")
        if k.strip():
            out[k.strip()] = v.strip()
    return out


class AppSafe:
    @staticmethod
    def token(s: str, lo: int, hi: int) -> bool:
        return lo <= len(s) <= hi and all(c.isalnum() or c in "-_" for c in s) and s.isascii()


def beat_message(aid: str, session: str, device_id: str, ts: int, locked: bool, checks: str) -> str:
    return f"beat|{aid}|{session}|{device_id}|{ts}|{1 if locked else 0}|{checks}"


def session_header(request: Request) -> str | None:
    v = request.headers.get(SESSION_HEADER)
    return v.strip() if v and v.strip() else None


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


async def add_event(session: AsyncSession, store: Store, aid: str, exam_id: str, typ: str,
                    detail: str = "", commit: bool = True) -> None:
    """Server-side integrity event (shown to the teacher like the app's own)."""
    await session.execute(insert(IntegrityEvent), [{
        "attempt_id": aid, "exam_id": exam_id, "type": typ[:64], "detail": detail[:500],
        "client_time": None, "server_time": utcnow()}])
    if commit:
        await session.commit()
    await store.hincrby(f"evc:{exam_id}", aid, 1, LIVE_TTL)
    await store.hset(f"evl:{exam_id}", aid, f"{typ} {detail}".strip(), LIVE_TTL)


async def once_per(store: Store, key: str, seconds: int) -> bool:
    """True the first time in `seconds` (cheap de-duplication of repeated flags)."""
    if await store.get(key):
        return False
    await store.set(key, "1", seconds)
    return True


async def check_session(request: Request, session: AsyncSession, store: Store, aid: str,
                        meta: dict) -> None:
    """Writes/reads of an attempt must come from its current app session."""
    bound = meta.get("session_id")
    given = session_header(request)
    if bound is None or given == bound:
        return
    if given is None and not settings_of(request).require_app:
        return  # browser demo mode: unbound requests still work
    if await once_per(store, f"fs:{aid}", 60):
        await add_event(session, store, aid, meta["exam_id"], "foreign_session",
                        "request from another login or an old session" if given
                        else "request without the app session")
    raise HTTPException(409, SESSION_DETAIL)


async def note_js_heartbeat(request: Request, session: AsyncSession, store: Store, aid: str,
                            meta: dict) -> None:
    """The exam page is alive; flag it when the app itself has gone quiet (killed app,
    page running outside the app, tap-less copy...)."""
    if not settings_of(request).require_app or meta.get("status") != "in_progress":
        return
    raw = await store.get(f"abt:{aid}")
    now = time.time()
    if raw:
        quiet = now - json.loads(raw)["t"]
    else:
        from .timeutil import parse_iso
        started = parse_iso(meta["started_at"]) if meta.get("started_at") else None
        quiet = (utcnow() - started).total_seconds() if started else 0
    if quiet > NO_APP_SIGNAL_S and await once_per(store, f"nas:{aid}", FLAG_REPEAT_S):
        await add_event(session, store, aid, meta["exam_id"], "no_app_signal",
                        f"exam page open but no signal from the app for {int(quiet)} s")
