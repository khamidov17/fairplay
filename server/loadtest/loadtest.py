#!/usr/bin/env python3
"""Proctora load test: N simulated students across M live exams.

Start the server with PROCTORA_SEED_LOADTEST_USERS >= N so users lt0001..ltNNNN
(password "loadtest") exist. Then:

    python3 loadtest/loadtest.py --students 2000 --exams 50 --duration 60

Each student: login -> list exams -> start (with code) -> heartbeat every 5 s and
answer save every 3 s -> submit after --duration seconds. Logins/starts are spread
over --ramp seconds (a real exam room does not click "start" in the same millisecond).

Since the 2026-10-12 audit the server only lets the FairPlay app start: set
FAIRPLAY_APP_KEY to the server's key and each simulated student signs like the app
(challenge -> start -> X-FairPlay-Session on every request, app beat every 5 s).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import os
import multiprocessing as mp
import random
import ssl
import statistics
import time
from collections import defaultdict

import httpx

LAT: dict[str, list[float]] = defaultdict(list)
ERR: dict[str, int] = defaultdict(int)
ERR_SAMPLES: dict[str, str] = {}


async def call(client: httpx.AsyncClient, name: str, method: str, url: str, ok=(200,), **kw):
    t0 = time.perf_counter()
    try:
        r = await client.request(method, url, **kw)
    except httpx.HTTPError as e:
        LAT[name].append(time.perf_counter() - t0)
        ERR[name] += 1
        ERR_SAMPLES.setdefault(name, f"{type(e).__name__}: {e}")
        return None
    LAT[name].append(time.perf_counter() - t0)
    if r.status_code not in ok:
        ERR[name] += 1
        ERR_SAMPLES.setdefault(name, f"HTTP {r.status_code}: {r.text[:200]}")
        return None
    return r


EXAM_BODY = {
    "duration_min": 120,
    "course": "LOAD 101",
    "shuffle_questions": True,
    "questions": [dict(q, topics=["load"]) for q in (
        [{"type": "mcq", "prompt": f"MCQ {i}", "points": 1, "options": ["a", "b", "c", "d"],
          "correct_index": i % 4} for i in range(6)]
        + [{"type": "numeric", "prompt": f"Num {i}", "points": 2, "answer_number": i * 1.5,
            "tolerance": 0.1} for i in range(2)]
        + [{"type": "short", "prompt": f"Short {i}", "points": 2,
            "accepted_answers": [f"word{i}"]} for i in range(2)]
        + [{"type": "essay", "prompt": "Essay", "points": 10, "min_words": 50, "max_words": 800,
            "rubric": [{"label": "Argument", "points": 6}, {"label": "Clarity", "points": 4}]}])],
}
ESSAY_WORDS = "the exam asks why load matters and the answer grows with every save".split()


async def setup_exams(client, n_exams: int) -> list[dict]:
    r = await call(client, "login(teacher)", "POST", "/api/auth/login",
                   json={"username": "teacher", "password": "teacher123"})
    assert r is not None, ERR_SAMPLES
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    exams = []
    for i in range(n_exams):
        r = await call(client, "create_exam", "POST", "/api/exams", ok=(201,), headers=h,
                       json={**EXAM_BODY, "title": f"Load exam {i + 1}"})
        e = (await call(client, "open_exam", "POST", f"/api/exams/{r.json()['id']}/open",
                        headers=h)).json()
        exams.append(e)
    return exams, h


SSL_CTX = ssl.create_default_context()


async def student(idx: int, exam: dict, args):
    # One keep-alive connection per student, like a browser tab. (A single shared httpx
    # pool with thousands of connections makes the *client* the bottleneck.)
    await asyncio.sleep(random.uniform(0, args.ramp))
    async with httpx.AsyncClient(base_url=args.base, timeout=httpx.Timeout(60.0), verify=SSL_CTX,
                                 limits=httpx.Limits(max_connections=2)) as client:
        await _student(client, idx, exam, args)


def _sign(key: str, msg: str) -> str:
    return hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()


async def _student(client, idx: int, exam: dict, args):
    r = await call(client, "login", "POST", "/api/auth/login",
                   json={"username": f"lt{idx:04d}", "password": "loadtest"})
    if r is None:
        return
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    await call(client, "student_exams", "GET", "/api/student/exams", headers=h)
    key = os.environ.get("FAIRPLAY_APP_KEY", "").split(",")[0]
    device = f"loadtest-device-{idx:06d}"
    body: dict = {"start_code": exam["start_code"]}
    if key:
        c = await call(client, "challenge", "GET", f"/api/student/exams/{exam['id']}/challenge",
                       headers=h)
        if c is None:
            return
        nonce = c.json()["nonce"]
        body["client"] = {"device_id": device, "nonce": nonce, "version": "loadtest",
                          "signature": _sign(key, f"start|{nonce}|{device}")}
    r = await call(client, "start", "POST", f"/api/student/exams/{exam['id']}/start",
                   headers=h, json=body)
    if r is None:
        return
    a = r.json()
    aid, qs = a["attempt_id"], a["questions"]
    sess = a.get("session")
    if sess:
        h = {**h, "X-FairPlay-Session": sess}
    answers: dict = {}
    seq = 0
    t_end = time.monotonic() + args.duration
    next_hb = time.monotonic() + random.uniform(0, 5)
    next_save = time.monotonic() + random.uniform(0, 3)
    while True:
        now = time.monotonic()
        if now >= t_end:
            break
        if now >= next_hb:
            next_hb += 5
            await call(client, "heartbeat", "POST", f"/api/student/attempts/{aid}/heartbeat",
                       headers=h, json={"current_question": random.randrange(len(qs))})
            if key and sess:
                ts = int(time.time() * 1000)
                checks = "vm=0,displays=1,tap=1,input=1"
                await call(client, "app_beat", "POST", f"/api/student/attempts/{aid}/app-beat",
                           headers=h, json={
                               "session": sess, "device_id": device, "ts": ts, "locked": True,
                               "checks": checks,
                               "signature": _sign(key, f"beat|{aid}|{sess}|{device}|{ts}|1|{checks}")})
            if random.random() < 0.05:
                await call(client, "events", "POST", f"/api/student/attempts/{aid}/events",
                           headers=h, json={"events": [{"type": "blocked_shortcut",
                                                        "detail": "Cmd-key48",
                                                        "client_time": "2026-01-01T00:00:00Z"}]})
        if now >= next_save:
            next_save += 3
            q = random.choice(qs)
            answers[q["id"]] = (random.randrange(len(q["options"])) if q["type"] == "mcq"
                                else round(random.uniform(0, 10), 2) if q["type"] == "numeric"
                                else " ".join(random.choices(ESSAY_WORDS, k=random.randint(50, 400)))
                                if q["type"] == "essay"
                                else random.choice(["word0", "word1", "nope"]))
            seq += 1
            await call(client, "save_answers", "PUT", f"/api/student/attempts/{aid}/answers",
                       headers=h, json={"answers": answers, "client_seq": seq})
        await asyncio.sleep(max(0.0, min(next_hb, next_save, t_end) - time.monotonic()))
    await call(client, "submit", "POST", f"/api/student/attempts/{aid}/submit", headers=h,
               json={"answers": answers, "client_seq": seq + 1})


async def teacher_poll(client, h, exams, stop: asyncio.Event):
    """A few teachers watching their live rooms (poll every 3 s)."""
    while not stop.is_set():
        for e in random.sample(exams, min(5, len(exams))):
            await call(client, "live", "GET", f"/api/exams/{e['id']}/live", headers=h)
        try:
            await asyncio.wait_for(stop.wait(), 3)
        except asyncio.TimeoutError:
            pass


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = min(len(xs) - 1, max(0, int(round(p / 100 * len(xs) + 0.5)) - 1))
    return xs[k] * 1000


def run_shard(job):
    """Runs in a child process: simulate a slice of the students."""
    shard, idxs, exams, args = job
    random.seed(shard * 7919 + time.time_ns())
    try:
        import uvloop  # type: ignore
        uvloop.install()
    except ImportError:
        pass

    async def go():
        await asyncio.gather(*(student(i, exams[(i - 1) % len(exams)], args) for i in idxs))

    asyncio.run(go())
    return dict(LAT), dict(ERR), dict(ERR_SAMPLES)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--students", type=int, default=2000)
    ap.add_argument("--exams", type=int, default=50)
    ap.add_argument("--duration", type=float, default=60, help="seconds each student writes")
    ap.add_argument("--ramp", type=float, default=15, help="spread logins over this many seconds")
    ap.add_argument("--procs", type=int, default=max(1, min(4, mp.cpu_count())),
                    help="client processes (the load generator itself needs CPU)")
    args = ap.parse_args()

    async with httpx.AsyncClient(base_url=args.base, timeout=httpx.Timeout(60.0)) as client:
        t0 = time.monotonic()
        exams, th = await setup_exams(client, args.exams)
        print(f"setup: {len(exams)} exams opened in {time.monotonic() - t0:.1f}s; "
              f"{args.students} students in {args.procs} client processes")
        stop = asyncio.Event()
        poller = asyncio.create_task(teacher_poll(client, th, exams, stop))
        t1 = time.monotonic()
        ids = list(range(1, args.students + 1))
        jobs = [(k, ids[k::args.procs], exams, args) for k in range(args.procs)]
        loop = asyncio.get_running_loop()
        with mp.get_context("spawn").Pool(args.procs) as pool:
            shards = await loop.run_in_executor(None, pool.map, run_shard, jobs)
        wall = time.monotonic() - t1
        stop.set()
        await poller
        for e in exams:
            await call(client, "close_exam", "POST", f"/api/exams/{e['id']}/close", headers=th)
    for lat, err, samples in shards:
        for k, v in lat.items():
            LAT[k].extend(v)
        for k, v in err.items():
            ERR[k] += v
        for k, v in samples.items():
            ERR_SAMPLES.setdefault(k, v)

    total = sum(len(v) for v in LAT.values())
    errors = sum(ERR.values())
    print(f"\nstudents={args.students} exams={args.exams} duration={args.duration}s "
          f"ramp={args.ramp}s wall={wall:.1f}s requests={total} "
          f"throughput={total / wall:.0f} req/s")
    print(f"{'endpoint':<16}{'count':>8}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}{'max ms':>10}"
          f"{'errors':>8}")
    for name in sorted(LAT):
        xs = LAT[name]
        print(f"{name:<16}{len(xs):>8}{pct(xs, 50):>10.1f}{pct(xs, 95):>10.1f}{pct(xs, 99):>10.1f}"
              f"{max(xs) * 1000:>10.1f}{ERR[name]:>8}")
    allx = [x for v in LAT.values() for x in v]
    print(f"{'ALL':<16}{len(allx):>8}{pct(allx, 50):>10.1f}{pct(allx, 95):>10.1f}"
          f"{pct(allx, 99):>10.1f}{max(allx) * 1000:>10.1f}{errors:>8}")
    print(f"error rate: {errors / max(total, 1) * 100:.3f}%  "
          f"(mean {statistics.mean(allx) * 1000:.1f} ms)")
    for k, v in ERR_SAMPLES.items():
        print(f"  sample error [{k}]: {v}")


if __name__ == "__main__":
    asyncio.run(main())
