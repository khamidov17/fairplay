from __future__ import annotations

import json
import time
from datetime import timedelta

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, func, insert, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .grading import exam_questions, grade_attempt, is_answered, num, percent
from .models import Attempt, Exam, IntegrityEvent
from . import app_client as ac
from .access import member_of_exam, require_member
from .schemas import AnswersIn, AppBeatIn, EventsIn, ExitCodeIn, HeartbeatIn, OfflineKeyIn, StartIn
from .security import Principal, require_student
from .services import (LIVE_TTL, attempt_view, cache_attempt, find_attempt, get_attempt_meta,
                       get_exam_cached, new_id, new_receipt, new_session_id)
from .store import get_store
from .timeutil import iso, utcnow

router = APIRouter(prefix="/api/student", tags=["student"])

MAX_WRONG_CODES = 5
WRONG_CODE_LOCK_S = 60
WRONG_CODE_WINDOW_S = 600
LATE_GRACE = timedelta(seconds=60)
# Audit S-06
EVENT_REQUESTS_PER_MIN = 60
MAX_EVENTS_PER_ATTEMPT = 5000
EVENTS_AFTER_SUBMIT = timedelta(minutes=10)
OFFLINE_START_MAX = timedelta(minutes=30)
# Invigilator exit code (checked here, never sent to the app): 5 wrong -> 5 min pause.
MAX_WRONG_EXIT = 5
WRONG_EXIT_LOCK_S = 300
# A second start on the same Mac within this window is a double click, not a re-entry.
REENTRY_QUIET_S = 15
# Audit 2026-10-13 (hardening): per-student / per-attempt request budgets.
CHALLENGES_PER_MIN = 30
ANSWER_WRITES_PER_MIN = 240
SUBMIT_RETRIES = 3


async def take_code_try(store, lock_key: str, count_key: str, limit: int, window_s: int,
                        detail: str) -> int:
    """Audit 2026-10-13 (wrong-code lockouts were check-then-act): reserve the try FIRST
    (one atomic INCR), then look at the lock. However many requests race, at most `limit`
    of them get to compare a code before the lock is set. Returns the try number."""
    n = await store.incr(count_key, window_s)
    if n > limit or await store.get(lock_key):
        raise HTTPException(429, detail)
    return n


async def wrong_code(store, lock_key: str, count_key: str, n: int, limit: int,
                     lock_s: int) -> None:
    """The n-th reserved try was wrong: the last allowed one sets the lock. The lock goes
    on before the counter is cleared, so a try reserved in between still sees it."""
    if n >= limit:
        await store.set(lock_key, "1", lock_s)
        await store.delete(count_key)


def same_code(given: str, code: str | None) -> bool:
    """Constant-time compare of bytes (compare_digest raises on non-ASCII str)."""
    return bool(code) and secrets.compare_digest(given.strip().encode(), str(code).encode())


def count_answered(answers: dict) -> int:
    """Audit S-10: answered questions in an answer map (any non-empty value)."""
    return sum(1 for v in (answers or {}).values() if is_answered(v))


def offline_start(offline_started_at, now):
    """Slice 6: start time for an attempt begun offline. Up to 30 min in the past is
    accepted; older values are clamped to 30 min, future ones to now."""
    if offline_started_at is None:
        return now
    return min(now, max(offline_started_at, now - OFFLINE_START_MAX))


