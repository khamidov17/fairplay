# P3 load test: a 300-student exam

Branch `lt-load-test` (from `fairplay-classroom` @ b46c3a2). Date 2026-10-14.

**Short answer.** In this container (2 vCPUs shared by the app, MariaDB and the load generator)
Classroom serves a 300-student exam **without a single error**, but **not with acceptable
latency**. Latency stays good up to about **200 concurrent students (~33 req/s)**. Past that the
app workers saturate the CPU and requests queue. The database, Redis and memory are not the limit.
The fixes in this branch cut the CPU cost per request by about 25% and the grade page from 5,493
queries to 46. On the same server config, p95 fell from 12 s to 5 s, and the worker-model change
recommended below brings it to 3.3 s. Absolute numbers from this box do not carry over to
production. Re-measure on real hardware (see the last section).

## Setup

| Part | How it ran here | Production (Dockerfile / docker-compose / entrypoint) |
|---|---|---|
| App server | gunicorn 26.2 `dmoj.wsgi`, `--worker-class gevent --workers 4`, venv `/tmp/claude-0/lt-venv` from the shared venv's freeze + `gunicorn[gevent]` | same (`docker/entrypoint.sh`); DB driver `mysqlclient` (installed by the Dockerfile) |
| Settings | `CLASSROOM_DEBUG=0`, `ALLOWED_HOSTS` set, `CONN_MAX_AGE` unset (0) | per-deploy `local_settings.py` (not in the repo) |
| Static files | not requested by the load generator | host nginx serves `/static/` |
| Cache / sessions | own Redis on 6392 (`RedisCache`), `cached_db` sessions | Redis on the host |
| DB | MariaDB 10.11 on the same box (default 128 MB buffer pool), slow log at 0.2 s | MariaDB/MySQL on the host |
| Load | Locust 2.46 `FastHttpUser`, same box (4–5% CPU) | n/a |
| Machine | 2 vCPU, 7 GB RAM, nothing else running | |

Data (`classroom/scripts/loadtest/seed.py`): course LT300 with one class of 300 students and a
teacher. The running exam `lt300exam` (format `exam`, results hidden) has 10 questions: 4 MCQ,
3 fill-in-the-blank and 3 short answer, with no code (so no judge) and no AI. There is also a
running homework `lt300hw`.

Scenario (`classroom/scripts/loadtest/locustfile.py`), per student:

1. Log in, open the exam, join and reload the exam page.
2. For each question:
   - open the workspace and its "my submissions" JSON;
   - save a draft twice, 15–30 s apart;
   - submit (the JSON submit the workspace uses);
   - poll the status once and reload the list.
3. Open the assignment submissions page and leave. Then start over.

The 300 students ramp up over 120 s ("everyone joins at 9:00") and then hold for 7 min. On top of
that, one teacher loops over the grade page, analytics and ranking every 10–20 s.
`run.sh` re-seeds before each run, flushes Redis, turns the slow log on and samples CPU/RSS with
`monitor.py`. `query_counts.py` counts the queries of each hot endpoint in-process.

Re-run: `python scripts/loadtest/seed.py --reset`, start the app server, then
`LT_PY=… LT_LOCUST=… bash scripts/loadtest/run.sh OUTDIR 300 120 9m`.

## Results under load

The table shows p50 / p95 / p99 in ms and the request rate. There were 0 failures in every run.
r1 and r3 use the production config (gevent ×4, `CONN_MAX_AGE=0`); r1 is before the fixes and r3
after them. r6 and r5 use the fixed code with `gthread` 4 workers × 4 threads: r6 without
persistent connections, r5 with `CONN_MAX_AGE=60`.

