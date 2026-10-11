# FairPlay server: scale notes and load-test results

## How it scales
- **Stateless API.** Tokens are HMAC-signed (no session table, no DB hit to authenticate). Any request can go to any worker or machine, so capacity grows by adding uvicorn workers and API containers behind a load balancer.
- **Hot path off the database.** Heartbeats, live-room state, integrity-event counters, wrong-code counters/lockouts and cached exam content live in Redis. The live room is one Redis pipeline per poll, not a table scan.
- **Database writes are small and idempotent.** Answer saves are one conditional `UPDATE ... WHERE client_seq < :seq` (a late or duplicated save can never overwrite a newer one). Submit is idempotent and returns the same receipt. Unique `(exam_id, student_id)` prevents double attempts; all foreign keys are indexed.
- **Offline-first clients.** Students keep answers in local storage and retry, so a slow or briefly unavailable server never loses work; it just delays the sync.
- **Postgres in production, SQLite for dev** (same code, `DATABASE_URL`). Redis optional in dev (in-memory fallback, single process only).

## Load test (2026-10-11, this build)
Run on a 2-vCPU sandbox where the load generator, 2 API workers, Postgres and Redis all shared the same 2 cores. Each simulated student: login, list exams, start with code, heartbeat every 5 s, save answers every 3 s, submit.

| Run | Students / exams | Requests | Errors | p50 | p95 | p99 |
|---|---|---|---|---|---|---|
| A | 500 / 20 | 18,480 | 0 | 8 ms | 23 ms | 36 ms |
| B | 2,000 / 50 (all at once) | 66,738 | 0 | 2.2 s | 8.2 s | 14 s |

Run A is the healthy per-core figure: about 230 req/s on roughly one core with p95 23 ms. Run B saturated the 2 shared cores (2,000 students need about 1,100 req/s); nothing failed, everything queued, and the offline-first client hides that delay from students. Throughput there was 421 req/s.

**Sizing rule of thumb:** one student generates about 0.55 req/s (heartbeat 0.2 + saves 0.33). One API core handles about 250-400 req/s at low latency, so plan **~1 API core per 500 concurrent students**, plus Postgres and Redis on their own machines. 10,000 simultaneous students ≈ 20 API cores (e.g. 5 containers × 4 workers).

Reproduce:
```
PROCTORA_SEED_LOADTEST_USERS=2000 DATABASE_URL=postgresql+asyncpg://... REDIS_URL=redis://... \
  uvicorn app.main:app --port 8100 --workers 4
python3 loadtest/loadtest.py --base http://127.0.0.1:8100 --students 2000 --exams 50 --duration 90 --ramp 30
```

## Phase 1a hot-path cost
- Every authenticated request now does one store GET (`tv:<user>`: token-version check; a DB read only on a cache
  miss, once per user per 10 min). Tokens stay stateless otherwise.
- Answer save: + 2 store ops (audit coalescing); a DB row only once per attempt per 5 min.
- Heartbeat: + 1 store GETSET; DB rows only after a > 30 s gap.
- Login: + 1 audit INSERT and 2-4 store ops (sign-in limits).

## Known gaps before a real pilot
- Docker files are validated (`docker compose config`) but the image was not built here (no Docker daemon in the sandbox).
- Accounts: demo users in development only; production accounts come from `python -m app.manage create-user`.
  Canvas LTI 1.3 login is the next step.
- The Mac app's dev safety unlock (180 min, `SafetyUnlockMinutes`) is still on by default; set it to 0 for real exams.
- Upload URLs (question images, scan crops) are unguessable capabilities, not per-user checked (see CONTRACT.md).