@router.get("/exams")
async def my_exams(request: Request, p: Principal = Depends(require_student),
                   session: AsyncSession = Depends(get_session)):
    from .offline import package_id
    secret = ac.settings_of(request).secret
    rows = (await session.execute(
        select(Exam.id, Exam.title, Exam.duration_min, Exam.question_count, Exam.status,
               Attempt.id, Attempt.status, Attempt.submitted_at, Attempt.receipt,
               Exam.course, Exam.room, Exam.scheduled_at, Exam.grades_released,
               Exam.total_points, Attempt.score, Exam.mode, Exam.delivery, Exam.start_code)
        .outerjoin(Attempt, and_(Attempt.exam_id == Exam.id, Attempt.student_id == p.user_id))
        # Audit S-03: live exams of the student's classes / LMS courses, plus their own attempts.
        .where(or_(and_(Exam.status == "live", member_of_exam(p.user_id)), Attempt.id.is_not(None)))
        # slice 6: paper exams are printed; students never see them in the app
        .where(or_(Exam.delivery.is_(None), Exam.delivery != "paper"))
        .order_by(Exam.created_at.desc(), Exam.id))).all()
    out = []
    for (eid, title, dur, qc, status, aid, astatus, sub, receipt, course, room, sched, released,
         total, score, mode, delivery, code) in rows:
        mode = mode or "questions"
        delivery = delivery or ("website" if mode == "external" else "computer")
        attempt = None
        if aid is not None:
            released = bool(released)
            show = released and astatus == "submitted"
            attempt = {"attempt_id": aid, "status": astatus, "submitted_at": iso(sub),
                       "receipt": receipt, "released": released,
                       "percent": percent(score or 0.0, total) if show else None}
        out.append({"id": eid, "title": title, "duration_min": dur, "question_count": qc,
                    "status": status, "course": course or "", "room": room or "",
                    "scheduled_at": iso(sched), "mode": mode, "delivery": delivery,
                    "attempt": attempt,
                    "package_id": package_id(secret, eid, code, dur) if status == "live" else None})
    return out


@router.get("/exams/{exam_id}/challenge")
async def challenge(exam_id: str, p: Principal = Depends(require_student),
                    session: AsyncSession = Depends(get_session)):
    """Single-use nonce the FairPlay app signs to prove it starts the attempt (audit #1)."""
    exam = await get_exam_cached(get_store(), session, exam_id)
    if exam is None:
        raise HTTPException(404, "Exam not found")
    await require_member(session, exam, p.user_id)
    if await get_store().incr(f"chalrate:{p.user_id}", 60) > CHALLENGES_PER_MIN:
        raise HTTPException(429, "Too many tries, wait a minute")
    nonce = secrets.token_urlsafe(24)
    await get_store().set(f"chal:{nonce}", f"{p.user_id}:{exam_id}", ac.CHALLENGE_TTL_S)
    return {"nonce": nonce, "expires_in": ac.CHALLENGE_TTL_S}


@router.post("/exams/{exam_id}/start")
async def start(exam_id: str, body: StartIn, request: Request,
                p: Principal = Depends(require_student),
                session: AsyncSession = Depends(get_session)):
    store = get_store()
    settings = ac.settings_of(request)
    exam = await get_exam_cached(store, session, exam_id)
    if exam is None or exam.get("delivery") == "paper":  # slice 6: paper is never started
        raise HTTPException(404, "Exam not found")
    if exam["status"] != "live":
        raise HTTPException(409, "Exam is not open")
    await require_member(session, exam, p.user_id)   # audit S-03: class / LMS course only
    # Audit #1: the app proves itself with a signed, single-use challenge.
    device_id = None
    if body.client is not None:
        # Audit S-09: read-and-delete in one step, so one proof starts at most once.
        owner = await store.getdel(f"chal:{body.client.nonce}")
        if owner != f"{p.user_id}:{exam_id}" or not ac.verify(
                settings, ac.start_message(body.client.nonce, body.client.device_id),
                body.client.signature):
            raise HTTPException(403, ac.APP_PROOF_DETAIL)
        device_id = body.client.device_id
    elif settings.require_app:
        raise HTTPException(403, ac.APP_REQUIRED_DETAIL)
    lock_key, count_key = f"codelock:{p.user_id}", f"wrongcode:{p.user_id}"
    n = await take_code_try(store, lock_key, count_key, MAX_WRONG_CODES, WRONG_CODE_WINDOW_S,
                            "Too many wrong codes, wait 60 seconds")
    if not same_code(body.start_code, exam["start_code"]):
        await wrong_code(store, lock_key, count_key, n, MAX_WRONG_CODES, WRONG_CODE_LOCK_S)
        raise HTTPException(403, "Wrong start code")
    await store.delete(count_key)

    now = utcnow()
    a = await find_attempt(session, exam_id, p.user_id)
    created = False
    if a is None:
        # Audit S-07: the cached exam may be up to 120 s old. Check the real status in the
        # same transaction as the INSERT (FOR SHARE on Postgres): a close that commits
        # first makes this start fail; a close that comes after waits, then sweeps it.
        row = (await session.execute(
            select(Exam.status, Exam.duration_min).where(Exam.id == exam_id)
            .with_for_update(read=True))).first()
        if row is None or row.status != "live":
            await session.rollback()
            raise HTTPException(409, "Exam is not open")
        # T-02 (extend vs start): the duration comes from the same locked read, not the
        # cache -- an "Add 10 min" that commits first is included, one that comes after
        # waits for this INSERT (FOR SHARE) and then extends it like everyone else.
        exam = {**exam, "duration_min": row.duration_min}
        started = offline_start(body.offline_started_at, now)
        a = Attempt(id=new_id("a"), exam_id=exam_id, student_id=p.user_id, status="in_progress",
                    started_at=started,
                    deadline_at=started + timedelta(minutes=exam["duration_min"]),
                    answers={}, client_seq=-1, updated_at=now,
                    device_id=device_id, session_id=new_session_id(), entries=1,
                    client="app" if device_id else "browser")
        session.add(a)
        try:
            await session.commit()
            created = True
        except IntegrityError:  # double-click / two tabs: someone else inserted it first
            await session.rollback()
            a = await find_attempt(session, exam_id, p.user_id)
            if a is None:
                raise
    if a.status == "submitted":
        raise HTTPException(409, "Already submitted")
    if created:
        # (client "browser" -- only possible with FAIRPLAY_REQUIRE_APP=0 -- shows in the live room)
        await store.set(f"entry:{a.id}", "1", REENTRY_QUIET_S)
    else:
        await _resume(session, store, a, device_id, exam_id)
    await cache_attempt(store, a)
    return {**attempt_view(a, exam, now), "session": a.session_id}