| Endpoint | r1 before | r3 after | r6 gthread | r5 gthread + CONN_MAX_AGE |
|---|---|---|---|---|
| POST login | 2500 / 6600 / 8700 | 1400 / 5500 / 7500 | 1400 / 6200 / 7000 | 1400 / 4700 / 6000 |
| GET exam page | 1300 / 23000 / 29000 | 470 / 15000 / 22000 | 770 / 5100 / 5900 | 630 / 4500 / 5200 |
| GET workspace | 2400 / 11000 / 17000 | 1600 / 6800 / 11000 | 1500 / 4500 / 5800 | 1100 / 3600 / 5000 |
| GET workspace submissions JSON | 700 / 2500 / 3600 | 490 / 1900 / 2800 | 810 / 3700 / 5000 | 440 / 2900 / 4300 |
| POST draft (autosave) | 9000 / 16000 / 21000 | 1800 / 7600 / 13000 | 830 / 3700 / 4800 | 450 / 3000 / 4200 |
| POST submit | 740 / 2200 / 2900 | 510 / 1500 / 2400 | 1100 / 4000 / 4900 | 750 / 3200 / 4700 |
| GET submission status | 440 / 1600 / 2200 | 330 / 1100 / 1600 | 870 / 3800 / 5200 | 480 / 3000 / 4200 |
| POST join | 120 / 2700 / 3500 | 81 / 1900 / 3300 | 290 / 4600 / 5800 | 260 / 4100 / 4700 |
| Teacher: grade page | 15000 / 34000 / 34000 | 4400 / 13000 / 13000 | 3600 / 8700 / 8700 | 5000 / 8900 / 8900 |
| Teacher: analytics | 10000 / 17000 / 17000 | 1700 / 3700 / 3700 | 3800 / 5700 / 5700 | 2600 / 5300 / 5300 |
| **All requests** | **1300 / 12000 / 20000, 27.5 req/s** | **760 / 5000 / 12000, 35.7 req/s** | **970 / 4100 / 5300, 36.0 req/s** | **610 / 3300 / 4600, 38.5 req/s** |
| Max latency | 35 s | 28 s | 8.7 s | 8.9 s |

Resources during the hold (averages; 100% = one core):

| Run | Whole box | gunicorn | MariaDB | Redis | Locust | gunicorn RSS (5 procs) | Slow queries > 0.2 s |
|---|---|---|---|---|---|---|---|
| r1 | 100% of 2 cores | 153% | 36% | 1.4% | 4% | 0.8 GB | 4 |
| r3 | 100% | 151% | 37% | 2.0% | 4% | 0.9 GB | 4 |
| r5 | 98% | 150% | 36% | 1.6% | 5% | 0.9 GB | 1 |

**Where the knee is.** In run r4 (fixed code, gevent) the 300 students ramped up slowly, over
8 min. The rolling p95 was 0.3–0.6 s up to ~200 users at ~33 req/s. From ~225 users on it rose to
2–8 s, while throughput stayed at ~34 req/s and the box sat at 100% CPU.

### What broke first

1. **App CPU.** Every run saturated both cores from about 70 users into the 2-minute join spike
   onward. Throughput is capped by CPU per request: about 56 ms per request before the fixes and
   about 42 ms after (app CPU ÷ req/s). The scenario asks for roughly 45–50 req/s at 300 students,
   so this box is about 40% short.
2. **The grade page made it worse.** One teacher load took 6.4 s of CPU and 5,493 queries, an
   N+1 over 1,820 submissions. That pinned a worker and everything queued behind it.
3. **Unfair queueing with gevent and mysqlclient.** `mysqlclient` is C code and gevent cannot make
   it cooperative, so a gevent worker stops at every query. Each worker still accepts as many
   connections as it likes. Requests that came in on a new connection therefore waited behind a
   busy worker instead of going to a free one. In r1 the autosave POST, which follows a 15–30 s
   idle gap and so always opens a new connection, had p50 9 s while the cheap JSON calls had
   0.7 s. Behind nginx every request is a new upstream connection, so production would see this
   on every request. With `gthread`, the max latency went from 28 s to 8.7 s and p99 from 12 s to
   5.3 s at the same throughput.
4. **Not the bottlenecks.** The database had at most 8 queries over 0.2 s per run, all 0.2–0.66 s,
   mostly `UPDATE judge_problem` from `Problem.update_stats` (see below). Redis, memory, connection
   counts and errors were all fine.

## Fixes (with tests in `judge/views/tests/test_load_performance.py`, all failing before)

These are single-request costs (`query_counts.py`, Django test client, idle box). After the runs
the exam held 2,471 submissions, against 1,820 in the "before" column for the grade page.

