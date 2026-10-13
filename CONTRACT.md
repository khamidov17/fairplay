# Proctora slice 1 — shared contract

Everything in this file is binding for `server/`, `web/` and `mac/`. Do not invent
endpoints or fields outside it; if something is missing, add it here first.

## Layout

```
proctora/
  CONTRACT.md
  server/            FastAPI app (Python 3.11+, SQLAlchemy 2 async, asyncpg/aiosqlite, redis)
    app/main.py      `uvicorn app.main:app` from server/
    app/...
    static/          built web UI goes here (copied from web/dist)
    tests/           pytest, uses fastapi TestClient (httpx)
    requirements.txt
  web/               React 18 + TypeScript + Vite, react-router
  mac/               Swift app (built on the Mac)
```

## Scale requirements (binding)

Target: many universities at once — 10,000+ students writing concurrently across
hundreds of live exams. Every student sends a heartbeat every 5 s and answer
saves every few seconds, so the hot path is ~3,000+ writes/s at peak.

- **Stateless API servers.** No in-process state that matters (sessions, rate
  limits, live state) — any request can hit any instance. Run N uvicorn workers /
  N containers behind a load balancer.
- **Database: PostgreSQL in production** via SQLAlchemy 2.0 async (asyncpg).
  The SAME code must also run on SQLite (aiosqlite) for local single-laptop dev.
  Select by env `DATABASE_URL` (default `sqlite+aiosqlite:///./data/proctora.db`).
  Proper indexes on every foreign key and on (exam_id, student_id).
  Connection pool sized via env.
- **Hot path off the database.** Heartbeats and live-room state go to **Redis**
  (env `REDIS_URL`); if `REDIS_URL` is unset, fall back to an in-memory store
  (dev only, single process). Postgres only gets durable writes: answers,
  submits, integrity events (batched inserts).
- **Answer saves are idempotent upserts** keyed by (attempt_id) with client_seq
  compare — one row update per save, never read-modify-write races.
- **Rate limits and wrong-code counters** in Redis (memory fallback in dev).
- **Exam content cached** (questions without keys) per exam in Redis/memory,
  invalidated on edit/open.
- **Live room reads** come from Redis, never a full DB scan per poll.
- **All endpoints async**, no blocking calls.
- **Load test** (`server/loadtest/`): async script simulating N students
  (default 2,000) across M exams (default 50): login, start, heartbeat every 5 s,
  answer save every 3 s, submit. Reports p50/p95/p99 latency and error rate.
- **Deployment files**: `docker-compose.yml` with postgres, redis and the API
  (4 workers) + a 2-replica example; `Dockerfile`.

## Serving

- Server listens on `http://127.0.0.1:8000`.
- `GET /api/...` = JSON API.
- Web UI is ONE Vite app served by the server from `server/static`:
  - `/teacher/*` → teacher UI, `/student/*` → student UI (SPA fallback: any non-API, non-file path returns `static/index.html`).
  - Vite `base: "/"`. Client routes: `/teacher/login`, `/teacher`, `/teacher/exams/new`, `/teacher/exams/:id`, `/student/login`, `/student`, `/student/exam/:attemptId`, `/student/done/:attemptId`. `/` redirects to `/teacher`.
- CORS: allow all origins (dev).

## Auth

- `POST /api/auth/login` body `{"username": str, "password": str}` → `200 {"token": str, "role": "teacher"|"student", "name": str}`; wrong → `401 {"detail": "Invalid username or password"}`.
- All other endpoints need header `Authorization: Bearer <token>`. Missing/invalid → 401. Wrong role → 403.
- Tokens: signed and stateless (HMAC-SHA256 over user id + role + expiry, secret from env `PROCTORA_SECRET`, 12 h expiry) so no DB lookup per request.
- Seed users (created on startup if absent; passwords hashed with hashlib.pbkdf2_hmac sha256 + salt):
  - teacher `teacher` / `teacher123`, name "Demo Teacher"
  - students `alice` / `alice123` (name "Alice Chen"), `bob` / `bob123` (name "Bob Karimov"), `carol` / `carol123` (name "Carol Wang")
- `GET /api/me` → `{"username","name","role"}`.

## Data shapes

Question (teacher view):
```json
{"id": "q_xxx", "type": "mcq" | "numeric" | "short",
 "prompt": "text", "points": 1,
 "options": ["A text", "B text"],       // mcq only, 2-8 items, else []
 "correct_index": 1,                      // mcq only, else null
 "answer_number": 3.14, "tolerance": 0.01,// numeric only, else null
 "accepted_answers": ["paris"]            // short only, else []
}
```
Question (student view) = same minus `correct_index`, `answer_number`, `tolerance`, `accepted_answers`.

Exam (teacher view):
```json
{"id": "e_xxx", "title": "str", "duration_min": 30,
 "status": "draft" | "live" | "closed",
 "start_code": "4729" | null,             // set while live
 "questions": [Question...], "created_at": "ISO8601Z",
 "question_count": 5, "total_points": 10}
```

Attempt status: `"in_progress" | "submitted"`.

Answer values: mcq → integer option index; numeric → number; short → string. Missing = not answered.

## Teacher API (role teacher)

- `GET /api/exams` → `[Exam...]` newest first (questions included).
- `POST /api/exams` body `{"title","duration_min","questions":[Question without id]}` → `201 Exam` (status draft, ids generated). Validation → 422.
- `GET /api/exams/{id}` → Exam. 404 if missing.
- `PUT /api/exams/{id}` same body as POST → Exam. Only allowed when status draft, else `409 {"detail":"Exam is not a draft"}`.
- `DELETE /api/exams/{id}` → 204 (draft or closed only, else 409).
- `POST /api/exams/{id}/open` → Exam with status live and a new random 4-digit `start_code` (no leading-zero issues: string). Allowed from draft or closed.
- `POST /api/exams/{id}/close` → Exam status closed, start_code null. All in_progress attempts become submitted (auto) with `auto_submitted: true`.
- `GET /api/exams/{id}/live` →
```json
{"exam_id","status","start_code","duration_min",
 "students":[{"attempt_id","username","name","status",
   "started_at","submitted_at","deadline_at",
   "answered":3,"question_count":5,"current_question":2,
   "locked":true,"last_heartbeat_at":"ISO"|null,"online":true,
   "event_count":4,"last_event":"blocked_shortcut Cmd-key48"|null}]}
```
  `online` = last heartbeat within 15 s. Students listed = those with an attempt.
- `GET /api/exams/{id}/results` →
```json
{"exam_id","title","total_points","average_percent": 72.5,
 "attempts":[{"attempt_id","username","name","status","auto_submitted",
   "score":7,"total_points":10,"percent":70.0,"submitted_at",
   "receipt":"K7Q2-M9XD",
   "answers":[{"question_id","given":<value|null>,"correct":true,"points_awarded":1}],
   "events":[{"type","detail","client_time","server_time"}]}],
 "questions":[{"question_id","prompt","type","percent_correct": 50.0}]}
```

## Student API (role student)