async def _resume(session: AsyncSession, store, a: Attempt, device_id: str | None,
                  exam_id: str) -> None:
    """Start on an existing attempt (audit #3, #6): another device is refused and flagged;
    the same device gets a fresh session (the old one stops working) and is flagged as a
    re-entry, unless it is a double click."""
    if a.device_id and device_id != a.device_id:
        if await ac.once_per(store, f"secdev:{a.id}", 60):   # one flag a minute, not per try
            await ac.add_event(session, store, a.id, exam_id, "second_device",
                               f"start refused from {'device ' + device_id[:8] if device_id else 'a browser'}")
        raise HTTPException(409, ac.OTHER_DEVICE_DETAIL)
    if device_id is None:
        # Browser demo mode (FAIRPLAY_REQUIRE_APP=0) on an unbound attempt: keep the session
        # so a second tab does not break the first one.
        if a.session_id is None:
            a.session_id = new_session_id()
            await session.commit()
        return
    if await store.get(f"entry:{a.id}") and a.device_id == device_id:
        return  # double click within REENTRY_QUIET_S: same session
    first_bind = a.device_id is None
    a.device_id = device_id
    a.session_id = new_session_id()
    a.entries = (a.entries or 0) + 1
    a.client = "app"
    a.updated_at = utcnow()
    await session.commit()
    await store.set(f"entry:{a.id}", "1", REENTRY_QUIET_S)
    if first_bind:
        await ac.add_event(session, store, a.id, exam_id, "device_bound",
                           f"exam moved to device {device_id[:8]}")
    else:
        await ac.add_event(session, store, a.id, exam_id, "re_entry",
                           f"started again on the same computer (entry {a.entries})")


def _meta_of(a: Attempt) -> dict:
    return {"exam_id": a.exam_id, "session_id": a.session_id, "status": a.status}


async def _own_attempt(session: AsyncSession, aid: str, p: Principal) -> Attempt:
    a = await session.get(Attempt, aid)
    if a is None or a.student_id != p.user_id:
        raise HTTPException(404, "Attempt not found")
    return a


