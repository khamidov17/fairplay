"""Phase 1a: append-only audit log + per-attempt session timeline.

`audit_events` answers "what happened in this exam session?": sign-ins, exam open/close,
attempt start, answer saves (coalesced), connection gaps, submit / auto-submit, grade
changes, releases, offline package/key downloads, invigilator exit codes ...

Rows are never updated or deleted by the app (no foreign keys either, so deleting an exam
keeps its audit trail). Writes stay off the hot path:
- answer saves are coalesced in the store into one row per attempt per SAVE_WINDOW_S
  (first/last client_seq + count), written when the next window starts or at submit;
- heartbeats cost one store GETSET; a row is written only when a gap > GAP_S is seen;
- everything else is rare (one row per action).

Helpers for other modules:
- `add(session, action, ...)`  adds a row to the caller's transaction (caller commits);
- `await record(action, ...)`  writes in its own short transaction, never raises.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import JSON, BigInteger, DateTime, Index, String, and_, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, get_database, get_session
from .models import Attempt, BigIntPK, Exam, IntegrityEvent, User
from .security import Principal, require_teacher
from .timeutil import iso, parse_iso, utcnow

log = logging.getLogger("proctora.audit")

SAVE_WINDOW_S = 300           # one answer_saved row per attempt per 5 minutes at most
GAP_S = 30                    # heartbeats every 5 s: 30 s without one = connection lost
STATE_TTL = 26 * 3600
ACTIONS = (
    "login_ok", "login_failed", "login_locked", "password_changed", "sessions_revoked",
    "exam_opened", "exam_closed", "deadline_extended", "start_code_wrong",
    "attempt_started", "attempt_resumed", "answer_saved", "connection_lost",
    "connection_restored", "submitted", "auto_submitted", "grade_changed", "comment_changed",
    "grades_released", "grades_unreleased", "package_downloaded", "package_key_released",
    "exit_code_used", "exit_code_rejected",
)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_exam_attempt_at", "exam_id", "attempt_id", "at"),
                      Index("ix_audit_attempt_at", "attempt_id", "at"),
                      Index("ix_audit_actor_at", "actor_id", "at"))
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_role: Mapped[str] = mapped_column(String(16), nullable=False, default="",
                                            server_default="")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    user_agent: Mapped[str] = mapped_column(String(300), nullable=False, default="",
                                            server_default="")
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    exam_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- writing
def client_ip(request: Request | None) -> str:
    """Peer address. Behind nginx run uvicorn with --proxy-headers (and
    FORWARDED_ALLOW_IPS) so this is the real client, not the proxy."""
    if request is None or request.client is None:
        return ""
    return (request.client.host or "")[:64]


def user_agent(request: Request | None) -> str:
    if request is None:
        return ""
    return (request.headers.get("user-agent") or "")[:300]


def row(action: str, *, request: Request | None = None, actor_id: int | None = None,
        actor_role: str = "", exam_id: str | None = None, attempt_id: str | None = None,
        detail: dict | None = None, at: datetime | None = None, ip: str | None = None,
        ua: str | None = None) -> dict:
    return {"at": at or utcnow(), "actor_id": actor_id, "actor_role": actor_role or "",
            "ip": ip if ip is not None else client_ip(request),
            "user_agent": ua if ua is not None else user_agent(request),
            "action": action[:40], "exam_id": exam_id, "attempt_id": attempt_id,
            "detail": detail or {}}


def add(session: AsyncSession, action: str, **kw) -> None:
    """Add an audit row to the caller's transaction (the caller commits)."""
    session.add(AuditEvent(**row(action, **kw)))


def add_many(session: AsyncSession, rows: list[dict]) -> None:
    for r in rows:
        session.add(AuditEvent(**r))


async def record_rows(rows: list[dict]) -> None:
    if not rows:
        return
    try:
        async with get_database().sessionmaker() as s:
            await s.execute(insert(AuditEvent), rows)
            await s.commit()
    except Exception:  # noqa: BLE001 - the audit log must never break the request
        log.exception("audit write failed (%s)", ",".join(r["action"] for r in rows))


async def record(action: str, **kw) -> None:
    """Write one audit row in its own short transaction. Never raises."""
    await record_rows([row(action, **kw)])


# ---------------------------------------------------------------- coalesced answer saves
def _save_key(aid: str) -> str:
    return f"aud:sv:{aid}"