| Endpoint | Before | After |
|---|---|---|
| Grade page (teacher) | 5,493 queries, 6.4 s | **46 queries, 1.8 s** (with more submissions) |
| Exam page | 67 queries, 128 ms | 36 queries, 107 ms |
| Workspace (question page) | 86–96 queries, 135–186 ms | 63–72 queries, 137–150 ms |
| Workspace submissions JSON | 26–33 queries | 14–22 queries |
| Draft save | 31–34 queries | 25–26 queries |
| Submit | 73–77 queries | 63–67 queries |
| Submission status | 31–32 queries | 20 queries |
| Assignment submissions page | 44 queries | 23 queries |
| Join / leave | 32 / 27 queries | 21 / 16 queries |

1. **Grade desk N+1** (`judge/utils/contest_grade.py`). Each row loaded its source, its contest
   problem and its MCQ options or blanks one at a time. These are now `select_related` /
   `prefetch_related`, and the answer display sorts prefetched blanks in Python. The practice grade
   desk uses the same row query. Test: same query count for 2 and 24 submissions, and the answer
   text is unchanged.
2. **Question names.** On the workspace and the exam page, `translated_name()` ran one query per
   question. `Problem.prime_translated_names()` now loads them in one query.
3. **Scoreboard permission per row.** The exam page asked `can_see_full_scoreboard` once per
   question row, at 1–4 queries each. The answer is now memoized per contest instance and user.
   Test: the exam page costs the same with 4 and 10 questions.
4. **Contest access check for students** (`Contest.access_check`). This runs in the contest
   middleware on every request of a joined student. A class member of a visible, non-private,
   course-private assignment is now granted before the staff/teacher checks: 1 query instead of
   about 12. Every check after that point only grants, or refuses once all have failed, so the
   answers are the same. Tests cover a member, someone in the course but not the class, the
   teacher, a hidden assignment, and a private one without and with an invitation. The course-role
   checks (`is_viewable_by_org_admin`, `is_viewable_by_course_teacher`, linked courses, class
   membership) are memoized on the contest instance, which lives for one request, so the
   middleware and the draft/workspace views no longer repeat them. A test covers a fresh instance
   seeing a role change.
5. **`Problem.reserved_auto_type_labels()`** switched through every configured language
   (~6 ms) and ran 12 times per workspace page. It is now cached per process, keyed by active
   language and `LANGUAGES`. The catalogs do not change while a process runs.
6. **Analytics cache.** `CLASSROOM_ANALYTICS_CACHE_SECONDS = 30` in `dmoj/settings.py`. The page
   reads every submission and source, and compares every pair of students for code questions.
   It is now computed at most every 30 s per assignment, and the page shows "Computed at …" with a
   "Recompute now" link (`?refresh=1`). The editor check still runs before the cache is read. The
   setting `0` turns the cache off.
7. **`docker/entrypoint.sh`.** `GUNICORN_WORKER_CLASS` and `GUNICORN_THREADS` can now be set; the
   default is still gevent. Choosing the worker model is a deploy decision, so it is a
   recommendation here and not a change of default.

No grading, visibility or result-hiding rules changed. No migration was added (no missing index
showed up).

Tests run, by module, all OK:

- `test_load_performance` (19 tests incl. `test_contest_analytics`);
- `judge.models.tests.test_contest`, `test_contest_security`, `test_assignment_draft_api`,
  `test_contest_grade`, `test_contest_grade_variants`, `test_contest_grade_fixes`,
  `test_grade_entry` (262 tests);
- `test_exam_contest`, `judge.models.tests.test_problem`, `test_problem_workspace`,
  `test_hidden_results`, `test_fill_blank`, `test_mcq`, `test_organization_courses`,
  `test_problem_create_v2`, `test_contest_detail_timezone`, `test_contest_v2` (383 tests);
- `test_teacher_actions`, `test_teacher_finalize`, `test_p1_gap_fixes`, `test_p1_verification`,
  `test_full_crawl_fixes`, `test_click_through_fixes`, `test_submission_list`,
  `test_problem_list` (136 tests).

Before the fix, 8 of the new tests failed or errored. The two "answers unchanged" access tests
pass both before and after, as they should.

## Found, not fixed (needs a decision or more risk than this pass)