@router.get("/attempts/{aid}")
async def get_attempt(aid: str, request: Request, p: Principal = Depends(require_student),
                      session: AsyncSession = Depends(get_session)):
    a = await _own_attempt(session, aid, p)
    await ac.check_session(request, session, get_store(), aid, _meta_of(a))
    exam = await get_exam_cached(get_store(), session, a.exam_id)
    if exam is None:
        raise HTTPException(404, "Attempt not found")
    return attempt_view(a, exam, utcnow())


@router.put("/attempts/{aid}/answers")
async def save_answers(aid: str, body: AnswersIn, request: Request,
                       p: Principal = Depends(require_student),
                       session: AsyncSession = Depends(get_session)):
    now = utcnow()
    store = get_store()
    if await store.incr(f"ansrate:{aid}", 60) > ANSWER_WRITES_PER_MIN:
        raise HTTPException(429, "Saving too often, slow down")
    answers = await _known_answers(store, session, aid, body.answers)
    given = ac.session_header(request)
    # Audit #3: only the attempt's current app session may write.
    if given is not None:
        sess_ok = or_(Attempt.session_id.is_(None), Attempt.session_id == given)
    elif ac.settings_of(request).require_app:
        sess_ok = Attempt.session_id.is_(None)
    else:
        sess_ok = true()
    # Hot path: exactly one conditional UPDATE, no read-modify-write.
    res = await session.execute(
        update(Attempt)
        .where(Attempt.id == aid, Attempt.student_id == p.user_id,
               Attempt.status == "in_progress", Attempt.client_seq < body.client_seq,
               Attempt.deadline_at > now, sess_ok)
        .values(answers=answers, client_seq=body.client_seq, updated_at=now,
                answered=count_answered(answers)))
    await session.commit()
    if res.rowcount == 1:
        return {"saved": True, "client_seq": body.client_seq, "server_time": iso(now)}
    # Slow path: find out why nothing was updated.
    row = (await session.execute(
        select(Attempt.student_id, Attempt.status, Attempt.deadline_at, Attempt.client_seq,
               Attempt.exam_id, Attempt.session_id)
        .where(Attempt.id == aid))).first()
    if row is None or row.student_id != p.user_id:
        raise HTTPException(404, "Attempt not found")
    await ac.check_session(request, session, get_store(), aid,
                           {"exam_id": row.exam_id, "session_id": row.session_id})
    if row.status == "submitted":
        raise HTTPException(409, "Attempt already submitted")
    if now >= row.deadline_at:
        raise HTTPException(409, "Time is up")
    return {"saved": False, "client_seq": row.client_seq, "server_time": iso(now)}


async def _known_answers(store, session: AsyncSession, aid: str, answers: dict) -> dict:
    """Audit 2026-10-13 (hardening): keep only answers to the exam's own questions, so
    made-up keys can't inflate "answered" (or the stored row). Both lookups are cache hits
    on the hot path; an unknown attempt or a question-less exam is left as it is."""
    if not answers:
        return answers
    meta = await get_attempt_meta(store, session, aid)
    exam = await get_exam_cached(store, session, meta["exam_id"]) if meta else None
    ids = set((exam or {}).get("question_ids") or [])
    if not ids:
        return answers
    return {k: v for k, v in answers.items() if k in ids}


@router.post("/attempts/{aid}/heartbeat")
async def heartbeat(aid: str, body: HeartbeatIn, request: Request,
                    p: Principal = Depends(require_student),
                    session: AsyncSession = Depends(get_session)):
    store = get_store()
    meta = await get_attempt_meta(store, session, aid)  # store hit; DB only on cache miss
    if meta is None or meta["student_id"] != p.user_id:
        raise HTTPException(404, "Attempt not found")
    await ac.check_session(request, session, store, aid, meta)
    now = time.time()
    # Audit #2: body.locked is self-reported and no longer stored; the live room takes
    # the lock state from the app's signed beats (POST .../app-beat).
    await store.hset(f"hb:{meta['exam_id']}", aid,
                     json.dumps({"t": now, "cq": body.current_question}), LIVE_TTL)
    await ac.note_js_heartbeat(request, session, store, aid, meta)
    return {"ok": True, "status": meta["status"], "server_time": iso(utcnow()),
            "deadline_at": meta["deadline_at"]}