def _save_row(aid: str, w: dict) -> dict:
    return row("answer_saved", actor_id=w.get("u"), actor_role="student", exam_id=w.get("e"),
               attempt_id=aid, at=_utc(w["t0"]),
               ip=w.get("ip", ""), ua=w.get("ua", ""),
               detail={"first_seq": w["f"], "last_seq": w["l"], "count": w["n"],
                       "until": iso(_utc(w["t1"]))})


async def note_answer_saved(store, aid: str, exam_id: str | None, student_id: int, seq: int,
                            request: Request | None) -> None:
    """Called after a successful save: two store ops; a DB row only when a window closes."""
    try:
        now = time.time()
        win = int(now // SAVE_WINDOW_S)
        raw = await store.get(_save_key(aid))
        cur = json.loads(raw) if raw else None
        if cur and cur.get("w") == win:
            cur["l"] = max(cur["l"], seq)
            cur["f"] = min(cur["f"], seq)
            cur["n"] += 1
            cur["t1"] = now
            await store.set(_save_key(aid), json.dumps(cur), STATE_TTL)
            return
        if exam_id is None:  # once per window: the attempt meta cached at start
            meta = await store.get(f"att:{aid}")
            exam_id = json.loads(meta).get("exam_id") if meta else None
        new = {"w": win, "f": seq, "l": seq, "n": 1, "t0": now, "t1": now, "e": exam_id,
               "u": student_id, "ip": client_ip(request), "ua": user_agent(request)}
        await store.set(_save_key(aid), json.dumps(new), STATE_TTL)
        if cur:
            await record_rows([_save_row(aid, cur)])
    except Exception:  # noqa: BLE001
        log.exception("audit answer_saved failed")


async def flush_answer_saves(store, aid: str) -> list[dict]:
    """Rows for the open save window of an attempt (and forget it). Used at submit/close."""
    raw = await store.get(_save_key(aid))
    if not raw:
        return []
    await store.delete(_save_key(aid))
    return [_save_row(aid, json.loads(raw))]


async def pending_answer_saves(store, aid: str) -> list[dict]:
    raw = await store.get(_save_key(aid))
    return [_save_row(aid, json.loads(raw))] if raw else []


# ---------------------------------------------------------------- heartbeat gaps
def _hb_key(aid: str) -> str:
    return f"aud:hb:{aid}"


async def note_heartbeat(store, aid: str, exam_id: str, student_id: int,
                         request: Request | None) -> None:
    """One store GETSET per heartbeat; writes connection_lost/restored only after a gap."""
    try:
        now = time.time()
        prev = await store.getset(_hb_key(aid), repr(now), STATE_TTL)
        if prev is None:
            return
        gap = now - float(prev)
        if gap <= GAP_S:
            return
        common = dict(actor_id=student_id, actor_role="student", exam_id=exam_id, attempt_id=aid)
        await record_rows([
            row("connection_lost", at=_utc(float(prev)), ip="", ua="",
                detail={"last_heartbeat": iso(_utc(float(prev)))}, **common),
            row("connection_restored", request=request, detail={"gap_s": round(gap)}, **common),
        ])
    except Exception:  # noqa: BLE001
        log.exception("audit heartbeat failed")


async def last_heartbeat(store, aid: str) -> float | None:
    raw = await store.get(_hb_key(aid))
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


async def once(store, key: str, ttl: int = STATE_TTL) -> bool:
    """True the first time `key` is seen within ttl (coalesces repeated downloads etc.)."""
    try:
        return await store.getset(key, "1", ttl) is None
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------- timeline
router = APIRouter(prefix="/api/exams", tags=["audit"])

INTEGRITY_TEXT = {
    "lock_started": "Computer locked for the exam",
    "lock_ended": "Computer unlocked",
    "blocked_shortcut": "Blocked a keyboard shortcut",
    "other_app_foreground": "Another app came to the front",
    "other_app_launched": "Another app was opened",
    "space_changed": "Tried to switch desktop (Space)",
    "extra_display": "Extra display connected",
    "blocked_navigation": "Blocked a website outside the allowed list",
    "emergency_exit": "Invigilator exit code entered on the computer",
    "emergency_exit_rejected": "Wrong invigilator exit code entered on the computer",
    "safety_unlock": "Unlocked by the development safety timer",
    "tap_unavailable": "Lockdown keyboard guard unavailable (no Accessibility permission)",
    "secure_input_on": "Another app holds secure keyboard input",
    "external_finished": "Finished the website exam",
    "focus_lost": "The exam window lost focus",
    "visibility_hidden": "The exam page was hidden",
    "copy": "Tried to copy",
    "paste": "Tried to paste",
}
NON_FLAG = ("lock_started", "lock_ended", "external_finished")


def _dur(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s} s" if s else f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min"


def summarize(action: str, d: dict) -> str:
    d = d or {}
    if action == "login_ok":
        return "Signed in"
    if action == "login_failed":
        return "Failed sign-in (wrong password)"
    if action == "login_locked":
        return "Sign-in blocked after too many failed attempts"
    if action == "password_changed":
        return "Changed password (signed out on other devices)"
    if action == "sessions_revoked":
        return "Signed out everywhere"
    if action == "exam_opened":
        return "Teacher opened the exam"
    if action == "exam_closed":
        n = d.get("auto_submitted") or 0
        return "Teacher closed the exam" + (f" ({n} still writing were submitted automatically)" if n else "")
    if action == "deadline_extended":
        return f"Teacher gave {d.get('minutes')} more minutes"
    if action == "start_code_wrong":
        return "Typed a wrong start code"
    if action == "attempt_started":
        if d.get("offline"):
            return f"Started the exam offline at {d.get('offline_started_at')} (reached the server later)"
        return "Started the exam"
    if action == "attempt_resumed":
        return "Reopened the exam (resume)"
    if action == "answer_saved":
        n = d.get("count", 0)
        rng = (f"#{d.get('first_seq')}" if d.get("first_seq") == d.get("last_seq")
               else f"#{d.get('first_seq')}–#{d.get('last_seq')}")
        return f"Answers saved {n} time{'s' if n != 1 else ''} (save {rng})"
    if action == "connection_lost":
        return "Connection lost (no heartbeat from the exam page)"
    if action == "connection_restored":
        return f"Connection restored after {_dur(d.get('gap_s') or 0)}"
    if action == "submitted":
        s = f"Submitted (receipt {d.get('receipt')})"
        if d.get("late"):
            s += " after the deadline"
        return s
    if action == "auto_submitted":
        return f"Submitted automatically when the teacher closed the exam (receipt {d.get('receipt')})"
    if action == "grade_changed":
        q = d.get("question_index")
        qn = f"question {q + 1}" if isinstance(q, int) else "a question"
        if d.get("points") is None:
            return f"Teacher cleared the grade of {qn}"
        old = d.get("old_points")
        frm = f" (was {old})" if old is not None else ""
        return f"Teacher graded {qn}: {d.get('points')} points{frm}"
    if action == "comment_changed":
        return "Teacher changed the overall comment"
    if action == "grades_released":
        return "Grades released to students"
    if action == "grades_unreleased":
        return "Grades hidden from students again"
    if action == "package_downloaded":
        return "Downloaded the offline exam package"
    if action == "package_key_released":
        return "Received the offline unlock key"
    if action == "exit_code_used":
        return "Invigilator exit code accepted: computer unlocked"
    if action == "exit_code_rejected":
        return "Wrong invigilator exit code tried"
    return action.replace("_", " ").capitalize()


def _integrity_summary(ev: IntegrityEvent) -> str:
    base = INTEGRITY_TEXT.get(ev.type, ev.type.replace("_", " ").capitalize())
    if ev.detail:
        base += f": {ev.detail}"
    if ev.review and ev.review != "open":
        base += f" (teacher marked {ev.review})"
    return base


def _event_time(ev: IntegrityEvent, lo: datetime) -> datetime:
    """Client time when believable (events from an offline period arrive in a batch),
    else the server time."""
    if ev.client_time:
        try:
            ct = parse_iso(ev.client_time)
            if lo - timedelta(hours=1) <= ct <= ev.server_time + timedelta(minutes=1):
                return ct
        except ValueError:
            pass
    return ev.server_time


ACTOR_ORDER = {"system": 0, "teacher": 1, "student": 2}


@router.get("/{exam_id}/attempts/{attempt_id}/timeline")
async def timeline(exam_id: str, attempt_id: str, p: Principal = Depends(require_teacher),
                   session: AsyncSession = Depends(get_session)):
    from .store import get_store
    e = await session.get(Exam, exam_id)
    if e is None or e.teacher_id != p.user_id:
        raise HTTPException(404, "Exam not found")
    a = await session.get(Attempt, attempt_id)
    if a is None or a.exam_id != exam_id:
        raise HTTPException(404, "Attempt not found")
    student = await session.get(User, a.student_id)
    lo = a.started_at - timedelta(hours=3)
    hi = (a.submitted_at or utcnow()) + timedelta(hours=1)

    rows = (await session.execute(
        select(AuditEvent).where(or_(
            AuditEvent.attempt_id == attempt_id,
            # exam-wide context: open / close / extend / release
            and_(AuditEvent.exam_id == exam_id, AuditEvent.attempt_id.is_(None),
                 AuditEvent.actor_role != "student"),
            # this student's own exam-level actions (wrong codes, package downloads)
            and_(AuditEvent.exam_id == exam_id, AuditEvent.attempt_id.is_(None),
                 AuditEvent.actor_id == a.student_id),
            # this student's sign-ins around the session
            and_(AuditEvent.actor_id == a.student_id, AuditEvent.exam_id.is_(None),
                 AuditEvent.at >= lo, AuditEvent.at <= hi),
        )).order_by(AuditEvent.at, AuditEvent.id))).scalars().all()
    items: list[dict] = []
    seq = 0

    def push(at: datetime, source: str, action: str, summary: str, actor: str,
             detail: dict | None = None, ip: str | None = None, tone: str = "info") -> None:
        nonlocal seq
        seq += 1
        items.append({"at": iso(at), "_at": at, "_seq": seq, "source": source, "action": action,
                      "summary": summary, "actor": actor, "ip": ip or None,
                      "detail": detail or {}, "tone": tone})

    warn = {"login_failed", "login_locked", "start_code_wrong", "connection_lost",
            "exit_code_used", "exit_code_rejected"}
    good = {"submitted", "auto_submitted", "connection_restored", "attempt_started"}
    for r in rows:
        if r.action in ("exam_opened", "exam_closed", "deadline_extended") and not (lo <= r.at <= hi):
            continue
        actor = r.actor_role or "system"
        d = dict(r.detail or {})
        d.pop("receipt_hash", None)
        push(r.at, "audit", r.action, summarize(r.action, d), actor, d, r.ip,
             "warn" if r.action in warn or (r.action == "submitted" and d.get("late"))
             else "good" if r.action in good else "info")
    store = get_store()
    for r in await pending_answer_saves(store, attempt_id):
        push(r["at"], "audit", "answer_saved", summarize("answer_saved", r["detail"]), "student",
             r["detail"], r["ip"])

    evs = (await session.execute(
        select(IntegrityEvent).where(IntegrityEvent.exam_id == exam_id,
                                     IntegrityEvent.attempt_id == attempt_id)
        .order_by(IntegrityEvent.server_time, IntegrityEvent.id))).scalars().all()
    for ev in evs:
        push(_event_time(ev, a.started_at), "integrity", ev.type, _integrity_summary(ev), "student",
             {"event_id": ev.id, "detail": ev.detail, "client_time": ev.client_time,
              "server_time": iso(ev.server_time), "review": ev.review or "open"},
             tone="info" if ev.type in NON_FLAG else "warn")

    if a.status == "in_progress":
        last = await last_heartbeat(store, attempt_id)
        if last is not None and time.time() - last > GAP_S:
            at = _utc(last)
            push(at, "heartbeat", "no_heartbeat",
                 f"No heartbeat since {iso(at)} ({_dur(time.time() - last)} ago)", "system",
                 {"last_heartbeat": iso(at)}, tone="warn")

    items.sort(key=lambda it: (it["_at"], it["_seq"]))
    for it in items:
        del it["_at"], it["_seq"]
    return {
        "exam_id": exam_id, "attempt_id": attempt_id, "exam_title": e.title,
        "student": {"username": student.username if student else "",
                    "name": student.name if student else ""},
        "status": a.status, "started_at": iso(a.started_at), "deadline_at": iso(a.deadline_at),
        "submitted_at": iso(a.submitted_at), "receipt": a.receipt, "late": bool(a.late),
        "auto_submitted": bool(a.auto_submitted),
        "events": items,
    }
