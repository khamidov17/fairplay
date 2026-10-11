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

- Answers saved to `localStorage["proctora:attempt:<aid>"]` = `{answers, client_seq}` on every change (client_seq++), synced via PUT `/answers` debounced 1 s and retried every 3 s while unsynced. Header shows "All answers saved" (synced) / "Saved on this computer" (pending).
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
  Key = PBKDF2-SHA256(start_code + exam_id, salt, 100 000 iterations, 32 bytes). The server only serves packages while
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

---

# Phase 1a — trustworthy exam sessions (security + audit)

Everything above stays valid unless changed here.

## Environment and deployment
- `FAIRPLAY_ENV` = `development` (default) | `production`. Production refuses to start when `PROCTORA_SECRET` is
  unset, the dev value, or shorter than 32 characters, or when `FAIRPLAY_CORS_ORIGINS` contains `*`; it warns loudly
  without `REDIS_URL`; it never creates the demo users; load-test users need `PROCTORA_SEED_LOADTEST_USERS=N` **and**
  `FAIRPLAY_ALLOW_LOADTEST_USERS=1`.
- First accounts in production: `python -m app.manage create-user --username U --name "N" --role teacher`
  (password prompted twice, or `--password-stdin`); also `set-password`, `revoke-tokens`, `unlock-login`.
  `--role` is passed through unchanged (any role the token code accepts).
- CORS: `FAIRPLAY_CORS_ORIGINS` (comma list). Default: `*` in development, none (same-origin only) in production.
  (Replaces "CORS: allow all origins (dev)" in Serving.)
- Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`,
  `Cross-Origin-Opener-Policy: same-origin` and `Content-Security-Policy: default-src 'self'; script-src 'self';
  style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; font-src 'self' data:; connect-src 'self';
  object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'`; production adds HSTS. Nothing in
  FairPlay is framed (the Mac app and website exams navigate top-level). A route's own header wins.

## Auth (changed / added)
- Sign-in limits (store-backed, shared by all workers): 10 failures per username in 15 min, or 100 per IP, lock that
  username / IP for 15 min → `429 {"detail": "Too many failed sign-ins. Try again in N minutes."}` (even with the right
  password). Success resets the username counter.
- Token payload is now `user_id:role:exp:token_version` (HMAC-SHA256 as before; 3-field tokens count as version 0).
  `users.token_version` (int, default 0) is checked on every request through the store (`tv:<user_id>`, 10 min
  cache, DB read on a miss): a token older than the user's current version → 401.
- `POST /api/auth/password` `{"current_password","new_password" (≥ 8)}` → `{"token","role","name"}` (new token);
  wrong current password → 403. Bumps token_version, so every other sign-in stops working.
- `POST /api/auth/logout-all` → 204: bumps token_version (sign out everywhere, including this device).
- Admins (later: superadmin endpoint) call `app.account.revoke_user_tokens(session, user_id)`.

## Invigilator exit code (replaces the fixed 1234)
- `POST /open` issues a new per-exam code `XXXX-XXXX` (8 chars from the receipt alphabet, ~40 bits). Teacher only:
  `exit_code` in the teacher Exam (null while draft) and in `GET /live`. Never in a student response.
- Student attempt view (start / GET attempt) and the offline key release add
  `"exit_check": {"alg":"PBKDF2-SHA256","iterations":100000,"salt":b64,"hash":b64}` = PBKDF2(normalised code, salt).
  Normalised = upper-case, letters and digits only (`k7q2 m9xd` = `K7Q2-M9XD`).
- `POST /api/student/attempts/{aid}/exit-code` `{"code"}` (own attempt) → `{"ok": true}` | 403 `Wrong exit code` |
  429 after 5 wrong in 10 min (5 min lock) | 404 not this student's attempt. Every try is audited.
- Native bridge (added): JS → native `{"action":"examSession","attempt_id": str|null,"token": str|null,
  "exit_check": {...}|null}`, sent before `lock` and again when an offline attempt gets its server id; `openExternal`
  also carries `exit_check`. On Ctrl-Opt-Cmd-E the app asks the server first; only when the server cannot be reached
  does it compare with `exit_check` locally. The integrity event `emergency_exit` detail says how it was verified
  (`server` / `offline` / `override`). `defaults write ai.proctora.student InvigilatorCode X` is a dev/IT override used
  only when no exam session is known; there is no built-in default code any more.

## Attempt-session write rules (unchanged, now tested)
- Answer saves: 409 after the deadline (`Time is up`), after submit / close (`Attempt already submitted`).
- Submit: idempotent (same receipt); after the deadline + 60 s still accepted but marked late (offline work is never
  lost) and audited with `late: true` and whether the submit body carried newer answers.
- Heartbeat after submit / close returns `status: "submitted"`. Start after submit → 409; after close → 409.

## Offline package security (replaces the key of slice 6 A)
Threat model. A student may copy the package off the laptop, work on it at leisure with fast hardware, share it
with others, and use their own valid token against the API. The start code (4 digits = 10 000 values) is a
"you are in the room now" check, not a cryptographic secret: anything that lets a student test start codes offline
reveals the code in seconds. Goals: (1) a package obtained before the exam opens reveals nothing; (2) a package is
only useful together with something the server hands out only while the exam is open, to an authenticated student
allowed to take it; (3) every hand-out is recorded; (4) a student whose network drops at the start can still begin.

Design (package format version 2):
- `GET /api/student/exams/{id}/package` (student; exam live, or scheduled to start within 2 h; website / paper → 409 /
  404; LMS course gate as for start) → `{"exam_id","version":2,"alg":"AES-GCM","iv","ciphertext"}` =
  AES-256-GCM(K, iv, plaintext, aad = exam_id), K = random 256-bit per-exam key (`exams.package_key`, created on first
  use, dropped at close so the next opening gets a new K). No part of it depends on the start code, so it cannot be
  brute-forced.
- `GET /api/student/exams/{id}/package-key` (student; exam **live** only, else 409) → `{"exam_id","version":2,
  "alg":"PBKDF2-SHA256/AES-GCM","iterations":100000,"salt","iv","wrapped_key","exit_check"}`: K wrapped with
  KEK = PBKDF2-SHA256(start_code + exam_id, salt, iterations, 32 bytes) → AES-GCM(aad = exam_id). The check-in page
  fetches it once the exam is open (`fairplay:pkgkey:<exam_id>`); an offline start needs both halves; a wrong typed
  code fails the GCM check ("Wrong start code"). Both responses are cached per exam (one PBKDF2 per room).