@router.post("/attempts/{aid}/events")
async def events(aid: str, body: EventsIn, request: Request,
                 p: Principal = Depends(require_student),
                 session: AsyncSession = Depends(get_session)):
    store = get_store()
    meta = await get_attempt_meta(store, session, aid)
    if meta is None or meta["student_id"] != p.user_id:
        raise HTTPException(404, "Attempt not found")
    await ac.check_session(request, session, store, aid, meta)
    if not body.events:
        return {"ok": True, "stored": 0}
    # Audit S-06: bounded per attempt, closed shortly after hand-in.
    if await store.incr(f"evrate:{aid}", 60) > EVENT_REQUESTS_PER_MIN:
        raise HTTPException(429, "Too many event reports, slow down")
    if meta["status"] == "submitted":
        sub = (await session.execute(select(Attempt.submitted_at).where(Attempt.id == aid))).scalar()
        if sub is None or utcnow() - sub > EVENTS_AFTER_SUBMIT:
            raise HTTPException(409, "Attempt already submitted")
    # Audit 2026-10-13: reserve the room first (one atomic INCRBY), so parallel batches
    # can't all read the same total and overshoot the cap.
    want = len(body.events)
    after = await store.incrby(f"evtotal:{aid}", want, LIVE_TTL)
    before = after - want
    room = MAX_EVENTS_PER_ATTEMPT - before
    if room <= 0:
        return {"ok": True, "stored": 0}
    batch = body.events[:room]
    now = utcnow()
    rows = [{"attempt_id": aid, "exam_id": meta["exam_id"], "type": ac.client_event_type(ev.type),
             "detail": (ev.detail or "")[:500],
             "client_time": (ev.client_time or None) and ev.client_time[:64],
             "server_time": now} for ev in batch]
    await session.execute(insert(IntegrityEvent), rows)  # one batched INSERT
    await session.commit()
    if before < MAX_EVENTS_PER_ATTEMPT <= after:     # exactly one batch crosses the cap
        await ac.add_event(session, store, aid, meta["exam_id"], "event_limit_reached",
                           f"more than {MAX_EVENTS_PER_ATTEMPT} events; later ones were dropped")
    exam_id = meta["exam_id"]
    await store.hincrby(f"evc:{exam_id}", aid, len(rows), LIVE_TTL)
    last = rows[-1]
    await store.hset(f"evl:{exam_id}", aid, f"{last['type']} {last['detail']}".strip(), LIVE_TTL)
    return {"ok": True, "stored": len(rows)}