- `GET /api/student/exams` → live exams plus exams this student has an attempt in:
```json
[{"id","title","duration_min","question_count","status",
  "attempt": null | {"attempt_id","status","submitted_at","receipt"}}]
```
- `POST /api/student/exams/{id}/start` body `{"start_code": "4729"}`:
  - exam not live → 409 `{"detail":"Exam is not open"}`; wrong code → 403 `{"detail":"Wrong start code"}` (count failures per student, after 5 wrong in a row → 429 `{"detail":"Too many wrong codes, wait 60 seconds"}` for 60 s).
  - Already has in_progress attempt → return it (resume). Already submitted → 409 `{"detail":"Already submitted"}`.
  - → `200 {"attempt_id","exam_id","title","duration_min","started_at","deadline_at","server_time","questions":[Question student view], "answers":{"q_id": value}, "status"}`
- `GET /api/student/attempts/{aid}` → same shape as start response (resume after reload). 404 if not this student's.
- `PUT /api/student/attempts/{aid}/answers` body `{"answers":{"q_id":value}, "client_seq": int}` → `{"saved": true, "client_seq": int, "server_time"}`. Ignore (but still 200, `saved:false`) if client_seq <= stored seq. After deadline or submitted → 409.
- `POST /api/student/attempts/{aid}/heartbeat` body `{"current_question": int, "locked": bool}` → `{"ok": true, "status", "server_time", "deadline_at"}`. (If exam was closed, status comes back submitted so the client finishes.)
- `POST /api/student/attempts/{aid}/events` body `{"events":[{"type": str, "detail": str, "client_time": "ISO"}]}` → `{"ok": true, "stored": n}`. Allowed even after submit.
- `POST /api/student/attempts/{aid}/submit` body `{"answers":{...}, "client_seq": int}` → `{"status":"submitted","submitted_at","receipt": "XXXX-XXXX"}`. Idempotent: second call returns same receipt. Late (after deadline + 60 s grace) still accepted but marked `late` internally (no field needed in slice 1).
- Scores are NOT returned to students in slice 1.

## Grading

- mcq: given == correct_index → full points.
- numeric: abs(given - answer_number) <= tolerance (tolerance default 0) → full points.
- short: normalize (strip, lowercase, collapse spaces) and compare with each accepted answer normalized → full points.
- Otherwise 0. Graded at submit time and recomputed on results read.

## Native bridge (student UI inside the Mac app)

The Mac app loads `http://127.0.0.1:8000/student/login` in a WKWebView.

- Detect: `window.webkit?.messageHandlers?.proctora` exists → running inside the Mac app.
- JS → native: `window.webkit.messageHandlers.proctora.postMessage(msg)` with:
  - `{"action":"lock"}` — call right after a successful start, before showing questions.
  - `{"action":"unlock"}` — call after submit succeeded (and when heartbeat says submitted).
  - `{"action":"hello"}` — on page load; native answers with its state.
- Native → JS: `window.dispatchEvent(new CustomEvent("proctora-native", {detail}))` with detail:
  - `{"type":"state","locked":bool,"trusted":bool,"version":"0.3"}`
  - `{"type":"locked"}` / `{"type":"lockFailed","reason":"accessibility"}` / `{"type":"unlocked"}`
  - `{"type":"event","event":{"type":"blocked_shortcut","detail":"Cmd-key48","client_time":"ISO"}}` — JS forwards these to `/events` and includes `locked` in heartbeats.
- Outside the Mac app (plain browser), the student UI still works but shows a yellow banner "Not in locked mode — use the Proctora Mac app for real exams" and sends `locked:false`.

## Student UI behaviour (offline-first)