- Audit: `package_downloaded` and `package_key_released` once per student per exam opening.
- The server re-checks the start code when the offline attempt reaches it (`POST start` with `offline_started_at`).
Residual risk (accepted, documented): once the exam is open, a student who fetched the key release can brute-force
the start code from it, i.e. learn the code without being in the room. Mitigations: release only while live, only to
authenticated students allowed to start (course gate), each release audited and shown in the session timeline,
offline starts are marked in the audit log with the claimed start time (clamped to 30 min), and closing/reopening
the exam rotates K and the code. If this is not acceptable for a deployment, the next step is to deliver the key
release only to registered devices or to bind it to the student (per-student wrap), at the cost of one PBKDF2 per
student.

## Audit log
Table `audit_events` (append-only; no foreign keys so an exam delete keeps its trail):
`id, at, actor_id, actor_role ("student"|"teacher"|"system"|""), ip, user_agent, action, exam_id, attempt_id,
detail (JSON)`. Indexes (exam_id, attempt_id, at), (attempt_id, at), (actor_id, at).
Actions: `login_ok, login_failed, login_locked, password_changed, sessions_revoked, exam_opened, exam_closed,
deadline_extended, start_code_wrong, attempt_started (detail.offline …), attempt_resumed, answer_saved (coalesced:
one row per attempt per 5 min with first_seq, last_seq, count, until), connection_lost / connection_restored
(heartbeat gap > 30 s, detail.gap_s), submitted (receipt, late, answers_from_submit), auto_submitted (receipt),
grade_changed (question_id, question_index, old_points, points), comment_changed, grades_released,
grades_unreleased, package_downloaded, package_key_released, exit_code_used, exit_code_rejected`.
Integrity events stay in `integrity_events` and are merged into the timeline. Hot-path cost: a save adds two store
operations (DB row only when a 5-min window closes), a heartbeat one store GETSET (DB rows only after a gap).
Helpers for other modules: `app.audit.add(session, action, ...)` (in the caller's transaction) and
`await app.audit.record(action, ...)` (own transaction, never raises).

`GET /api/exams/{exam_id}/attempts/{attempt_id}/timeline` (teacher, own exam only; 404 otherwise) →
```json
{"exam_id","attempt_id","exam_title","student":{"username","name"},"status","started_at","deadline_at",
 "submitted_at","receipt","late","auto_submitted",
 "events":[{"at":"ISO","source":"audit"|"integrity"|"heartbeat","action":"connection_lost",
            "summary":"Connection lost (no heartbeat from the exam page)","actor":"student"|"teacher"|"system",
            "ip":"10.0.0.7"|null,"detail":{},"tone":"info"|"warn"|"good"}]}
```
Ordered by time: this attempt's audit rows, the student's sign-ins and exam-level actions around the session,
exam open/close/extend/release, integrity events (client time when believable), and a trailing "No heartbeat since"
line when an in-progress attempt has gone quiet. Web: `/teacher/exams/:id/submissions/:attemptId/timeline`
("Session timeline", linked from the submission review page).

## Uploads (clarified)
`GET /api/uploads/{id}` stays unauthenticated: an upload id is a 128-bit capability (unguessable, never listed),
because `<img>` tags cannot send a bearer token. Uploading needs a teacher. Anyone who is given an upload URL can
read that image; do not put upload URLs anywhere other than the exam/grading views of the people allowed to see them.
# Registration + superadmin

## Users (added columns)
- `email` (nullable, unique, stored lowercase), `status` `"active" | "pending" | "disabled"` (default `active`),
  `last_login_at` (set on every successful login). Registered usernames are stored lowercase.

## Auth (added / changed)
- `POST /api/auth/login`: `username` may be a username **or an email** (contains `@`; case-insensitive), max 254.
  Right password but `status` pending → `403 {"detail": "Your teacher account is waiting for approval by an administrator."}`;
  disabled → `403 {"detail": "This account has been disabled. Contact your administrator."}`. Wrong password stays 401
  (status is never revealed before the password is checked). Response `role` may be `"superadmin"`.
- `POST /api/auth/register` (no token) body `{"name","email","username","password","role": "teacher"|"student"}` →
  `201 {"status": "active", "role", "name", "token"}` (signed in, like login) or, for a teacher in approval mode,
  `201 {"status": "pending", "role": "teacher", "name", "token": null, "detail"}`.
  - name 2–100 chars (whitespace collapsed); email `x@y.z`; username `^[a-z0-9][a-z0-9._-]{2,31}$` after lowercasing;
    password 8–128 chars, letters plus a digit or symbol, at least 4 distinct characters, not containing the username
    or the email's local part. Invalid → 422 `{"detail": str}`.
  - Username or email already used (case-insensitive) → 409. More than 10 sign-ups per client IP per hour → 429.
  - Teachers follow env `FAIRPLAY_TEACHER_SIGNUP`: `open` (default in development) → active; `approval` (default when
    `FAIRPLAY_ENV=production`) → pending; `closed` → 403. Students can always sign up (they join classes with a code).
- Role `superadmin`: accepted in tokens; dependency `require_superadmin`. Teacher/student endpoints answer 403 to it.
  Dev seed: `admin` / `admin123` ("Platform Admin"), alongside the other dev users.

## Admin API (role superadmin; teachers and students get 403)
- `GET /api/admin/stats` → `{"teachers": {"active","pending","disabled","total"}, "students", "exams_created",
  "exams_held" (status live or closed = opened at least once), "attempts_submitted", "active_teachers_7d",
  "active_teachers_30d", "generated_at"}`. A teacher is active in a window when one of their exams was created or
  changed (edit/open/close/release bump `updated_at`) or a submitted attempt of theirs was graded/commented
  (`updated_at > submitted_at`) in it.
- `GET /api/admin/teachers` → `[{"id","username","name","email"|null,"status","created_at","last_login_at"|null,
  "exams","students_reached"}]` (pending first, then newest). `students_reached` = distinct students with an attempt.
- `POST /api/admin/teachers/{id}/approve` (pending → active), `/disable` (any → disabled), `/enable` (disabled → active)
  → the updated teacher row; not a teacher → 404; wrong current status → 409.

## Web
- `/register` (`?role=teacher|student` preselects the role): full name, email, username, password + confirmation, role.
  Linked as "Create account" from `/teacher/login` and `/student/login`; login pages show "Forgot password?" →
  "Contact your administrator to reset your password." Label "Username or email" (input ids unchanged: `#username`, `#password`).
- After sign-in on any login page the token is stored for the returned role and the app goes to that role's home:
  teacher → `/teacher`, student → `/student`, superadmin → `/admin`.
- `/admin/login`, `/admin` (RequireAuth role superadmin): top bar with name + Sign out, tiles, teacher table with
  Approve / Disable / Enable. Token key `proctora:token:superadmin`. Types: `SessionRole = Role | "superadmin"`.
# Classes (Google-Classroom style) — as implemented

Goal: put students in a class once, then give an exam to the whole class. Server: `server/app/classes/`
(models, service with the eligibility rule) + `server/app/routes_classes.py`. Web: `/teacher/classes`,
`/teacher/classes/:classId`, `/student/classes`; API module `web/src/api/classes.ts`.

## Model
- `classes`: id, teacher_id, name (1–120), section (course code, ≤ 64), term (≤ 64), description (≤ 2000),
  join_code (7 chars from `ABCDEFGHJKMNPQRSTUVWXYZ23456789` — no 0/O/1/I/L; unique; regenerable), join_enabled (default true),
  archived (default false), created_at.
- `class_members`: class_id, user_id, joined_at, added_by `"teacher"|"code"|"lms"`; unique (class_id, user_id). Only
  `role = student` users are ever members.
- `exam_classes`: exam_id, class_id; unique pair; both FKs `ON DELETE CASCADE` (deleting an exam drops its assignments).

## Eligibility rule (who may see and start an exam)
Precedence, first match wins (`app.classes.service.exam_gate` / `require_exam_access`):
1. **Explicit roster** (`scan_exam_roster` rows, `PUT /api/exams/{id}/roster`) → only those students.
2. **Assigned classes** (≥ 1 `exam_classes` row) → only members of *any* assigned class (union). Archived classes still count.
3. **Linked LMS course** (`course_link_id`, slice 5, unchanged) → only course members (403 "This exam is for <course> students").
4. **None of these** → open, as before: any student with the start code.
- Refused in 1/2 → `403 {"detail":"You are not in a class for this exam"}`. Applied to `POST /api/student/exams/{id}/start`
  (checked after "Exam is not open" and before the start code, so refused students do not burn wrong-code attempts) and
  `GET /api/student/exams/{id}/package`. Every other student endpoint is attempt-bound (own attempt only).
- `GET /api/student/exams`: exams restricted by 1/2 that the student is not in are hidden (unless the student already has
  an attempt). Each item gains `"class_names": [str]` — the student's classes the exam is assigned to (`[]` when none).
- Teacher UI shows "Not assigned to any class: any student with the start code can join." for case 4.
- Paper exams (Scan & Grade): `roster_of` uses the same order — `source` is `"picked"` (explicit roster) >
  `"classes"` (union of class members) > `"course"` > `"all"`. `PUT roster []` returns to the default (classes, else course, else all).

## Teacher API (role teacher; another teacher's class or exam → 404)
- `GET /api/classes[?archived=true]` → `[Class]` (active by default), by name. Class =
  `{"id","name","section","term","description","join_code","join_enabled","archived","created_at","member_count","exam_count"}`.
- `POST /api/classes` `{"name","section"?,"term"?,"description"?}` → 201 Class (blank name → 422).
- `GET /api/classes/{id}` → Class + `"students":[{"id","username","name","email"|null,"joined_at","added_by"}]` (by name) +
  `"exams":[{"id","title","status","course","delivery","scheduled_at","created_at"}]` (newest first).
- `PATCH /api/classes/{id}` any of `name, section, term, description, join_enabled, archived` → Class (archive = `{"archived":true}`).
- `POST /api/classes/{id}/join-code` → Class with a new code; the old code stops working.
- `POST /api/classes/{id}/members` `{"text": "alice\nbob@x.edu"}` and/or `{"entries": [...]}` (newline / comma / semicolon /
  tab separated, case-insensitive; matches `User.username`, and `User.email` when that column exists) →
  `{"added":[Student],"already":[Student],"not_found":[str],"class":Class}`. Nothing to add → 422. Teachers never match.
- `POST /api/classes/{id}/members/csv` `{"csv": "<file text>"}` — header row with `username`/`email`/`login`/`id` columns
  (+ optional `name` column used in the not-found label `"Name (id)"`), or a plain list (every cell tried) → same shape.
- `POST /api/classes/{id}/members/lms` `{"course_link_id"}` → copies the already-synced `CourseMember`s of the teacher's
  linked course (no LMS call); `added_by "lms"`. Unknown / other teacher's link → 404.
- `DELETE /api/classes/{id}/members/{user_id}` → 204 (not a member → 404).
- `GET /api/exams/{id}/classes` / `PUT /api/exams/{id}/classes {"class_ids":[...]}` (replace; any exam status) →
  `{"exam_id","classes":[{"id","name","section","term","archived","member_count"}],"student_count" (union),
  "roster_override": bool, "audience": "roster"|"classes"|"course"|"open"}`. A class id not owned → 404 "Class not found".
- `GET /api/gradebook[.csv]?class_id=` → only that class's students and the exams assigned to it (combinable with `course`).

## Student API (role student)
- `GET /api/student/classes` → `[{"id","name","section","term","description","teacher_name","joined_at",
  "exams":[{"id","title","course","status","scheduled_at","duration_min","delivery","attempt":null|{"attempt_id","status"}}]}]`
  — non-archived classes only; exams listed: live, closed, or drafts with a planned date (paper exams never).
- `POST /api/student/classes/join` `{"code"}` (spaces/dashes ignored, case-insensitive) →
  `{"id","name","section","term","teacher_name","already_member"}`. Unknown or archived class → 404
  "No class has this code. Check it with your teacher."; joining off → 403 "Joining this class is turned off. Ask your teacher."
  (already-members get 200). 10 wrong codes in 10 min → 429 "Too many wrong class codes, wait 10 minutes".

## Web
- Teacher nav "Classes": tiles (Active / Archived) + "New class"; class page: large join code with Copy, Regenerate
  (confirm), "Students can join with the code" switch; Add students (usernames/emails textarea, CSV file, "From LMS course"
  when linked courses exist) with a not-found list; searchable student table with Remove (confirm); assigned exams and
  "New exam for this class" (`/teacher/exams/new?class=<id>`, class preselected).
- Exam editor: "Assign to classes" checkbox list (saved with PUT `/classes` after the exam is saved) + the open-exam warning.
  Exam page: strip with assigned classes, union student count and "Change". Gradebook: "Class" filter.
- Student: nav "Classes" (join form + class cards with their exams); exam lists show the class names.
## Step verification (math/physics)
Code: `server/app/scan/mathverify.py` (LaTeX parsing, step chain, final answer, reconciliation, prompt v2, key
LaTeX), `server/app/scan/units.py` (pint units, dimensions, significant figures). One hook in
`ai_grade.suggest`: `prompt = mathverify.extend_prompt(build_prompt(...))` and
`mathverify.apply_async(parse_suggestion(out, q, ctx), out, q, ctx)` (worker thread, ≤ 10 s per answer, never
raises; on timeout the model's draft is kept). `FAIRPLAY_STEP_VERIFY=0` turns it off. Deps:
`antlr4-python3-runtime==4.11` (SymPy's LaTeX parser; a small built-in LaTeX→SymPy converter is used when it is
missing or fails) and `pint`.

- **Prompt `fp-step-grade-v2`** = v1 + per step `"latex"` (the student's line as LaTeX, mistakes kept, units as
  `\text{m/s}`, continuation lines start with `=`), `"ecf": bool`, top-level `"final_answer": {"latex","units"}`,
  per criterion `"steps": [n...]` (the steps it is judged on); the prompt also carries the key's final answer /
  worked solution as LaTeX, the ECF criteria and required sig figs. Parsing stays backward compatible: v1 outputs
  (no LaTeX) are checked from the plain step text; `prompt_version` is `fp-step-grade-v2` only when LaTeX came back.
- **Per step** (each against the last readable step): expressions algebraically equal (expand / simplify /
  random-point sampling, `mathcheck.check`); equations equivalent (same equation up to a constant factor, same real
  solution set for one unknown — "loses / adds solution" errors; physics keeps a dropped negative root; several
  letters: solved for a shared letter both ways = formula rearranged); `= …` lines compared with the previous
  value; claims inside a line (`3·4 = 12`, `\frac{d}{dx}(…) = …`, `f'(x)` / `y'` / `dy/dx` against an earlier
  `f(x) = …`, `\int_a^b … = …`, `\int … dx = F + C` by differentiating F); written decimals compared with their
  rounding; substituting values / new quantities / prose / inequalities / sums → `unverifiable`.
  Units come only from `\text{}` / `\mathrm{}` markup or the `units` fields: a unit becomes its SI factor × base-unit
  symbols, so `72 km/h = 20 m/s` is algebra and `5 m + 3 s` is an error ("adds m and s").
- **ECF**: after a `verified_error`, a later step that follows from the wrong line is `verified_ok` with
  `ecf: true` ("correct given the error in step k"); a step that is equivalent to the last correct line again
  clears it. A wrong final answer that follows from the error gets `final.ecf: true`.
- **Final answer** (`final_answer.latex`, else the last readable line) vs the key's `final_latex` (else a numeric
  question's `answer_number`): equivalent forms, solution sets (any order, count must match), vectors `(x, y)` /
  `\langle\rangle` / `\hat{i}`/`\mathbf{j}` component-wise, tolerance = the question's tolerance, the rounding of
  the student's decimals (only to ≈3 significant figures or finer) or of a decimal key; units converted (km/h vs
  m/s), "no units" / "wrong units" errors; significant figures when the key sets `sig_figs` or a criterion says
  "… N significant figures".
- **Reconcile** (verifier wins on algebra/arithmetic): a step the model called wrong but SymPy proves → `status`
  becomes `correct` (`model_status: "error"`, `verifier_disagrees` text); a step the model called right but SymPy
  refutes → `error`. Criteria with `steps` are re-scored from the checked steps: all `verified_ok` → full; ECF
  steps count only when the criterion has `ecf: true` (method marks); otherwise max × ok/total, rounded down to
  0.5; criteria with an unverifiable step keep the model's points (`criteria[i].model_points`, `verifier_note`).
  `points` = sum. A disagreement that changes points (or a wrong final answer / units / sig figs with full marks, a
  right final answer with 0, a re-scored criterion) adds review reason `verifier_disagrees` ("The math check
  disagrees with the AI") and caps confidence at 0.5; a disagreement that changes no points lowers it by 0.1; full
  agreement (checked steps + final matches the key, or a wrong final the model already marked down) raises it to
  ≥ 0.9 (no final: +0.15, ≤ 0.95). `first_error_step` is recomputed. Drafts from the key (`source: "key"`) keep
  their points.
- Record additions in `attempt.ai_drafts[qid]`:
  `steps[i] += {latex?, verify: "verified_ok"|"verified_error"|"unverifiable", verify_reason, ecf, model_status?,
  verifier_disagrees?}`; `criteria[i] += {steps?, ecf?, model_points?, verifier_note?}`;
  `final_answer: {latex, units, text} | null`;
  `verification: {engine: "sympy"|"sympy+pint", version: "fp-verify-v1", steps: [{n, status, reason, ecf, latex}],
  final: {status, reason, student, key, ecf} | null, units: {status, reason, student, key} | null,
  sig_figs: {required, given: n | [min, max] | null, ok} | null, summary: {verified_ok, verified_error, unverifiable},
  disagreements: [{n: step | "final" | "units" | "sig_figs" | "criteria", model, verifier, reason, changes_points}],
  checked (false = nothing was math: no per-step `verify` fields, no UI), agrees, confidence_before, points_before,
  seconds}` (or `{engine, version, error, ...}` if the check crashed).
  Approved grades copy the steps as before (the extra fields ride along).
- **Answer key** (`questions[qid]` in the key): `final_latex`, `units`, `solution_latex: [line]`, `sig_figs` (1–15 |
  null), `latex_source: "teacher"|"ai"`; criteria may carry `ecf: true`. `PUT …/answer-key/questions/{qid}` accepts
  `final_latex, units, solution_latex, sig_figs` (omitted = keep) and `criteria[].ecf`, and returns
  `latex_warnings: [text]` (LaTeX / unit that cannot be read). `POST /api/exams/{id}/answer-key/latex
  {question_ids?, overwrite?}` → the AI converts each key (worked solution + correct answer) to LaTeX once and
  stores it (`latex_source: "ai"`; questions that already have `final_latex` are skipped unless `overwrite`; mcq
  skipped); 409 when AI is off; response = the answer key + `converted, failed, latex_warnings: {qid: [text]}`.
  The rubric AI draft (`ai-draft`) also returns `final_latex, units, solution_latex`. `key_context` adds
  `key_final_latex, key_units, key_solution_latex, sig_figs`.
- **Web**: review screen steps show the LaTeX (KaTeX; unreadable LaTeX as monospace source), a badge next to the
  AI's mark (`✓ Proved` / `✓ ECF` / `✗ Error` / `? Can't check`) and a one-line disagreement; under the steps a
  "Math check" box (summary, final answer vs key, units, sig figs, point-changing disagreements). Answer key: "Math
  check (LaTeX)" fields (final answer with preview, units, sig figs, worked solution lines), an ECF checkbox per
  criterion and a "Key to LaTeX (math check)" button.

# Phase 3 — Grading quality (benchmark, AI-vs-human metrics, review routing, second marking)

Code: `server/app/quality/` (models, metrics, routing, graders, runner, importer, service, routes);
web: `web/src/pages/teacher/quality/`, `web/src/api/quality.ts`. All endpoints are role teacher and
scoped to the requesting teacher (another teacher's set / run / exam → 404), except the second-marker
endpoints, which only expose the answers assigned to the caller, blind.

## Data read from grading (owned by Phase 2)
- `attempt.ai_drafts[qid]`: `points` (or `suggested_points`), `confidence` 0..1, `needs_review`,
  `needs_review_reason` (or `review_reason`), `illegible`, `criteria` / `criterion_points` / `rubric_scores`
  (numbers or `{"points","max_points"}`), `mistake_tags`, `source` ("fallback" = no AI suggestion), `batch_id`.
- `attempt.grades[qid]`: `points`, `comment`, `ai_suggested_points`, `source` ("ai_accepted"|"ai_edited"|"manual"),
  `graded_by`, `graded_at`, `grading_seconds`, `batch_id`. **Missing = unknown**: AI points fall back to the
  stored draft's points; a missing `source` is derived (no suggestion → manual; final == suggested → ai_accepted;
  else ai_edited); answers without `grading_seconds` are left out of timing.
- Moderation writes the final grade: `grades[qid]` keeps its fields and gets `points`, `comment`, `graded_at`,
  `graded_by` (lead username), `source` (re-derived vs the AI suggestion), `moderated: true`,
  `moderation: {second_mark_id, first_points, first_by, second_points, second_by, moderated_points, moderated_by, at}`;
  `rubric_scores` is cleared when the points change. Score / needs_grading are recomputed.

## Metrics (`app.quality.metrics`; rates are fractions 0..1, points are numbers)
`Agreement = {n, exact, within_1, within_10pct, mae, mae_pct, bias, kappa, severe_rate, severe, mean_ai, mean_human}`.
exact = |ai−human| < 1e-6; within_1 ≤ 1 point; within_10pct ≤ 10 % of max; mae = mean |ai−human|;
bias = mean(ai) − mean(human) (> 0: AI more generous); kappa = quadratic weighted Cohen's kappa on integer marks
(round half up) over categories 0..ceil(largest max) of the group, null when undefined (n < 2 or one category);
severe = |ai−human| ≥ severe_fraction × max (default 0.25, `?severe_fraction=` 0.01..1). Breakdowns:
`[{...Agreement, key}]` by `question_type` and by `subject` (lower-case; production subject = exam course).
`Correction = {ai_suggested, accepted, edited, correction_rate, points_changed, mean_edit, mean_edit_pct}`
(mean_edit over edited suggestions, points). `Timing = {ai_assisted: T, manual: T, time_saved_fraction,
batches: [{batch, items, total_seconds, mean_seconds, ai_assisted}]}` with `T = {n, mean, median, total}` (seconds);
batch = grade/draft `batch_id`, else the exam id.

## Review routing (`app.quality.routing`)
`route_item(exam, attempt, qid, policy=None, weak=None) -> {"review": bool, "reasons": [{"code","message"}]}` —
pure (works on ORM rows or any objects/dicts with `questions, course, total_points` / `answers, grades, ai_drafts`);
`from app.quality import route_item`. Phase 2's review screen shows a "Check by hand" badge when `review` is true,
with the reason messages (or calls the HTTP endpoint below, which applies the exam's policy and benchmark weak areas).
Codes: `no_ai_suggestion`, `confidence_unknown`, `low_confidence`, `illegible_handwriting`, `ai_flagged`,
`points_out_of_range`, `criteria_inconsistent`, `near_boundary` (attempt total with the AI's points — other
answers: grade, else draft, else auto — within `boundary_margin` points of a boundary), `weak_question_type`,
`weak_subject` (benchmark kappa below `weak_kappa`, latest run per set pooled, groups with ≥ `weak_min_items`
items; subject matches the exam course or a question topic).
Policy defaults: `{"confidence_threshold":0.7,"review_illegible":true,"review_ai_flagged":true,
"boundary_margin":1,"grade_boundaries":[50,60,70,80,90],"check_criteria":true,"weak_kappa":0.6,
"weak_min_items":5,"review_without_ai":true}`; layers: built-in → teacher default → exam override
(table `quality_routing_policies`: teacher_id, exam_id "" = default, settings JSON).
Candidates = every answer with an AI draft, every paper answer (except bubbles read automatically) and every
essay / website item of submitted attempts.

- `GET|PUT /api/quality/routing-policy` (teacher default; PUT body = any subset of the policy; boundaries
  0..100 else 422) → `{policy, teacher_settings, defaults}`.
- `GET|PUT|DELETE /api/exams/{id}/quality/routing-policy` → `{policy, teacher_settings, defaults, exam_settings|null,
  inherits}` (DELETE = back to the teacher default).
- `GET /api/exams/{id}/quality/routing[?review_only=true]` → `{exam_id, policy, weak_areas:{types:{type:kappa},
  subjects:{subject:kappa}}, summary:{total, routed, routed_ungraded, by_reason:{code:n}}, items:[{attempt_id,
  anon_id, question_id, review, reasons, graded, ai_points, confidence}]}`.
- `GET /api/exams/{id}/quality/routing/{attempt_id}/{question_id}` → `{attempt_id, question_id, review, reasons}`.

## Benchmark
Tables: `benchmark_sets` (id `bs_…`, teacher_id, name, subject, consent_note, consent_basis, created_at);
`benchmark_items` (id `bi_…`, set_id, question_prompt, question_type, subject, max_points, rubric JSON,
answer_key, answer_image `/api/uploads/<id>` | null, answer_text, human_marks [num] (first = reference),
human_points, human_comment, source_exam_id / source_attempt_id / source_question_id (if copied), consent,
consent_by, consent_at, created_at — **no student name column**); `benchmark_runs` (id `br_…`, set_id, teacher_id,
grader, provider, model, prompt_version, status running|done|failed, severe_fraction, item_count, done_count,
metrics JSON, error, created_at, finished_at); `benchmark_run_items` (run_id, item_id, question_type, subject,
max_points, human_points, ai_points|null (failed), confidence, needs_review, explanation, error).

- `GET /api/quality/benchmarks` → `[Set]`, `Set = {id, name, subject, consent_note, consent_basis, created_at,
  item_count, last_run: Run|null}`; `POST` (201) body `{name, subject?, consent_note?, consent_basis?}`;
  `GET /api/quality/benchmarks/{sid}` → `Set + {items:[Item], runs:[Run]}`; `PUT` same body; `DELETE` → 204 (items, runs too).
- `Item = {id, set_id, question_prompt, question_type, subject, max_points, rubric, answer_key, answer_image,
  answer_text, human_marks, human_points, human_comment, source:{exam_id,attempt_id,question_id}|null, consent,
  consent_by, consent_at, created_at}`.
- `POST /api/quality/benchmarks/{sid}/items` (201) body `{question_prompt, question_type: mcq|numeric|short|essay|work|external,
  subject?, max_points, rubric?, answer_key?, answer_text?, answer_image?, human_marks:[1..5 marks], human_comment?,
  consent:true}`. Without `consent: true` → 422 (detail mentions consent); marks outside 0..max → 422.
- `POST /api/quality/benchmarks/{sid}/items/from-grade` (201) body `{exam_id, attempt_id, question_id, consent:true,
  human_comment?}` → copies prompt, type, points, rubric, answer key, answer (text; paper: crop URL + approved
  transcript), the teacher grade (+ the second mark if one exists) — never the student's name. 422 without consent,
  409 if not graded / blank / already in the set.
- `POST /api/quality/benchmarks/{sid}/import` multipart `file` (CSV ≤ 5 MB), optional `images` (zip ≤ 200 MB),
  `consent=true` (else 422) → `{imported, errors:[{row, error}], ignored_columns, dropped_personal_columns}`.
  Columns (aliases): question_prompt (prompt, question), question_type (type), subject, max_points (points),
  rubric (JSON list or `label: pts; label: pts`), answer_key, answer_text (answer), image (file name in the zip),
  human_mark (human_marks, mark, score: `3` or `3;4`), human_comment (comment). Name/email/username columns are dropped.
- `DELETE /api/quality/benchmarks/{sid}/items/{iid}` → 204.
- `GET /api/quality/graders` → `{graders:[name], default, ai_enabled}`.
- `POST /api/quality/benchmarks/{sid}/runs` (202) body `{grader?, severe_fraction?}` → `Run` (status running; runs in
  the background, 4 at a time). 409 if AI is off for an AI grader, the set is empty or a run is going; 422 unknown grader.
  `Run = {id, set_id, grader, provider, model, prompt_version, status, severe_fraction, item_count, done_count,
  metrics:{overall: Agreement, by_type, by_subject, failed, human_vs_human: Agreement|null}, error, created_at, finished_at}`.
- `GET /api/quality/benchmarks/{sid}/runs` → `[Run]` newest first; `GET /api/quality/runs/{rid}` → `Run + {set:{id,name,subject},
  items:[{item_id, question_type, subject, prompt, max_points, human_points, ai_points, diff, severe, confidence,
  needs_review, explanation, error}]}` (largest relative difference first).
- Graders (`app.quality.graders`): `async def grader(item) -> {"points", "confidence"?, "explanation"?, "needs_review"?}`
  with `item = {id, question:{type,prompt,points,rubric,options,answer_number,tolerance,accepted_answers,correct_index,answer_key},
  subject, answer_text, answer_image, image: bytes|None}` (never the human mark). `register_grader(name, fn,
  prompt_version="", needs_ai=True)`, `set_default_grader(name)` or env `FAIRPLAY_BENCHMARK_GRADER`. Built-in
  `default` uses `app.ai.complete_json` (prompt version `bench-default-1`). Phase 2 drops its grader in here.

## Quality reports (live production data)
- `GET /api/quality/overview?course=&exam_ids=&severe_fraction=` → `Block + {exams:[{exam_id, title, course, graded, n,
  exact, within_1, mae, kappa, severe_rate, bias, correction_rate, mean_seconds_ai, mean_seconds_manual}], courses,
  all_exams:[{exam_id,title,course}], weak_areas, benchmarks:[{set_id, name, run_id, finished_at, overall}]}`.
- `GET /api/exams/{id}/quality?severe_fraction=` → `Block + {exam_id, title, course, status, grades_released,
  questions:[{question_id,index,type,prompt,points,rubric,options,images}], routing: summary, moderation:{total, open, by_status}}`.
- `Block = {agreement: Agreement, by_type, by_question:[Agreement + {exam_id, question_id, question_index, prompt, type}],
  correction, timing, worst:[{exam_id, exam_title, attempt_id, anon_id, question_id, question_index, prompt, type,
  source, graded_by, confidence, ai_points, final_points, max_points, diff, severe}] (top 25 by |diff|/max),
  graded, severe_fraction}`. Pairs = graded answers with an AI suggestion (ai_suggested_points vs final points).
- `GET /api/exams/{id}/quality/graded` → `[{attempt_id, anon_id, question_id, question_index, prompt, type, max_points,
  suggested, final, source}]` (pickers).

## Second marking and moderation
Table `quality_second_marks` (id `sm_…`, exam_id, attempt_id, question_id unique together, lead_id, marker_id,
tolerance, reason selected|sample|routed, second_points/comment/at, first_points/comment/by/at (snapshot at
moderation), ai_points, moderated_points/comment/by/at, created_at). Lead status: `awaiting_second` →
(`awaiting_first` if no teacher grade yet) → `agreed` (|first − second| ≤ tolerance) | `needs_moderation` →
`moderated`. The first mark compared is the live `attempt.grades[qid]`.
- `GET /api/quality/second-marking/teachers` → `[{username, name}]` (other teacher accounts).
- `POST /api/exams/{id}/quality/second-marking` (201) body `{marker_username, question_ids?, sample_percent? (0..100],
  routed_only?, tolerance? (points; default 10 % of the question), seed?}` → `{created, skipped, ids, marker}`.
  Candidates: routed_only → answers routed to review; else routing candidates (+ every answered answer of the
  listed `question_ids`); filtered by `question_ids`; then a random `sample_percent` (ceil, seeded by `seed` or exam
  id). Existing assignments are skipped. Marker must be another teacher (self → 422, unknown/student → 404).
- `GET /api/exams/{id}/quality/second-marking` → `{exam_id, title, counts:{status:n}, open, items:[{id, attempt_id,
  anon_id, username, name, question_id, question_index, prompt, type, max_points, answer:{text,image}, marker:{username,name},
  tolerance, reason, status, first:{points,comment,graded_by,graded_at}|null, second:{points,comment,marked_at}|null,
  ai:{points,confidence,comment}|null, moderated:{points,comment,by,at}|null, created_at}]}`.
- `DELETE /api/exams/{id}/quality/second-marking/{sm_id}` → 204 (409 once moderated).
- `POST /api/exams/{id}/quality/second-marking/{sm_id}/moderate` body `{points, comment?}` (lead only) →
  `{id, status:"moderated", grade, score, percent}`; 409 unless status is needs_moderation; 422 points out of range.
- Marker (blind): `GET /api/quality/second-marking/queue` → `{items:[{id, exam_id, exam_title, course, anon_id,
  question:{question_id,index,type,prompt,points,rubric,options,images}, answer:{text,image},
  status:"to_mark"|"marked"|"closed", my_mark:{points,comment,marked_at}|null}], to_mark}` — never the first mark,
  the AI draft/transcript, the comparison result or the student's name. `PUT /api/quality/second-marking/{sm_id}/mark`
  body `{points, comment}` → same item shape (404 if not assigned to the caller, 409 once moderated, 422 out of range).
- `GET /api/exams/{id}/quality/release-check` → `{exam_id, moderation_open, by_status, ok}`.
- `POST /api/exams/{id}/release` (changed): after the pending-grading check, if any second-marked answer of the
  exam is not agreed/moderated and `force` is false → 409 `"<n> second-marked answers still need moderation: grades may change"`
  (the Results "Release anyway" button sends force).

## Web
Teacher nav "Grading quality" → `/teacher/quality` (overview across exams, course filter), `/teacher/quality/exams/:id`
(exam report, routing rules + routed answers, second-marking assignment, moderation cards),
`/teacher/quality/benchmarks`, `/teacher/quality/benchmarks/:id` (answers, add from graded answers with a consent
checkbox, add by hand, CSV + zip import, run), `/teacher/quality/runs/:id`, `/teacher/quality/second-marking`
(the second marker's blind queue). Charts follow the Insights style (one primary hue, values printed, table view).

# Graph and diagram questions (paper exams, AI grading)

A written-answer question (`type: "essay"`) may be marked as a hand-drawn **graph / chart / diagram**. It is
then graded against a **feature checklist** instead of steps/rubric. No `diagram` = today's behaviour.
Code: `server/app/scan/diagram_spec.py` (kinds, defaults, validation), `diagrams.py` (prompt, parsing, endpoints,
grade/student views), `diagram_cv.py` (OpenCV checks); web `src/api/diagrams.ts`, `src/components/diagrams/`.

## Spec (`question.diagram`, and the per-exam override `answer_key.questions[qid].diagram`)
```
{"kind": "function_graph" | "data_chart" | "geometry" | "physics_fbd" | "physics_circuit" | "physics_ray"
         | "chemistry_structure" | "labelled_diagram" | "flowchart",
 "features": [{"id": "f1", "key": "axes", "label": "Axes drawn and labelled", "points": 1,
               "expected": "free text", "points_xy": [[x, y], ...], "tolerance": 0.5}],   # points_xy/tolerance optional
 "reference_image": "/api/uploads/<id>" | null,   # teacher's correct drawing
 "notes": "free text for the AI"}
```
- Feature points add up to the question's points (422 otherwise); empty `features` = the kind's default checklist
  shared to the question's points (multiples of 0.5, largest remainder). Max 20 features, 50 points_xy each.
- On an essay with `diagram` the essay rubric is cleared (the checklist replaces it); on other types `diagram` is dropped.
- Teacher views of the exam include `diagram`; **student views never do** (`student_question` removes it).
- Effective spec = answer-key override, else the question's (rescaled if the question's points changed).
- `effective_criteria(q, entry)` returns the features as criteria (`{id, label, points, description: expected}`,
  source `"diagram"`), so the review screen's criterion toggles and `approve` with `criteria` work unchanged.
  `rubric_scores` stays null for these.

## API (role teacher)
- `GET /api/diagram-kinds` → `{"kinds": [{"kind", "label", "group", "hint", "features": defaults for 10 points}]}`
- `GET /api/exams/{id}/diagrams` → `{"exam_id", "kinds", "questions": [{"question_id", "index", "type", "prompt",
  "points", "diagram": effective spec | null, "source": "key" | "question" | null, "question_diagram"}]}`
- `PUT /api/exams/{id}/diagrams/{qid}` body `{"diagram": spec | null}` → one question row. Stores the override in
  the answer key (works after the exam was given); `null` removes the override. Essays only (422 otherwise).
- Scan queue questions carry `"diagram": effective spec | null`.

## Grading
- Prompt: the step prompt plus a checklist section (`fp-diagram-v1`, appended by the one hook in
  `ai_grade.build_prompt`): every feature with points, expected text and points to plot; asks per feature for
  `status` present/wrong/missing, `points`, `note`, `box` [x0,y0,x1,y1] or `point` [x,y] (fractions 0..1 of the
  student's image from its top-left), and `readings` (values read off the axes with their image spot).
- Images: the student's answer first; the reference drawing (if any) last (`scan.grading.draft_for`).
- Parsing (one hook in `parse_suggestion`) is strict: unknown feature ids dropped; matched by id, else label, else
  position; points clamped to [0, max]; `missing` with points and `wrong` with full points → 0 + reason
  `features_inconsistent`; a feature the model skipped → `not_assessed`, 0 points, reason `missing_features`;
  percentages accepted for coordinates, bad boxes dropped. Total = sum of features. The step grader's
  `missing_criteria` / `total_mismatch` / `no_steps` reasons do not apply.
- OpenCV checks (`diagram_cv.analyze`): axes (longest near-horizontal/vertical lines, ignoring a printed box
  border), tick marks + even spacing, filled dots (blobs thicker than the pen stroke), `plot_dots` = those in the
  first quadrant. `cross_check` compares: the model's readings vs. the expected points; axes present/missing vs.
  detected; "even scale" vs. tick spacing (data charts); expected points mapped to pixels through a linear fit of
  the model's readings vs. detected dots; readings that are not at a dot; points credited with no dot found.
  Any disagreement → reason `diagram_check_disagrees`, confidence ≤ 0.6. The checks never award points.
- Bulk-approve groups for drawings: identical feature verdicts + points (`"d:..."`), never when flagged.

## Records
Draft (`attempt.ai_drafts[qid]`) adds, besides `criteria` mirroring the features:
```
"diagram": {"kind", "kind_label",
            "features": [{"id","key","label","points","max_points","met",
                          "status": "present"|"wrong"|"missing"|"not_assessed","note","box","point"}],
            "readings": [{"label","x","y","at": [x,y]|null}],
            "check": {"ran","axes_found","dots_found","ticks_even","expected_points","points_matched",
                      "readings_on_dots","calibrated","agrees": bool|null,"issues": [text]},
            "reference_image", "prompt_version": "fp-diagram-v1"}
```
Grade (`attempt.grades[qid]`) adds `"diagram": {"kind","kind_label","features": [{"id","label","points",
"max_points","status","note","box","point"}],"reference_image"}` — status recomputed from the approved criteria
points (full → present, 0 → the AI's wrong/missing else wrong, otherwise `partial`); the AI note is dropped when
the teacher changed that feature's points. Student feedback (`GET /api/student/grades/{id}`) questions carry
the same `diagram` (approved grade only). No new DB columns (all JSON).

## Web
- Exam editor (essay) and answer-key step: "Answer is" select (written answer, or a kind grouped by subject),
  checklist editor with defaults sized to the points, expected text, points to plot + tolerance, "Fit to N pts",
  notes for the AI, reference drawing upload.
- Review screen: the drawing with ✓/✗ marks (numbered like the checklist) and the checklist with toggles and
  per-feature points (ticking recomputes the total and the mark), the AI's notes, the picture-check warnings,
  values the AI read, and a link to the reference drawing.
- Student feedback: the marked drawing, the checklist results and "See the correct drawing".
- E2E: `e2e/e2e_diagrams.py` (ports 8270/8271, fake vision model).
# Any-paper answer finding — as implemented

Goal: grade ANY handwritten paper (own answer sheets, notebook pages, phone photos), not only FairPlay
booklets. Code: `server/app/scan/segment.py` (OpenCV candidates + vision prompt + validation + crops),
`server/app/scan/grouping.py` (page headers, student grouping, roster fuzzy match),
`server/app/scan/any_paper.py` (table `scan_answer_sets`, pipeline step, teacher API). Web:
`web/src/pages/teacher/scan/RegionCheck.tsx` + `regionLogic.ts`, `web/src/api/anyPaper.ts`.
QR booklets and the manual "Map pages" step are unchanged and remain the fallbacks.

## Pipeline (upload `?unprinted=1` or phone capture "not printed"; AI on)
1. Pages are read and flattened as before (`pipeline.analyze_unprinted`, photos via `photo.rectify`).
2. `grouping.read_header` per page (prompt `fp-page-header-v1`, page ≤ 1000 px) →
   `{"name","student_id","first_page","page_number","confidence"}`.
3. `grouping.group_pages(headers, roster, pages_per_student|None)`: a page with a different name/ID or a
   first-page mark starts a new student, other pages continue the current one; strong duplicate matches are
   merged (pages sorted by the written page number); no names at all + mapping `pages_per_student` → chunks.
   Roster match (`match_roster`): exact username/ID → 1.0, else name similarity (accents folded, Cyrillic
   transliterated, token/initials aware, difflib). Picked when ≥ 0.72; sure when ≥ 0.88 with a 0.08 margin.
4. Per student, `segment.segment_pages(questions, page_images)` (below); one `AnswerSet` row is stored and
   applied: every question becomes a paper answer of that student (crop of exactly its region(s); an answer
   continued on the next page is stitched top-to-bottom into one image); the pages get `status "assigned"`,
   `page_no` = position in the set, reason "Answers found by AI". A set without a matched student leaves its
   pages `unmatched` ("Answers found — choose the student").
5. The existing AI grading drafts every answer (`draft_answers`). Questions without a region get
   `{"blank": true, "not_found": true, "whole_page": true, "image": <first page>}` → rule draft "No answer written".
With AI off nothing changes: pages wait for "Map pages" (or the teacher builds sets by hand, see API).

## `segment_pages(exam_questions, page_images, hint="") -> dict` (async, never raises)
`page_images`: one student's pages (bytes PNG/JPEG or arrays), any order, already flattened.
Pre-pass (`candidate_regions(img)`): adaptive + global ink threshold, ruled/margin lines removed
(morphology), text lines from the row ink profile, lines merged into blocks split at gaps > max(1.1 × median
line height, 1.8 % page height), ≤ 40 per page. Blocks are numbered across pages and drawn on the images sent
to the model (`fp-segment-v1`, ≤ 8 pages per call, long side 1400 px); the prompt lists every candidate's box.
The model answers with candidate numbers (or a raw box with its page, snapped to the candidates it covers).
`parse_segmentation` validates: question references resolved by id or by the number the student wrote
("2", "Q2", "2)", "#2", "2b" → a question whose prompt starts with "2b", else 2); unknown questions dropped;
boxes clamped to 0..1 (percent accepted), min side 0.01; two questions claiming the same box → the more
confident keeps it, flagged; confidence 0..1 (percent accepted, default 0.5).
```
{"prompt_version": "fp-segment-v1", "source": "ai"|"fallback",
 "pages": [{"index", "candidates": [{"n", "box"}]}],
 "regions": [{"id","question_id"|null,"page" (0-based),"box":[x0,y0,x1,y1],"confidence","crossed_out",
              "label" (as written),"order","source":"ai","needs_check" (confidence < 0.7)}],
 "missing": [qid], "name": {"text","student_id","page","box"|null,"confidence"}|null,
 "confidence" (lowest live region), "needs_check": bool,
 "reasons": ["low_confidence","missing","ai_off","ai_failed","no_questions","no_pages"]}
```
Crossed-out regions are kept (shown to the teacher) but never cropped into the answer.
Helpers: `region_answers(questions, regions, page_urls, page_keys=None, extra=None) -> {qid: answer}` (sync,
writes crops as uploads) and `await find_answers(exam_questions, page_urls, extra=None) -> (seg, answers)`.
Answer value: `{"image","page" (1-based, first part),"blank":false,"mcq":null,"marks":null,
"regions":[{"page","page_id"|"page_key","box"}],"region_confidence","found":"ai"|"teacher","set_id"}`.

## AnswerSet (`scan_answer_sets`) and teacher API (role teacher; other teacher's exam → 404)
- `GET /api/exams/{id}/scans/sets` → `{exam_id, ai_enabled, low_confidence_below, questions:[{question_id,index,
  prompt,type,points}], students (roster), sets:[Set], needs_check (count), loose_pages:[{page_id,seq,preview,batch_id}]}`.
  `Set = {set_id, batch_id, student_id, student:{id,username,name}|null, name_read, id_read,
  match:{confidence, candidates:[{student_id,name,username,score}], reasons, teacher}, status:"needs_check"|"ok"|"confirmed",
  needs_check, reasons:["no_student"|"check_student"|"low_confidence"|"missing"|"not_found_by_ai"],
  pages:[{page_id,seq,preview,image,candidates:[box]}], regions:[{id,question_id,page_id,box,confidence,crossed_out,
  label,source:"ai"|"teacher",needs_check}], missing:[qid], segment:{prompt_version,source,reasons,confidence,name}, updated_at}`.
- `PUT /api/exams/{id}/scans/sets/{set_id}` any of `{"student_id": id|null, "page_ids": [...], "regions": [{id?,question_id|null,
  page_id,box,crossed_out?,label?}], "confirm": bool}` → the list above + `queued` + `set`. Student taken by another set → 409;
  unknown question / box on a page outside the set / box too small → 422. Changed regions become `source "teacher"`,
  confidence 1. Only questions whose regions changed are re-cropped and re-drafted; a new student gets every question and
  the old student's not-yet-graded answers from this set are removed (an emptied paper attempt is deleted). Pages listed in
  `page_ids` are taken from other sets (those are rebuilt; an emptied set is deleted). Teacher grades are never touched.
- `POST /api/exams/{id}/scans/sets {"page_ids", "student_id"?}` → 201: new set from pages (split / loose pages), AI segments it.
- `POST /api/exams/{id}/scans/sets/{set_id}/resegment` → the AI finds the answers again (409 when AI is off).
- Scan queue items gain `"found": {"by","confidence","not_found","regions"}` (null for QR booklets / hand mapping).

## Web
Upload step: "Check answer regions" card (shown when unprinted uploads exist; "Map pages by hand instead" is folded below
when AI is on). Student chips (filter "Needs check (n)" / "All"; drop a dragged page on a chip to move it), page images with
coloured boxes labelled `Q<n> · <confidence>` (dashed = unsure, hatched = crossed out); drag to move, corner handles to
resize, "Draw" + drag to add, per-question "Not answered", selected-box panel (question, crossed out, delete), student
select with AI-read name and roster suggestions, "Move to…" per page, "Find again with AI". Keyboard: Tab/] [ boxes, 1–9
question, arrows move (Shift: resize), Delete, x crossed out, n not answered, Enter save + confirm + next, s save, j/k students.
Review screen shows "Answer region found by AI (n% sure)" or "No answer was found…".