@router.post("/attempts/{aid}/submit")
async def submit(aid: str, body: AnswersIn, request: Request,
                 p: Principal = Depends(require_student),
                 session: AsyncSession = Depends(get_session)):
    a = await _own_attempt(session, aid, p)
    await ac.check_session(request, session, get_store(), aid, _meta_of(a))
    exam = await session.get(Exam, a.exam_id) if a.status != "submitted" else None
    sent = await _known_answers(get_store(), session, aid, body.answers)
    for _ in range(SUBMIT_RETRIES):
        if a.status == "submitted":
            break
        now = utcnow()
        late = now > a.deadline_at + LATE_GRACE
        stored = a.answers or {}
        seen_seq = a.client_seq
        # Same rule as PUT /answers: the body wins only if it is strictly newer...
        use_body = body.client_seq > seen_seq
        # ...and only in time (audit S-02). After the deadline the graded answers are the
        # ones saved in time; anything new is kept aside for the teacher and flagged.
        changed = {k: v for k, v in sent.items() if stored.get(k) != v} if use_body else {}
        answers = sent if use_body and not late else stored
        late_answers = changed if late and changed else None
        score, pending, _ = grade_attempt(exam_questions(exam) if exam else [], answers, a.grades)
        # Audit 2026-10-13: only if no newer PUT landed since the read above (client_seq
        # unchanged); otherwise read again and redo the choice with the newer answers.
        res = await session.execute(
            update(Attempt).where(Attempt.id == aid, Attempt.status == "in_progress",
                                  Attempt.client_seq == seen_seq).values(
                status="submitted", submitted_at=now, receipt=new_receipt(), score=score,
                needs_grading=pending,
                answers=answers, client_seq=max(body.client_seq, seen_seq),
                answered=count_answered(answers),
                late=late, late_answers=late_answers, auto_submitted=False, updated_at=now))
        await session.commit()
        if res.rowcount == 1 and late:
            mins = int((now - a.deadline_at).total_seconds() // 60)
            await ac.add_event(session, get_store(), aid, a.exam_id, "late_submission",
                               f"handed in {mins} min after time"
                               + (f"; {len(changed)} answer(s) sent after time were not graded" if changed else ""))
        # rowcount 0: a concurrent submit/close won (refresh returns its receipt), or a
        # newer PUT moved client_seq (loop again with it).
        await session.refresh(a)
        if res.rowcount == 1:
            break
    if a.status != "submitted":
        raise HTTPException(409, "Answers changed while handing in, try again")
    await cache_attempt(get_store(), a)
    return {"status": "submitted", "submitted_at": iso(a.submitted_at), "receipt": a.receipt}


@router.post("/attempts/{aid}/app-beat")
async def app_beat(aid: str, body: AppBeatIn, request: Request,
                   p: Principal = Depends(require_student),
                   session: AsyncSession = Depends(get_session)):
    """Signed signal from the Mac app itself (audit #2, #6): the only source of "locked"
    in the live room. A gap in these beats is flagged (app killed / Mac asleep)."""
    store = get_store()
    settings = ac.settings_of(request)
    meta = await get_attempt_meta(store, session, aid)
    if meta is None or meta["student_id"] != p.user_id:
        raise HTTPException(404, "Attempt not found")
    if meta.get("session_id") != body.session or (
            meta.get("device_id") and meta["device_id"] != body.device_id):
        raise HTTPException(409, ac.SESSION_DETAIL)
    msg = ac.beat_message(aid, body.session, body.device_id, body.ts, body.locked, body.checks)
    if not ac.verify(settings, msg, body.signature):
        raise HTTPException(403, ac.APP_PROOF_DETAIL)
    raw = await store.get(f"abt:{aid}")
    last = json.loads(raw) if raw else None
    # Replayed or reordered beat. Per session (audit S-13): a new Mac / new session with
    # a slower clock is not rejected because of the old session's timestamps.
    if last and last.get("session", body.session) == body.session and body.ts <= last["ts"]:
        raise HTTPException(409, "Old app signal")
    now = time.time()
    exam_id = meta["exam_id"]
    # Audit M-02: a Mac whose login session is in the background (fast user switching,
    # login window) is not locked, whatever the app says.
    checks = ac.parse_checks(body.checks)
    locked = body.locked and checks.get("console") != "0"
    if checks.get("console") == "0" and meta["status"] == "in_progress" \
            and await ac.once_per(store, f"offcon:{aid}", ac.FLAG_REPEAT_S):
        await ac.add_event(session, store, aid, exam_id, "off_console",
                           "this Mac switched to another user or the login window")
    if checks.get("secinput") == "1" and meta["status"] == "in_progress" \
            and await ac.once_per(store, f"secin:{aid}", ac.FLAG_REPEAT_S):
        await ac.add_event(session, store, aid, exam_id, "secure_input_other_app",
                           "another app holds secure keyboard input (it may see keys)")
    if last and meta["status"] == "in_progress" and now - last["t"] > ac.APP_GAP_S:
        await ac.add_event(session, store, aid, exam_id, "app_gap",
                           f"no signal from the app for {int(now - last['t'])} s")
    if last and last.get("locked") and not locked and meta["status"] == "in_progress":
        await ac.add_event(session, store, aid, exam_id, "unlocked_in_exam",
                           "the app reports it is no longer locked")
    entry = {"t": now, "ts": body.ts, "locked": locked, "checks": body.checks,
             "session": body.session}
    await store.set(f"abt:{aid}", json.dumps(entry), LIVE_TTL)
    await store.hset(f"ab:{exam_id}", aid, json.dumps(entry), LIVE_TTL)
    return {"ok": True, "status": meta["status"], "server_time": iso(utcnow()),
            "deadline_at": meta["deadline_at"]}


@router.get("/attempts/{aid}/handin")
async def handin_status(aid: str, request: Request, nonce: str,
                        p: Principal = Depends(require_student),
                        session: AsyncSession = Depends(get_session)):
    """Audit M-04: may the app unlock? Always 200 with a signed answer, so a man in the
    middle can't fake "submitted" / "gone" (he lacks the app key). status: "submitted",
    "in_progress", "gone" (no such attempt for this student), "moved" (another session).
    proofs = one HMAC per app key over "handin|<aid>|<session>|<status>|<nonce>"."""
    if not ac.AppSafe.token(nonce, 8, 128):
        raise HTTPException(422, "bad nonce")
    sess = (request.headers.get(ac.SESSION_HEADER) or "").strip()
    # Audit 2026-10-13: the session goes into the signed message; no separators in it.
    if sess and not ac.AppSafe.token(sess, 1, 128):
        raise HTTPException(422, "bad session")
    a = await session.get(Attempt, aid)
    if a is None or a.student_id != p.user_id:
        status = "gone"
    elif a.session_id and a.session_id != sess:
        status = "moved"
    else:
        status = "submitted" if a.status == "submitted" else "in_progress"
    msg = ac.handin_message(aid, sess, status, nonce)
    return {"status": status, "proofs": [ac.sign(k, msg) for k in ac.settings_of(request).app_keys]}


@router.post("/attempts/{aid}/exit-code")
async def exit_code(aid: str, body: ExitCodeIn, request: Request,
                    p: Principal = Depends(require_student),
                    session: AsyncSession = Depends(get_session)):
    """Invigilator emergency exit (audit #5): the code lives on the server only, per exam,
    with an attempt limit. The app unlocks only after {"ok": true}."""
    store = get_store()
    a = await _own_attempt(session, aid, p)
    await ac.check_session(request, session, store, aid, _meta_of(a))
    lock_key, count_key = f"exitlock:{aid}", f"exitwrong:{aid}"
    # Audit 2026-10-13: the try is reserved before the (awaited) DB read, so parallel
    # guesses can't all slip past the lock check.
    n = await take_code_try(store, lock_key, count_key, MAX_WRONG_EXIT, 600,
                            "Too many wrong codes, wait 5 minutes")
    exam = await session.get(Exam, a.exam_id)
    code = (exam.invigilator_code or "") if exam else ""
    if not same_code(body.code, code):      # audit S-12: compare bytes
        await wrong_code(store, lock_key, count_key, n, MAX_WRONG_EXIT, WRONG_EXIT_LOCK_S)
        await ac.add_event(session, store, aid, a.exam_id, "emergency_exit_rejected",
                           f"wrong invigilator code ({n})")
        raise HTTPException(403, "Wrong invigilator code")
    await store.delete(count_key)
    await ac.add_event(session, store, aid, a.exam_id, "emergency_exit",
                       "invigilator code accepted")
    out: dict = {"ok": True}
    if body.nonce:
        # Audit 2026-10-13: signed like the hand-in status, so the app unlocks only on a
        # reply the server really sent (a man in the middle lacks the app key).
        msg = ac.exit_message(aid, ac.session_header(request) or "", body.nonce)
        out.update(nonce=body.nonce,
                   proofs=[ac.sign(k, msg) for k in ac.settings_of(request).app_keys])
    return out


@router.post("/attempts/{aid}/offline-key")
async def offline_key(aid: str, body: OfflineKeyIn, request: Request,
                      p: Principal = Depends(require_student),
                      session: AsyncSession = Depends(get_session)):
    """Key for the offline invigilator exit (OfflineExit.swift), handed to the app once
    per app session. The page never sees it. A second request for the same session is
    refused and flagged: someone other than the app is asking."""
    settings = ac.settings_of(request)
    store = get_store()
    a = await _own_attempt(session, aid, p)
    await ac.check_session(request, session, store, aid, _meta_of(a))
    if a.status != "in_progress":
        raise HTTPException(409, "Attempt already submitted")
    if body.session != a.session_id or (a.device_id and a.device_id != body.device_id):
        raise HTTPException(409, ac.SESSION_DETAIL)
    msg = ac.offline_key_message(aid, body.session, body.device_id, body.ts)
    if not ac.verify(settings, msg, body.signature):
        raise HTTPException(403, ac.APP_PROOF_DETAIL)
    if await store.incr(f"offkey:{aid}:{body.session}", LIVE_TTL) > 1:
        await ac.add_event(session, store, aid, a.exam_id, "offline_key_requested_twice",
                           "the offline exit key for this session was asked for again")
        raise HTTPException(409, "Already issued for this session")
    return {"key": ac.offline_key(settings.secret, aid, body.session).hex()}


# ---------------------------------------------------------------- slice 2: grades
@router.get("/grades")
async def my_grades(p: Principal = Depends(require_student),
                    session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Exam.id, Exam.title, Exam.course, Exam.total_points, Exam.grades_released,
               Attempt.submitted_at, Attempt.score)
        .join(Attempt, Attempt.exam_id == Exam.id)
        .where(Attempt.student_id == p.user_id, Attempt.status == "submitted")
        .order_by(Attempt.submitted_at.desc(), Exam.id))).all()
    out = []
    for eid, title, course, total, released, sub, score in rows:
        released = bool(released)
        out.append({"exam_id": eid, "title": title, "course": course or "",
                    "submitted_at": iso(sub), "released": released,
                    "score": num(score or 0.0) if released else None,
                    "total_points": num(total),
                    "percent": percent(score or 0.0, total) if released else None})
    return out