- Answers saved to `localStorage["proctora:attempt:u<account id>:<aid>"]` = `{answers, client_seq, synced, owner}` (audit 2026-10-13: one account never loads another's copy; sign-out drops copies the server already has and old unowned offline copies) on every change (client_seq++), synced via PUT `/answers` debounced 1 s and retried every 3 s while unsynced. Header shows "All answers saved" (synced) / "Saved on this computer" (pending).
- Heartbeat every 5 s.
- Timer counts down to `deadline_at` using server_time offset. At 0 → auto submit.
- Submit: confirm screen listing unanswered count → POST submit → native unlock → `/student/done/:aid` shows receipt.

---

# Slice 2 — design look, essays, grading, gradebook, student grades

Everything above stays valid unless changed here. Slice 2 additions are binding.

## Exam fields (added)

Exam (teacher view) gains:
```json
{"course": "MATH 201",            // str, default "", max 64
 "room": "AB 1087",               // str, default "", max 64
 "scheduled_at": "ISO8601Z"|null, // planned start, informational only
 "shuffle_questions": false,      // per-student order, deterministic per attempt
 "grades_released": false,
 "released_at": "ISO8601Z"|null,
 "closed_at": "ISO8601Z"|null,
 "pending_grading": 0}            // essay answers in submitted attempts not yet graded
```
POST/PUT body accepts `course`, `room`, `scheduled_at`, `shuffle_questions` (all optional).
Existing rows: defaults above (add columns; create_all on a fresh DB is fine, dev DB may be wiped).

## Question types (added)

- `"essay"`: `{"type":"essay","prompt","points","rubric":[{"label":"Two clear reasons","points":10}],"min_words":null|int,"max_words":null|int}`.
  `rubric` may be `[]`; if non-empty its points must sum exactly to `points` (else 422). Essays are never auto-graded.
  All other type-specific fields null/[] for essay; `rubric`/`min_words`/`max_words` are `[]`/null for non-essay types.
- True/false is NOT a new type: the UI creates an `mcq` with options `["True","False"]`.
- Student view of essay includes `rubric`, `min_words`, `max_words` (students see how it is graded).
- Essay answer value: string (plain text, max 50,000 chars — raise the slice-1 10,000 limit for essays only, or globally to 50,000).

## Grading model

Each submitted attempt has teacher grades per question: `{"points": num, "comment": str, "rubric_scores": [num]|null, "graded_at": ISO}` (stored as JSON on the attempt, column `grades`), plus `overall_comment` (str).

Per question:
- If a teacher grade exists → its points (an override; allowed on ANY question type).
- Else essay → if answer missing/blank: 0 and counts as graded; else **pending** (0 for now, not graded).
- Else auto grade (slice 1 rules).

Attempt: `score` = sum; `needs_grading` = number of pending essay answers; `fully_graded` = needs_grading == 0.
Percent = score / total_points * 100 rounded to 1 decimal (0 if total 0).

## Teacher API (added / changed)

- `GET /api/exams` items and `GET /api/exams/{id}` include the new fields.
- `POST /api/exams/{id}/extend` body `{"minutes": 1..120}` → Exam. Only when live (else 409). Adds minutes to `duration_min` AND to `deadline_at` of every in_progress attempt; heartbeats then return the new deadline (invalidate any cached attempt metadata).
- `GET /api/exams/{id}/live` gains:
  - each student: `"needs_look": bool` (has open flags)
  - `"flags": [{"event_id": int, "attempt_id", "username", "name", "type", "detail", "server_time"}]` — newest first, max 50, only `review == "open"` and type NOT in {"lock_started","lock_ended"}.
  - `"counts": {"writing": n, "submitted": n, "offline": n, "needs_look": n}` (offline = in_progress and not online).
- `POST /api/exams/{id}/events/{event_id}/review` body `{"status": "dismissed"|"noted", "note": "str (optional, max 500)"}` → `{"event_id","review","note"}`. 404 if event not in this exam.
- `GET /api/exams/{id}/results` changes:
  - top level adds `"course","grades_released","released_at","median_percent","pending_grading","submitted_count","attempt_count","flags_total","flags_open"`, and `"score_buckets": [{"label":"0–9","count":n}, … 10 buckets of percent, last "90–100"]` over submitted attempts.
  - each attempt adds `"needs_grading": n, "overall_comment": str, "flag_count": n`.
  - each answer adds `"max_points", "manual": bool` (teacher grade exists), `"pending": bool`, `"comment": str`, `"rubric_scores": [num]|null`. For pending essays `correct` is false and `points_awarded` 0. `correct` = points_awarded == max_points.
  - each event adds `"event_id","review","note"`.
  - questions add `"points"` and `"avg_points"`; `percent_correct` for essays = average awarded / points * 100 over graded essay answers (null if none graded).
- `GET /api/exams/{id}/grading` → grading queue for one exam:
```json
{"exam_id","title","course","status","grades_released",
 "questions":[{"question_id","index":0,"type","prompt","points","rubric":[...],
   "to_grade": n,     // essay: pending count; short: wrong auto answers without a teacher grade; others 0
   "graded": n}],     // answers of this question with a teacher grade
 "items":[{"attempt_id","anon_id":"#A7F2","username","name","question_id",
   "answer": <value|null>, "word_count": n|null, "auto_points": num|null,
   "grade": null|{"points","comment","rubric_scores","graded_at"},
   "flag_count": n}]}
```
  Items = for every submitted attempt: each **essay** answer (answered or not) and each **short** answer that was auto-graded wrong. Ordered by question index, then attempt started_at. `anon_id` = "#" + first 4 chars of sha256(attempt_id) uppercased.
- `PUT /api/exams/{id}/attempts/{aid}/grades/{qid}` body `{"points": num, "comment": "str", "rubric_scores": [num]|null}` → `{"attempt_id","question_id","grade":{...},"score","total_points","percent","needs_grading"}`.
  Rules: attempt must be submitted (409 otherwise); 0 <= points <= question points (422); if rubric_scores given: same length as rubric, each 0..criterion points, and `points` must equal their sum (422).
- `DELETE /api/exams/{id}/attempts/{aid}/grades/{qid}` → same response shape with `"grade": null` (back to automatic).
- `PUT /api/exams/{id}/attempts/{aid}/comment` body `{"comment": "str max 2000"}` → `{"attempt_id","overall_comment"}`.
- `POST /api/exams/{id}/release` body `{"force": false}` → Exam. Exam must be closed (409 `{"detail":"Close the exam first"}`). If pending_grading > 0 and not force → 409 `{"detail":"<n> answers still need grading"}`.
- `POST /api/exams/{id}/unrelease` → Exam with grades_released false.
- `GET /api/grading` → `[{"exam_id","title","course","status","pending": n,"submitted": n,"grades_released"}]` for this teacher's exams that have submitted attempts, newest first.
- `GET /api/gradebook?course=` →
```json
{"courses":["MATH 201", ...],
 "exams":[{"exam_id","title","course","total_points","status","grades_released"}],   // oldest first
 "students":[{"username","name","average_percent": num|null,
   "cells":{"<exam_id>":{"state":"graded"|"needs_grading"|"in_progress"|"missing"|"not_taken",
                         "score": num|null,"percent": num|null}}}]}
```
  Students = all users with role student (sorted by name). State: submitted & fully graded → graded; submitted & pending → needs_grading (score = current partial); in_progress → in_progress; no attempt & exam closed → missing; no attempt & exam not closed → not_taken. average_percent over graded cells only.
- `GET /api/gradebook.csv?course=` → `text/csv` with header `Student,Username,<exam titles...>,Average %`, cells = percent or state word. Auth via Bearer like everything else.

## Student API (added / changed)

- `GET /api/student/exams` items add `"course","room","scheduled_at"`; `attempt` adds `"released": bool, "percent": num|null` (percent only when released).
- Start/attempt view: questions in per-student shuffled order when `shuffle_questions` (seeded by attempt_id, stable across reloads); essay questions include `rubric`, `min_words`, `max_words`. Response adds `"course"`.
- `GET /api/student/grades` → submitted attempts of this student, newest first:
```json
[{"exam_id","title","course","submitted_at","released": bool,
  "score": num|null,"total_points","percent": num|null}]   // score/percent null unless released
```
- `GET /api/student/grades/{exam_id}` → 403 `{"detail":"Grades not released yet"}` unless released; 404 if no submitted attempt. Else:
```json
{"exam_id","title","course","submitted_at","receipt","score","total_points","percent",
 "class_average_percent","overall_comment",
 "questions":[{"question_id","index","type","prompt","options":[...],"points","points_awarded",
   "status":"correct"|"partial"|"wrong"|"unanswered",
   "given": <value|null>,
   "correct_answer": <mcq index | number | first accepted string | null for essay>,
   "tolerance": num|null,
   "comment": str, "rubric":[...], "rubric_scores":[num]|null}]}
```
  Questions in the exam's original order.

## Web UI (slice 2)

Look follows the design canvas "Exam App Screens": font **Plus Jakarta Sans** (self-hosted via `@fontsource/plus-jakarta-sans`, NO Google Fonts — must work in China) and **JetBrains Mono** (`@fontsource/jetbrains-mono`) for codes/timers. Tokens (in `web/src/styles/tokens.css`): ground #F5F6F9, surface #FFFFFF, border #E3E6EE, divider #F0F2F6, text #14161F, text-2 #3A3F4E, muted #565B6B, primary #2F4BD8 (hover #1F35A8, tint #E9EDFC, tint-2 #F2F4FE), success #12704A (tint #E6F4EC), warn #B4470B (dark #8A3508, tint #FDEEE3, warn-border #F3C9AC, warn-bg #FFF8F3), dark #14161F / #262938 / #4A4E60, radius 10 (controls) / 14–16 (cards), touch targets ≥ 44 px.

Teacher routes: `/teacher` (home), `/teacher/exams` (list), `/teacher/exams/new`, `/teacher/exams/:id` (tabs: Live room / Results / Questions; edit when draft), `/teacher/exams/:id/edit`, `/teacher/exams/:id/grading`, `/teacher/grading`, `/teacher/gradebook`. Teacher layout = left sidebar nav (Home, Exams, Grading, Gradebook) like the design; nothing that does not work is shown (no Canvas/Homework/Word upload links yet).

Student routes: `/student` (home), `/student/exams`, `/student/exams/:examId/checkin` (check-in + start code), `/student/exam/:attemptId`, `/student/done/:attemptId`, `/student/grades`, `/student/grades/:examId`. Student layout = top nav (Home, Exams, Grades) except during an exam (locked layout like design "Taking an exam").

---

# Slice 3 — external-website exams (SEB-style) + lighter visual style

## Exam fields (added)

```json
{"mode": "questions" | "external",       // default "questions"
 "external_url": "https://..." | null,     // required when external; http(s) only, max 2000
 "allowed_domains": ["canvas.dku.edu"],    // hostnames, max 30, lowercase, no scheme/path; the external_url host is always allowed
 "finish_url_contains": "formResponse" | null}  // optional, max 200: when the external page URL contains it, the app finishes automatically
```
- POST/PUT accept these. When mode is "external": `external_url` required (422 otherwise), `questions` must be `[]` (422 otherwise), question_count 0, total_points 0. When mode is "questions": at least one question, and the three external fields are stored as null/[].
- A domain entry allows that host and all its subdomains (`dku.edu` allows `canvas.dku.edu`). Entries are validated: letters, digits, dots, hyphens; at least one dot or "localhost".

## Behaviour of external exams

- Student exam list items add `"mode"`.
- Start/attempt view adds `"mode"`, `"external_url"`, `"allowed_domains"` (always including the external_url host), `"finish_url_contains"`; `questions: []`, `answers: {}`.
- Submit works as usual with `{"answers": {}, "client_seq": 0}` (receipt issued). Heartbeat/events unchanged.
- Results: attempts of external exams have `"score": null, "percent": null`, `answers: []`; top-level `average_percent`/`median_percent` null, `score_buckets` all zero. `needs_grading` 0.
- Release/unrelease → 409 `{"detail":"This exam is graded on the other website"}`.
- Gradebook: external exams are listed; a submitted attempt's cell state is `"submitted"` (new state) with score/percent null; excluded from averages. CSV shows "submitted".
- Student grades list: external exams excluded. Feedback → 404.

## Native bridge (added)

JS → native:
```json
{"action": "openExternal", "attempt_id": "a_x", "url": "https://...", "allowed_domains": ["..."],
 "finish_url_contains": "..." | null, "deadline_at": "ISO", "server_time": "ISO",
 "title": "MATH 201 Midterm", "token": "<student bearer token>"}
```
Native (must already be locked; if not, it locks first and fails with `lockFailed` if it can't):
- Shows a native top strip above the web view: exam title, countdown to deadline_at (corrected by server_time offset), and a "Finish exam" button. The strip is light (white, hairline bottom border), not black.
- Navigates the web view to `url`. Navigation policy while external: allow the exam server, `allowed_domains` (and subdomains); block everything else and record `blocked_navigation` events. Popups/new windows: open in the same view if allowed, else block.
- While external, the JS page is gone, so native itself sends `POST /api/student/attempts/{aid}/heartbeat` `{"current_question":0,"locked":true}` every 5 s and forwards integrity events to `/api/student/attempts/{aid}/events` (batched), using `Authorization: Bearer <token>`. If a heartbeat returns `status: "submitted"` (teacher closed the exam), finish.
- Finish happens on: "Finish exam" pressed and confirmed (native sheet: "Finish the exam? Make sure you submitted on the website first."), page URL containing `finish_url_contains` (after a 2 s delay so the site's confirmation is shown), or deadline reached. Finishing = hide the strip, restore the normal navigation policy, and load `<server>/student/external-done/<attempt_id>`; that web page submits, calls `unlock`, and shows the receipt page. Native stays locked until that `unlock` arrives.

## Web routes (added)

- `/student/external/:attemptId` — ExternalExamPage: loads the attempt; outside the Mac app shows "This exam opens only in the Proctora app"; inside: requestLock (lockFailed screen with Retry as in ExamTakingPage), then `openExternal`.
- `/student/external-done/:attemptId` — submits (idempotent), `bridge.unlock()`, navigates to `/student/done/:attemptId`.
- Check-in/resume navigate to `/student/external/:aid` when the attempt's mode is external.
- Teacher exam setup: "Where do students answer?" — "Questions in Proctora" or "On another website" (URL, allowed websites list, optional "Finish automatically when the page address contains").

## Visual style (slice 3) — replaces the dark look

- No black/near-black surfaces anywhere: no dark top bars, banners, live-room header or check-in page. Ground `#F3F5F7`, surfaces white, text ink `#1B2533` (dark slate, used for text only), secondary `#4A5568`, muted `#6B7686`, hairlines `#E2E6EC`. Primary blue stays `#2F4BD8` with tint `#EEF1FD`. Attention/amber `#B4570B` (tint `#FDF1E6`). Success `#16794C` (tint `#E8F5EE`).
- Type: UI in Plus Jakarta Sans; **question text, essay text and the student's written answers in Source Serif 4** (self-hosted `@fontsource/source-serif-4`) for an exam-paper feel. Timers and start codes use Plus Jakarta Sans with `font-variant-numeric: tabular-nums` (no monospace).
- Student exam screen: a white "paper" sheet (max ~760 px) centered on the ground; slim white top bar (lock state pill, course + title, save status, timer pill that turns amber under 5 min); bottom bar with Previous · "Question n of N" (opens the question navigator popover/panel) · Next / Review. No permanent right rail.

---

# Slice 5 — LMS sync (Canvas first; Moodle, Blackboard, Brightspace via the same interface)

Goal: a teacher connects their LMS course, imports the student roster into FairPlay, links a FairPlay exam to the course, and pushes released grades back to the LMS gradebook.

## Model
- `LmsConnection` (per teacher): id, teacher_id, provider ("canvas"|"moodle"|"blackboard"|"brightspace"), base_url, credential (API token; stored server-side only, never returned), label, created_at, last_sync_at, last_error.
- `CourseLink`: id, connection_id, lms_course_id, name, code; roster synced into users (role student; username = LMS login id or email local part; name from LMS) + `CourseMember` (course_link_id, user_id, lms_user_id).
- Exam gains `course_link_id` (nullable) and `lms_assignment_id` (nullable, set on first grade push).

## API (role teacher)
- `GET /api/lms/providers` → `[{"id":"canvas","name":"Canvas","auth":"token","status":"ready"}, ...]` (others "beta").
- `GET/POST /api/lms/connections`, `DELETE /api/lms/connections/{id}`; POST body `{"provider","base_url","token","label"}`; the server verifies the token by calling the LMS ("who am I") and returns 422 `{"detail":"..."}` with a clear reason if it fails.
- `GET /api/lms/connections/{id}/courses` → courses the teacher teaches in the LMS.
- `POST /api/lms/connections/{id}/courses/{lms_course_id}/link` → CourseLink + roster sync (idempotent). `POST /api/lms/courses/{link_id}/sync` re-syncs the roster. `GET /api/lms/courses` lists linked courses with member counts.
- `PUT /api/exams/{id}/lms` body `{"course_link_id": id|null}` links an exam; only linked-course students can start it when set (others 403 "This exam is for <course> students").
- `POST /api/exams/{id}/lms/push` → pushes released grades (creates the LMS assignment named after the exam with points_possible = total_points on first push, then posts each student's score; comments included). Returns `{"pushed": n, "skipped": n, "errors": [{"student","detail"}]}`. 409 if grades not released or no course link.
- All outbound LMS calls: httpx async, timeouts 15 s, retries on 429/5xx with backoff, pagination (Canvas Link headers), never block the student hot path.

## Web (teacher)
- Sidebar item "LMS sync" → `/teacher/lms`: connect (provider cards, base URL, token with "How to get a token" help), connection list, courses list with "Link" / "Re-sync roster", linked courses with member counts, last sync time / error.
- Exam setup: "Course in your LMS" select (linked courses). Results header: "Send grades to Canvas" button when linked + released, showing the push result.

## Slice 5 — response shapes (as implemented)
- Provider: `{"id","name","auth":"token"|"oauth_token","status":"ready"|"beta","token_help","base_url_hint"}`.
- Connection: `{"id","provider","provider_name","status","base_url","label","lms_user_name","created_at","last_sync_at","last_error","course_count"}` — never a token. POST with the same provider+base_url replaces the stored token (same id). base_url must be https (http / private IP literals only with env `PROCTORA_LMS_ALLOW_PRIVATE=1`).
- `GET /api/lms/connections/{id}/courses` → `[{"lms_course_id","name","code","link_id": id|null}]`.
- CourseLink: `{"id","connection_id","provider","provider_name","lms_course_id","name","code","member_count","exam_count","last_sync_at","last_error"}`; link/sync responses add `"sync": {"added","updated","removed","total"}`.
- Exam (teacher view) adds `"course_link_id": id|null, "lms_assignment_id": str|null`.
- Push adds `"lms_assignment_id"`. `skipped` = attempts not submitted or whose student is not a member of the linked course.
- LMS failures (unreachable, rejected token, LMS error) → `502 {"detail"}` (never 401) and are stored as `last_error`; concurrent link/sync/push of the same thing → 409.
- Imported students get no usable password (password_hash `!lms-import`); they need SSO or a password reset flow (not built). An LMS user whose login id equals an existing FairPlay *student* username is mapped to that student.

---

# Slice 6 — offline packages, question images, Scan & Grade, topics, insights, profiles, practice

Delivery modes per exam (`delivery`): `"computer"` (FairPlay app, live room + lockdown — unchanged, optional),
`"paper"` (printed, scanned, AI-assisted grading), `"website"` (= existing mode "external"). Field `delivery`
is derived for old exams: mode external → "website", else "computer". New exams send it.

## A. Offline exam packages + resume offline
- `GET /api/student/exams/{id}/package` (student, exam live OR scheduled within 2 h): `{"exam_id","version","alg":"AES-GCM","salt","iv","ciphertext"}`.
  Plaintext = JSON of the student exam view (questions without keys, title, duration, images as data URLs ≤ 2 MB total, else URLs).
  Key = PBKDF2-SHA256(start_code + exam_id (+ package key from v2), salt, `iterations`, 32 bytes): version 3 (audit 2026-10-13) writes `"iterations": 600000` into the package; packages without the field use 100 000. Clients refuse counts outside 1 000–10 000 000. The server only serves packages while
  `start_code` is set; the client downloads on the check-in page and stores in IndexedDB/localStorage (`fairplay:pkg:<exam_id>`).
- Start stays online when possible (`POST start`). If the network is down at start, the client decrypts the package with the
  typed code (wrong code = decrypt fails = "Wrong start code"), creates a local attempt (`offline:<exam_id>`), lets the student
  work, and on reconnect calls `POST /api/student/exams/{id}/start` with `{"start_code", "offline_started_at"}` then syncs answers.
  Server accepts `offline_started_at` up to 30 min in the past and sets deadline from it.
- Resume: the exam page caches the last AttemptView in localStorage (`fairplay:attempt-view:<aid>`) and uses it when
  `GET attempt` fails with a network error.

## B. Question images
- `POST /api/uploads` (teacher, multipart `file`, png/jpeg/gif/webp/svg→rejected, ≤ 5 MB) → `{"id","url":"/api/uploads/<id>","width","height"}`.
  Stored under `server/data/uploads/` (or S3-compatible later); `GET /api/uploads/{id}` serves with long cache, auth not required
  (ids are 128-bit random).
- Question gains `"images": [{"url","alt"}]` (max 6). Shown above the prompt for students, in review, feedback, grading, print.
  Options may contain images later (not now).

## C. Topic tags (required)
- Question gains `"topics": ["integration by parts", ...]` (1–5, each 1–60 chars, trimmed, case-insensitive dedupe, stored lower-case).
  POST/PUT exam: every question needs ≥ 1 topic → 422 "Each question needs at least one topic" — EXCEPT old exams already stored
  (no backfill) and the hidden website-exam item (topic = exam course or "general").
- `GET /api/topics` (teacher) → `[{"topic","question_count"}]` for autocomplete.

## D. Paper exams — Scan & Grade
- Exam with `delivery: "paper"`: no start code / lockdown. Teacher assigns students via LMS course link or picks students
  (`PUT /api/exams/{id}/roster {"usernames": [...]}`; default = all students).
- Print: `GET /api/exams/{id}/print.pdf` → one booklet per rostered student (A4), each page has a QR code
  `FP1|<exam_id>|<student_id>|<page_no>` plus human-readable name/page, and per question a bordered answer box sized by type
  (mcq: bubbles A–H; numeric/short: one line; essay/work: big box; teacher can set `answer_lines` per question). The layout
  (box rectangles per page in PDF points) is stored as `print_layout` so scans can be cropped. `?blank=1` gives one unnamed
  copy (QR has student_id 0 → matched later by a "Name" box read by AI or picked by the teacher).
- Upload: `POST /api/exams/{id}/scans` (multipart PDF or images, ≤ 200 MB) → `{"batch_id"}`; processing runs in the background
  (asyncio task / thread pool; never blocks the event loop): render pages (PyMuPDF, 150–200 dpi), detect QR (OpenCV
  QRCodeDetector, try rotations), deskew/scale using QR position, crop answer boxes, store crops as uploads.
  `GET /api/exams/{id}/scans` → batches with status counts; `GET /api/exams/{id}/scans/unmatched` → pages without a usable QR
  with a preview; `POST .../pages/{page_id}/assign {"student_id","page_no"}` fixes them by hand.
- Matched answers create/merge a submitted attempt per student (`source: "paper"`); answers hold the crop image URL
  (`{"image": url}`), MCQ bubbles are read by pixel darkness (OpenCV) as an automatic answer.
- AI grading queue: for each paper answer the AI reads the crop (vision) against prompt + rubric/correct answer and drafts
  `{"points","comment","confidence","transcript","mistake_tags":[...]}` stored as `ai_draft` on the answer (not a grade).
  Teacher: Approve (draft → grade), Edit, Regrade (re-run AI), and **bulk approve** similar answers (group by identical
  normalized transcript or by same draft points+mistake tag, with a "approve these N" action). Low confidence (< 0.7) is flagged.
- Release / feedback / gradebook / LMS push: unchanged (paper attempts are normal attempts with grades).

### D — as implemented (server/app/routes_scan.py, server/app/scan/)
- Teacher: `GET /api/scan/exams` (paper exams + any exam with printed booklets/scans: `{exam_id,title,course,status,delivery,printed,batches,unmatched,paper_attempts,to_review,grades_released,...}`);
  `GET|PUT /api/exams/{id}/roster` → `{source:"picked"|"course"|"all", students:[{id,username,name}], all_students:[...]}` (PUT `[]` = default);
  `GET /api/exams/{id}/print` (pages_per_student, printed_at, layout_changed, questions with answer_lines) and
  `PUT /api/exams/{id}/print-settings {"answer_lines": {qid: 1..40|null}}`; `GET /api/exams/{id}/print.pdf[?blank=1]`.
  Question gains optional `answer_lines` (1–40, null = default: essay 12, numeric/short 1 line).
- `POST /api/exams/{id}/scans` → 202 `{"batch_id"}`; needs a printed layout (409) — the layout is stored when print.pdf is generated.
  The first upload sets the exam to `closed` (papers collected) so release works. `GET /api/exams/{id}/scans` →
  `{ai_enabled, pages_per_student, students_expected, students_scanned, unmatched, batches:[{batch_id,status:"queued"|"reading"|"grading"|"done"|"failed"|"stalled",page_count,processed,matched,unmatched,ai_total,ai_done,error,...}]}`;
  `GET .../scans/unmatched` → `[{page_id,seq,reason,preview,name_crop,qr_found,page_no,suggested_name,suggested_student_id}]`;
  `POST .../scans/pages/{page_id}/assign {"student_id","page_no","follow_on":true}` (follow_on: the next unnamed pages 2..n of a blank booklet
  in the same upload go to the same student); `POST .../scans/pages/{page_id}/ignore`; `POST .../scans/{batch_id}/rerun-ai`.
- Paper answer value: `{"image": "/api/uploads/<id>", "page", "mcq": idx|null, "marks": "one"|"none"|"multiple"|"unclear", "blank", "fill"|"ink"}`.
  Grading: mcq with marks "one" → automatic; "none" → 0 (unanswered); everything else on paper is **pending until a teacher grade exists**.
  Results/feedback rows of paper answers add `"image"`; `given` = mcq index, the approved transcript, or "(written on paper)". Attempt gains `source` ("computer"|"paper") and `ai_drafts`.
- Queue: `GET /api/exams/{id}/scan/queue` → `{questions:[{...,total,to_review,approved,auto,low_confidence}], items:[{attempt_id,username,name,question_id,image,mcq,marks,blank,draft,grade,status:"needs_review"|"approved"|"auto",auto_points,low_confidence,group}], groups:[{question_id,key,label,points,count,items}], ai_enabled}`.
  Draft = `{points,comment,confidence,transcript,mistake_tags,source:"ai"|"key"|"blank"|"bubbles"|"fallback",low_confidence}`.
  `POST .../scan/answers/{aid}/{qid}/approve` body `{}` (draft as is) or `{"points","comment"}` (edit); a "fallback" draft (AI off) cannot be approved as is (409).
  `POST .../scan/answers/{aid}/{qid}/regrade {"hint"}` → `{draft}`; `POST .../scan/approve-bulk {"items":[{attempt_id,question_id}]}` → `{approved,skipped}`.
- Web: `/teacher/scan`, `/teacher/exams/:id/scan?step=print|upload|grade|release`; paper exams show a "Scan & Grade" tab instead of the Live room.

## E. AI provider (shared)
- `server/app/ai/` — `async def complete_json(system, prompt, images: list[bytes]=[], schema_hint=str) -> dict`.
  Env: `FAIRPLAY_AI_PROVIDER` = `anthropic` | `openai` (any OpenAI-compatible: OpenAI, DeepSeek, Qwen/DashScope, Moonshot —
  needed for China) | `none` (default). `FAIRPLAY_AI_KEY`, `FAIRPLAY_AI_BASE_URL`, `FAIRPLAY_AI_MODEL`.
  With `none`: deterministic fallbacks (grading: 0 points, confidence 0, comment "AI is off — grade by hand"; variations: numeric
  perturbation only). Every AI output is a draft a teacher approves; nothing reaches students unapproved.

## F. Class insights (new page `/teacher/insights`)
- `GET /api/insights?course=&exam_ids=` → `{"topics":[{"topic","avg_percent","n_answers","n_students","trend"}],
  "hardest":[{"exam_id","question_id","prompt","avg_percent","lost_points"}], "mistakes":[{"label","count","examples":[...],
  "topics":[...]}]}`. Topic score = sum awarded / sum max over graded answers (pending excluded). Mistakes = grouped AI
  `mistake_tags` + teacher comments (normalized text clustering, top 20).

## G. Student profile (new page `/teacher/students/:username`, also `/teacher/students` list)
- `GET /api/students/{username}/profile` → `{"student","topics":[{"topic","avg_percent","n","trend":[{"date","percent"}]}],
  "strengths":[topic...], "weak":[topic...], "exams":[{"exam_id","title","date","percent","time_on_task_min"}]}`.
  Strength ≥ 80 %, weak < 60 % with ≥ 2 answers. Time-on-task = submitted_at − started_at (computer) — shown as context
  only, labelled "not a cheating signal", never flagged.

## H. Practice assignments (new page `/teacher/practice`; student `/student/practice`)
- `POST /api/practice/generate {"username"|"usernames", "topics"?, "count": 1–20}` → draft set per student from their weak
  topics: variations of the teacher's own questions in those topics (AI rewrite + numeric perturbation). Every numeric/math
  answer verified with SymPy (`server/app/mathcheck.py`: parse expression/number answers, check equivalence, numeric
  answers recomputed from a stored sympy formula when the variation is generated by perturbation); unverifiable items are
  dropped or marked "needs teacher answer". Sets status `draft` → teacher previews/edits/removes items → `approved` → `sent`.
- Student: `GET /api/student/practice` lists sent sets; answering gives instant correctness (checked server-side), never
  counts toward grades (`formative: true`, excluded from gradebook, results, averages; included in profile as "practice").

## Slice 6 A/B/C + delivery — as implemented
- `delivery`: stored column (NULL on old rows → derived). POST/PUT: omitted → derived from `mode`; only `delivery` sent →
  sets `mode` ("website" → "external", else "questions"); both sent and disagreeing → 422. Returned in teacher Exam and in
  `GET /api/student/exams` items. Paper exams: `POST /open` → 409 "Paper exams are printed, not opened in the app";
  `POST /close` also works from draft (so grades can be released); never listed in `GET /api/student/exams`;
  start / package → 404.
- Question `images`: `[{"url","alt"}]`, max 6, url `/api/uploads/<16–64 [A-Za-z0-9_-]>` or `https://…`, alt ≤ 300 (trimmed).
  Included in teacher view, student exam view, grading queue questions and student feedback questions.
  Upload ids are 32 hex chars; files in `FAIRPLAY_UPLOADS_DIR` (default `server/data/uploads`). Type checked by bytes
  (png/jpeg/gif/webp; svg and anything else 422), > 5 MB → 413, Pillow must open it (else 422).
  `app.uploads.save_image_bytes(bytes)` / `read_upload(id)` are reusable helpers (e.g. scan crops).
- Question `topics`: in teacher views only (NOT in the student exam view or package: tags can hint at the method).
  Validation error text: "Each question needs at least one topic" (422). Hidden website item topic = lower-cased course or "general".
- Package: `version` = 1 (format version). Plaintext `{"exam_id","title","course","duration_min","mode","shuffle_questions",
  "questions","generated_at"}`; questions in original order. Served while `start_code` is set (i.e. live); website exams → 409.
  Cached per exam+code+duration for 5 min (one PBKDF2 per room, built in a worker thread).
- `offline_started_at` older than 30 min is clamped to 30 min ago (not rejected, so offline work is never lost; the
  deadline may then already be past → answers 409, submit still accepted and marked late); future values → now.
- Web: local attempt id `offline:<exam_id>`; record `fairplay:offline:<exam_id>` = {exam_id, start_code, started_at, view,
  real_attempt_id}. The exam page keeps running under the offline URL after reconnect and routes every call to the real id.


## App session and lockdown hardening (audit 2026-10-12)

Binding for `server/`, `web/` and `mac/`. Background: `claude/lockdown-audit-2026-10-12.md`.

### Server settings

| env | meaning |
|---|---|
| `PROCTORA_SECRET` (or `FAIRPLAY_SECRET`) | required, 16+ chars. The old public default is refused. Without it the server **does not start** unless `FAIRPLAY_DEV=1` (then a random secret is kept in `data/secret.key`). |
| `FAIRPLAY_APP_KEY` | required when `FAIRPLAY_REQUIRE_APP=1`: HMAC key compiled into the Mac app (`mac/build.sh`). Comma-separated list during key rotation. The public dev key is accepted only with `FAIRPLAY_DEV=1`. |
| `FAIRPLAY_REQUIRE_APP` | default `1`: exams start only from the app. `0` = browser demo mode (e2e scripts, demos); browser attempts show as client "browser". |
| `FAIRPLAY_DEV` | `1` for a local laptop demo only (`Run Server.command` sets it). |
| `FAIRPLAY_CORS_ORIGINS` | extra origins (comma-separated). Default: none (the UI is same-origin). Dev adds the Vite dev server. |

Start codes have **6 digits**. Each live exam also has a 6-digit `invigilator_code` (teacher views only: exam, live room).

### Start (app proof)

1. `GET /api/student/exams/{id}/challenge` → `{"nonce", "expires_in": 120}` (single use, bound to student + exam).
2. Page → app bridge `{"action":"attest","nonce"}` → app replies event `{"type":"attestation","nonce","device_id","signature","version","build"}` (or `attestationFailed`).
   `signature = hex(HMAC-SHA256(app key, "start|<nonce>|<device_id>"))`.
3. `POST /api/student/exams/{id}/start` body adds `"client": {"device_id","nonce","signature","version","build"}`.
   - no `client` and `FAIRPLAY_REQUIRE_APP=1` → `403 {"detail": "Open this exam in the FairPlay app. ..."}`; bad/reused proof → `403`.
   - Response = attempt view + `"session"` (string). The page stores it per attempt and sends `X-FairPlay-Session: <session>` on **every** `/api/student/attempts/{aid}/...` request (GET included).
   - Existing attempt, other device → `409 "This exam is already open on another computer..."` + event `second_device`.
   - Existing attempt, same device → new session (old one stops working) + event `re_entry`, except a repeat within 15 s (double click).
   - After `POST /api/exams/{id}/attempts/{aid}/release-device` (teacher) the next start from any app binds the attempt (event `device_bound`).
4. Wrong/missing session on an attempt request → `409 "This exam was opened somewhere else..."` + event `foreign_session` (max 1/min). In browser demo mode a request without the header is still accepted.

### App beats (the only source of "locked")

`POST /api/student/attempts/{aid}/app-beat` every 5 s, sent by the **app** (not the page), with the student token and `X-FairPlay-Session`:
`{"session","device_id","ts","locked","checks","signature"}`, `signature = hex(HMAC(app key, "beat|<aid>|<session>|<device_id>|<ts>|<0|1>|<checks>"))`, `ts` = client ms, strictly increasing.
`checks` e.g. `vm=0,disp=1,mir=0,tap=1,inp=1,proc=0,bg=2`: `proc` = apps that must still close (only remote control / screen sharing / AI overlays; `?` when the process list could not be read, audit M-13), `bg` = other windowed apps left running behind the exam. Response `{"ok","status","deadline_at","server_time"}`.

**Apps during the lock (2026-10-13).** Only the denied list (remote control, screen sharing, AI assistants and overlays) is closed and blocks the exam. Every other app may keep running in the background (a VPN client such as AnyConnect, a sync tool, an open document): each is reported once per lock as `app_in_background` with its name, hidden, and kept behind the exam by the focus guard. Leftover shell scripts are ended only if they started within 10 minutes before the lock or later; older ones are reported (audit M-12). Same on Windows.

**Device id (audit M-07).** The Mac sends `hw-` + 32 hex of SHA-256(`fairplay-device|<IOPlatformUUID>|<app key>`), so copying FairPlay's files to another Mac does not move the device; a random id in Application Support is the fallback only when IOKit has no UUID. The server flags `network_changed` (once per 5 min) when one app session's signed beats come from a new network address.

**VM check (audit M-10).** Any of: `kern.hv_vmm_present`, a VM word in `hw.model`, the Intel `VMM` CPU flag, or a VM manufacturer/model on the IOKit platform entry. The one-screen message names the screens found.
Server events: `app_gap` (> 20 s since the last beat), `unlocked_in_exam` (locked → unlocked while writing), `no_app_signal` (page heartbeats but no app beat for 45 s; max 1 per 5 min).
`POST .../heartbeat` still updates "online"/current question; its `locked` is ignored.

Live room student fields added: `app_online`, `last_app_signal_at`, `app_checks`, `client` ("app"|"browser"), `entries`; `locked` = fresh signed beat says locked. Live room adds `invigilator_code`.

### Invigilator exit

App (Ctrl-Opt-Cmd-E) → `POST /api/student/attempts/{aid}/exit-code {"code", "nonce"}` → `{"ok": true, "nonce", "proofs": [HMAC(app key, "exit|aid|session|ok|nonce") per key], "proof": HMAC(first app key, "exit-verdict|aid|session|1|nonce")}` (Mac checks `proofs`; Windows checks `proof` when `RequireSignedVerdicts` is on) or `403`; 5 wrong → `429` for 5 min. Each try is reserved (atomic INCR) before the compare, so parallel guesses can't exceed 5 (audit 2026-10-13); the start code works the same way. Events `emergency_exit`, `emergency_exit_rejected`. No code is stored in the app.

**Offline exit (2026-10-12, reworked after review)** — when the server cannot answer (no network, 5xx, session moved) or the invigilator picks "Offline code…" in the same dialog. The locked Mac shows a random **Mac code** (8 chars from `ABCDEFGHJKMNPQRSTUVWXYZ23456789`, shown as `XXXX-XXXX`, new per lock). Exit code = first 4 bytes of `HMAC-SHA256(K, "offline-exit|<MAC CODE>")` big-endian, mod 10^8, 8 digits zero-padded. Checked in the app; 5 wrong → 5 min wait.

`K` is the attempt's **offline key**: `HMAC-SHA256(server secret, "offline-exit-key|<attempt>|<session>")`. It is not the app key and is not in the binary. The app fetches it itself right after the exam session is known:

- `POST /api/student/attempts/{aid}/offline-key {"session","device_id","ts","signature"}`, signature = HMAC(app key, `offkey|<aid>|<session>|<device_id>|<ts>`) → `{"key": "<64 hex>", "proof": HMAC(first app key, "offkey-resp|aid|session|key|ts")}`. **Once per session**: a second request → `409` + event `offline_key_requested_twice`. The page never sees the key; the app keeps it in memory only.
- Teacher: `POST /api/exams/{id}/attempts/{aid}/offline-exit {"mac_code"}` → `{"code"}` for that student's current session; event `offline_exit_code_issued` (not a flag). Live room: "Mac offline or stuck? · offline exit" → pick the student, type the Mac code.
- App events `offline_exit` / `offline_exit_rejected`, forwarded when the page is online again.
- No key (the app never reached the server for this exam): **release builds have no offline exit** — restart the Mac, answers are saved. Dev builds fall back to the app key (`server/tools/offline_exit_code.py`) and also accept `0000` (`defaults write ai.proctora.student DevExitCode <code>`).
- Limit: someone who extracted the app key AND holds the student's token can request the key once per session — that request is either the app's own (then theirs is refused and flagged) or it makes the app's refused (no offline exit, flagged). App Attest (M-20) closes this.
- Test vectors: key bytes `00 01 … 1f`, Mac code `K7QM3XPA` → `06710479`; dev key (fallback) → `37496243`.

### Dev trace log

Dev builds write `logs/events.log` in the repo (path compiled in by `mac/build.sh`, override with `FAIRPLAY_DEBUG_LOG`; rotates at 5 MB to `events.log.1`): clicks in the app and on the page (element only), lock/unlock, check results, every app-quit attempt and its result, bridge messages, app↔server requests that fail (and all non-beat requests), page fetch errors, JavaScript errors/console.error, page crashes, uncaught exceptions, sleep/wake. Never typed text, codes, tokens or answers. Release builds write nothing there.

### Teacher

- `POST /api/exams/{id}/rotate-code` → exam with a new start code (live only). Offline packages need the new code.
- `POST /api/exams/{id}/attempts/{aid}/release-device` → `{"ok": true}`.

### Native bridge (changed)

Page → app: `lock` may carry `attempt_id`, `token`, `session`; new `session` (same fields) and `attest`; `openExternal` adds `session`.
App → page: new `attestation`, `attestationFailed`, `unlockPending {"reason": "offline"|"not_submitted"}`.
`unlock` no longer unlocks by itself: the app unlocks only when `GET /api/student/attempts/{aid}` says `submitted` (or 404/409), retrying every 3 s. The app only accepts bridge messages from the main frame of the exam server (scheme + host + port).

### Website mode

`allowed_domains` entries: exact host (`canvas.dku.edu`), `*.host` for the host and all subdomains, optional path prefix (`docs.google.com/forms/d/e/1FAIpQL/`). Old bare entries now mean the exact host only. The exam address always adds its exact host, or host + folder on shared hosts (docs.google.com, drive.google.com, ...). Release apps open https only.

### Mac app builds

`mac/build.sh` (dev, default) vs `FAIRPLAY_BUILD=release` with `FAIRPLAY_SERVER` (https), `FAIRPLAY_APP_KEY`, optional `FAIRPLAY_PINS` (`mac/pin.sh`), `FAIRPLAY_SIGN_ID`, `FAIRPLAY_NOTARY_PROFILE`. Release: no UserDefaults/env overrides, no safety unlock, no ATS exceptions, hardened runtime.

### Audit 2026-10-13 additions (bridge and app)

- **Preflight (M-11):** page → app `{"action":"preflight"}`; app → page `{"type":"preflight","problems":[...]}` (plain sentences; `[]` = ready). The page shows the problems and doesn't ask to lock until the list is empty. Hosts that don't answer within 3 s count as `[]` (the lock's own checks still run).
- **Bridge nonce (W-10):** the host may set `window.__fairplayNonce` (≥16 chars) at document start; the page reads it once, deletes it, and from then on ignores `proctora-native` events whose `detail.bridge_nonce` differs (its own field: `attestation` messages already use `nonce` for the server challenge). The Mac app sets one per launch. The Windows shell should do the same.
- **Signed hand-in (M-04):** `GET /api/student/attempts/{aid}/handin?nonce=&device=<app device id>` → `{"status": "submitted"|"in_progress"|"gone"|"moved", "proofs": [HMAC(app key, "handin|aid|session|status|nonce") per key]}`. The app unlocks only on a valid proof for submitted/gone/moved. `moved` = the attempt is now bound to ANOTHER device: from the bound device itself a stale or made-up session is `in_progress` (audit 2026-10-13). While locked, both apps refuse a `session`/`lock` message naming a different attempt, and a token-only update keeps the offline key.
- **Beat checks (M-02):** `console=0` → the server treats the beat as not locked and flags `off_console`; `secinput=1` → `secure_input_other_app`.
- **Lock without a session (M-06):** `{"action":"lock"}` must carry `attempt_id`/`token`/`session`, or `offline: true` (offline start). Otherwise `{"type":"lockFailed","reason":"bad_session"}` and the page asks for the start code again.
- **Relaunch (M-17):** after ending while locked, the watchdog reopens the app with `--resume-attempt <aid>`; it opens `/student/exam/<aid>` directly.

## 2026-10-13 simplification
- **Optional parts:** `FAIRPLAY_FEATURES` (comma list of `scan`, `lms`, `practice`; default empty). Parts not listed have no API routes (404) and the teacher UI hides them; `GET /api/health` returns `features`. Paper exams (`delivery: "paper"`) are refused (400) unless `scan` is on. Insights is hidden in the UI; its `/api/students…` profile routes stay.
- **Classes:** `term` (free text, ≤80, e.g. "Fall 2026 · Section 2") on create/`PUT`; responses add `term` and `teacher_name`. Student `GET /api/student/classes` adds `term`. Invite link: `/student/join/<code>` (signed out: code kept for this tab, joined right after sign-in).
- **Grades:** `GET /api/gradebook` and `/api/gradebook.csv` take `class_id` (the class's members are listed even before any exam); `GET /api/grading` rows add `class_id`.
- **Exams:** saving needs no class; opening still does (the editor asks). Question `topics` are optional; a question without one is stored under the course (lower-case) or `general`.