- **Submit recomputes the participation three times.** The `ContestSubmission` signal fires on
  create and again in `update_contest`, then `recompute_results` runs explicitly. That is about
  10 of the 63 queries per submit. It is grading code, so I left it.
- **`Problem.update_stats()` on every submit.** It does a full-row `UPDATE judge_problem` and a
  `COUNT DISTINCT` over all submissions to the problem. All 300 students write the same row, and
  this is the only query that showed up in the slow log (0.2–0.66 s, lock waits). This is DMOJ
  behaviour. It could be `save(update_fields=[...])` or moved to Celery for exam problems.
- **Draft save still does ~9 queries of staff/teacher checks** (`user_bypasses_assignment_rules`)
  per autosave. Course teachers can join live, so the check is needed. It could be stored on the
  participation at join time.
- **Workspace page: still 63–72 queries.** Tickets, clarifications, vote, solution, editable and
  tester ids, completed and attempted sets. Most of this is DMOJ problem-page context that a
  student in an exam never sees.
- **Every request** writes `judge_profile.last_access` (`LogUserAccessMiddleware`) and loads the
  user's permissions. Django's `RedisCache` builds a new client per cache call (about 20 per
  workspace page). django-compressor runs online: 3 blocks and 12 cache reads per page. Run
  `compress --force` with `COMPRESS_OFFLINE` in the image.
- **Login.** PBKDF2 at 600,000 iterations costs ~185 ms of CPU per password login (300 logins
  ≈ 1 CPU-minute). Login also makes a synchronous HaveIBeenPwned request, which fails open after a
  2 s timeout (`judge/utils/pwned.py`). Here the proxy refused it at once; on a server without
  egress each login could wait the full 2 s. Have students sign in a few minutes early, or via
  NetID.
- **Analytics with code questions** compares every pair of students per question, about 45,000
  pairs at 300 students, and tokenizes every source with Pygments. Not measured here (no judge).
  The 30 s cache bounds how often it runs, but one computation could take seconds. Measure it, and
  move it to Celery if it is slow.

## Recommended production settings

- **Workers.** Use `GUNICORN_WORKER_CLASS=gthread`, `GUNICORN_WORKERS` = number of cores
  (2× if they are idle-heavy), `GUNICORN_THREADS=4`. Do not use gevent with `mysqlclient`; if
  gevent is wanted for long-held requests, it needs PyMySQL, which costs more CPU per query. AI and
  PDF work belongs in Celery, so threads are enough.
- **DB connections.** Set `CONN_MAX_AGE = 60` and `CONN_HEALTH_CHECKS = True` in
  `local_settings.py`, but **only with gthread or sync workers**. Under gevent, Django's
  connections are per greenlet, so persistent ones pile up. The change gave +7% throughput and p50 970 → 610 ms
  (r6 → r5). MariaDB `max_connections` must cover workers × threads + Celery + bridge + margin;
  the default 151 is enough for ≤ 16 cores × 4 threads. Set `innodb_buffer_pool_size` to ≥ 1 GB
  (the default here was 128 MB).
- **Cache.** Keep Redis as the cache and `cached_db` sessions; Redis used ≤ 2% CPU.
- **Capacity.** After the fixes this scenario needs about 42 ms of app CPU plus ~10 ms of MariaDB
  CPU per request, and 300 students generate about 45–50 req/s. That is about 2–2.5 cores of app
  and 0.5 of DB at 100% load. For p95 under 1 s, plan about **4 dedicated cores for the app
  container**, with MariaDB on its own cores. Memory is not a concern: ~180 MB per worker.

## What must be measured again on real hardware

This box had 2 vCPUs shared by the app, the database and the load generator, MariaDB with
defaults, and no network, so absolute latency and throughput here are not production numbers.
On the real server, run `scripts/loadtest` with:

- the load generator on another machine, through the real nginx (one upstream connection per
  request);
- the chosen worker model and `CONN_MAX_AGE`, at 300 and 450 users;
- code questions with real judges, and AI grading on if it will be used;
- the end of the exam: Celery harvesting ~300 drafts at the deadline, plus a "last minute"
  submit spike. Neither was simulated here;
- file-upload questions (S3/Box);
- the teacher pages with code submissions (analytics similarity);
- real browsers, for the extra JS requests and websocket/event-daemon load.