def _correct_answer(q: dict):
    t = q.get("type")
    if t == "mcq":
        return q.get("correct_index")
    if t == "numeric":
        return q.get("answer_number")
    if t == "short":
        acc = q.get("accepted_answers") or []
        return acc[0] if acc else None
    return None


@router.get("/grades/{exam_id}")
async def my_grade_detail(exam_id: str, p: Principal = Depends(require_student),
                          session: AsyncSession = Depends(get_session)):
    a = await find_attempt(session, exam_id, p.user_id)
    if a is None or a.status != "submitted":
        raise HTTPException(404, "No submitted attempt for this exam")
    e = await session.get(Exam, exam_id)
    if e is None:
        raise HTTPException(404, "No submitted attempt for this exam")
    if not e.grades_released:
        raise HTTPException(403, "Grades not released yet")
    questions = exam_questions(e)
    score, _, rows = grade_attempt(questions, a.answers, a.grades)
    # Class average from the stored per-attempt score (kept in sync by every grade change):
    # one aggregate instead of re-grading every attempt per student request.
    avg = (await session.execute(
        select(func.avg(Attempt.score))
        .where(Attempt.exam_id == exam_id, Attempt.status == "submitted"))).scalar()
    out_q = []
    for i, (q, r) in enumerate(zip(questions, rows)):
        pts = float(r["points_awarded"])
        mx = q.get("points", 1)
        if not is_answered(r["given"]):
            status = "unanswered"
        elif abs(pts - mx) < 1e-9:
            status = "correct"
        elif pts > 0:
            status = "partial"
        else:
            status = "wrong"
        out_q.append({
            "question_id": q["id"], "index": i, "type": q["type"], "prompt": q["prompt"],
            "options": list(q.get("options") or []), "points": mx,
            "points_awarded": r["points_awarded"], "status": status, "given": r["given"],
            "correct_answer": _correct_answer(q),
            "tolerance": q.get("tolerance") if q.get("type") == "numeric" else None,
            "comment": r["comment"], "rubric": [dict(x) for x in q.get("rubric") or []],
            "images": [dict(x) for x in q.get("images") or []],  # slice 6
            "rubric_scores": r["rubric_scores"],
        })
        if r.get("image"):  # slice 6 D: the student's scanned answer (paper exams)
            out_q[-1]["image"] = r["image"]
    return {
        "exam_id": e.id, "title": e.title, "course": e.course or "",
        "submitted_at": iso(a.submitted_at), "receipt": a.receipt, "score": num(score),
        "total_points": num(e.total_points), "percent": percent(score, e.total_points),
        "class_average_percent": percent(float(avg), e.total_points) if avg is not None else None,
        "overall_comment": a.overall_comment or "", "questions": out_q,
    }
