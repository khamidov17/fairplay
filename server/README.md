# FairPlay server (FastAPI)

API under `/api`, teacher UI at `/teacher`, student UI at `/student` (the built React app in `static/`).

## Run for development (SQLite, no Redis)
```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```
Open http://127.0.0.1:8000/teacher. Demo users (development only): `teacher / teacher123`, students `alice / alice123`, `bob / bob123`, `carol / carol123`.
Superadmin (development only): `admin / admin123` at http://127.0.0.1:8000/admin (approve teachers, usage counts).
New accounts: http://127.0.0.1:8000/register (sign in later with username or email).

## Production-shaped (Postgres + Redis + nginx, scalable)
```
export PROCTORA_SECRET=$(openssl rand -hex 32)
docker compose up --build --scale api=4
# first account (no demo users exist in production):
docker compose exec api python -m app.manage create-user --username jsmith --name "Jane Smith" --role teacher
```
`FAIRPLAY_ENV=production` (set in docker-compose.yml) refuses to start with a missing, dev or short
`PROCTORA_SECRET`, never creates demo users, sends no CORS headers unless `FAIRPLAY_CORS_ORIGINS` lists origins,
and adds HSTS. Without `REDIS_URL` it starts but warns loudly (rate limits, live room and token revocation then live
in one process: run a single worker).

## Accounts from the command line
```
python -m app.manage create-user --username U --name "Full Name" --role teacher   # prompts for the password
python -m app.manage set-password --username U        # also signs U out everywhere
python -m app.manage revoke-tokens --username U       # sign out everywhere
python -m app.manage unlock-login --username U        # clear a sign-in lockout
```
Add `--password-stdin` to read the password from stdin (scripts). Run with the same environment as the API.

## Settings (environment)
| Variable | Default | |
|---|---|---|
| DATABASE_URL | sqlite+aiosqlite:///./data/proctora.db | `postgresql+asyncpg://user:pw@host/db` in production |
| REDIS_URL | (none: in-memory, single process) | required with more than one worker |
| FAIRPLAY_ENV | development | `production`: strict checks, no demo users, same-origin CORS, HSTS |
| PROCTORA_SECRET | insecure dev value | token signing key; production needs ≥ 32 chars (`openssl rand -hex 32`) |
| FAIRPLAY_CORS_ORIGINS | `*` in development, none in production | comma-separated origins allowed to call the API from another site |
| DB_POOL_SIZE / DB_MAX_OVERFLOW | 10 / 20 | per worker |
| PROCTORA_SEED_LOADTEST_USERS | 0 | creates lt0001.. users for the load test |
| FAIRPLAY_ALLOW_LOADTEST_USERS | 0 | production only creates load-test users when this is also `1` |
| FORWARDED_ALLOW_IPS | 127.0.0.1 | (uvicorn) proxies whose X-Forwarded-For is trusted; set to the nginx address so audit rows and sign-in limits see real client IPs |
| FAIRPLAY_TEACHER_SIGNUP | open (approval when FAIRPLAY_ENV=production) | teacher self-registration: `open`, `approval` (a superadmin approves in /admin) or `closed`; students can always sign up |

## Security and audit (phase 1a)
- Sign-in limits: 10 failed sign-ins per username or 100 per IP in 15 min lock that username / IP for 15 min.
- Tokens carry `users.token_version`; a password change or "sign out everywhere" revokes older tokens.
- Invigilator exit code: a new random code per exam opening, shown in the teacher's live room only.
- Offline packages: the package is useless without a key the server releases only while the exam is open.
- Every exam session is recorded in `audit_events`; teachers see it as "Session timeline" on a submission.
See CONTRACT.md "Phase 1a" for the details and the threat model.

## Tests
```
pip install -r requirements-dev.txt && python3 -m pytest -q
```
Scale notes and load-test numbers: NOTES.md.
