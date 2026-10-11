#!/usr/bin/env python3
"""FairPlay benchmark: fixed scenarios, pass/fail thresholds, no-lost-answers check.

See loadtest/BENCHMARK.md for the definition. One command per scenario:

    # against a running server (seeded with PROCTORA_SEED_LOADTEST_USERS >= students)
    python3 loadtest/bench.py --scenario S1 --base http://127.0.0.1:8210 \
        --server-desc "1 uvicorn worker, SQLite, no Redis"

    # or let the bench start a fresh server itself (fresh DB, seeded users)
    python3 loadtest/bench.py --scenario S2 --spawn --port 8210 --workers 1 \
        --database-url sqlite+aiosqlite:////tmp/fp-bench.db

Writes loadtest/results/<scenario>-<UTC time>-<db>.json and .md and exits 0 on PASS.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import os
import platform
import random
import signal
import ssl
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(HERE)


# ---------------------------------------------------------------- scenarios (fixed)
@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    students: int
    exams: int
    ramp_s: float            # students log in and start spread over this window
    write_s: float           # writing time after the ramp (all end together: submit storm)
    p95_ms: float            # p95 threshold over all steady-state requests
    submit_p95_ms: float     # p95 threshold for the end-of-exam submit storm
    drop_after_s: float = 0  # S4: everyone loses the network this long into writing
    drop_s: float = 0        # S4: ... for this long, then all reconnect at the same instant
    reconnect_p95_ms: float = 0
    resync_max_s: float = 0  # S4: every student's pending answers saved within this


SCENARIOS = {
    "S1": Scenario("S1", "One class", 60, 1, ramp_s=20, write_s=90,
                   p95_ms=100, submit_p95_ms=1000),
    "S2": Scenario("S2", "Exam hall", 500, 10, ramp_s=30, write_s=90,
                   p95_ms=250, submit_p95_ms=2000),
    "S3": Scenario("S3", "Peak", 2000, 50, ramp_s=60, write_s=120,
                   p95_ms=1000, submit_p95_ms=3000),
    "S4": Scenario("S4", "Reconnect storm", 500, 10, ramp_s=30, write_s=90,
                   p95_ms=250, submit_p95_ms=2000,
                   drop_after_s=30, drop_s=20, reconnect_p95_ms=2000, resync_max_s=30),
}

HEARTBEAT_S = 5.0
CHANGE_S = 3.0              # a student changes one answer about every 3 s
LIVE_POLL_S = 5.0           # each exam's live room is open on a teacher screen
RETRY_BASE_S, RETRY_CAP_S, JITTER = 3.0, 30.0, 0.2   # same as web/src/lib/answerQueue.ts
REQ_TIMEOUT_S = 15.0        # same as STUDENT_REQUEST_TIMEOUT_MS

EXAM_BODY = {
    "duration_min": 180,
    "course": "BENCH 101",
    "shuffle_questions": True,
    "questions": [dict(q, topics=["bench"]) for q in (
        [{"type": "mcq", "prompt": f"MCQ {i}", "points": 1, "options": ["a", "b", "c", "d"],
          "correct_index": i % 4} for i in range(6)]
        + [{"type": "numeric", "prompt": f"Num {i}", "points": 2, "answer_number": i * 1.5,
            "tolerance": 0.1} for i in range(2)]
        + [{"type": "short", "prompt": f"Short {i}", "points": 2,
            "accepted_answers": [f"word{i}"]} for i in range(2)]
        + [{"type": "essay", "prompt": "Essay", "points": 10, "min_words": 50, "max_words": 800,
            "rubric": [{"label": "Argument", "points": 6}, {"label": "Clarity", "points": 4}]}])],
}
WORDS = ("the exam asks why load matters and the answer grows with every save "
         "ünïcödé 数学 математика o'zbekcha").split()


# ---------------------------------------------------------------- measurement
class Rec:
    """Per-process request log: (endpoint, phase, latency s, ok)."""

    def __init__(self):
        self.rows: list[tuple[str, str, float, bool]] = []
        self.err_samples: dict[str, str] = {}

    async def call(self, client, name, phase, method, url, ok=(200,), **kw):
        t0 = time.perf_counter()
        try:
            r = await client.request(method, url, **kw)
        except httpx.HTTPError as e:
            self.rows.append((name, phase, time.perf_counter() - t0, False))
            self.err_samples.setdefault(name, f"{type(e).__name__}: {e}"[:200])
            return None
        self.rows.append((name, phase, time.perf_counter() - t0, r.status_code in ok))
        if r.status_code not in ok:
            self.err_samples.setdefault(name, f"HTTP {r.status_code}: {r.text[:200]}")
            return None
        return r


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(p / 100 * len(xs) + 0.5)) - 1))
    return xs[k] * 1000


def summarize(rows):
    out = {}
    by = defaultdict(list)
    errs = defaultdict(int)
    for name, _phase, lat, ok in rows:
        by[name].append(lat)
        errs[name] += not ok
    for name in sorted(by):
        xs = by[name]
        out[name] = {"count": len(xs), "errors": errs[name], "p50_ms": round(pct(xs, 50), 1),
                     "p95_ms": round(pct(xs, 95), 1), "p99_ms": round(pct(xs, 99), 1),
                     "max_ms": round(max(xs) * 1000, 1)}
    allx = [r[2] for r in rows]
    out["ALL"] = {"count": len(allx), "errors": sum(errs.values()),
                  "p50_ms": round(pct(allx, 50), 1), "p95_ms": round(pct(allx, 95), 1),
                  "p99_ms": round(pct(allx, 99), 1),
                  "max_ms": round(max(allx) * 1000, 1) if allx else 0.0}
    return out


# ---------------------------------------------------------------- simulated student
def new_value(q, rnd):
    t = q["type"]
    if t == "mcq":
        return rnd.randrange(len(q["options"]))
    if t == "numeric":
        return round(rnd.uniform(-10, 10), 3)
    if t == "essay":
        return " ".join(rnd.choices(WORDS, k=rnd.randint(50, 400)))
    return rnd.choice(["word0", "word1", "nope", "Ünï 数学"])


async def student(idx, exam, sc: Scenario, t0: float, base: str, rec: Rec):
    """Mirrors the web client: snapshot saves with client_seq, heartbeat every 5 s,
    retries with backoff+jitter, full flush then submit at the end of the exam."""
    rnd = random.Random(idx * 7919 + int(t0))
    t_login = t0 + rnd.uniform(0, sc.ramp_s)
    t_end = t0 + sc.ramp_s + sc.write_s
    t_drop = t0 + sc.ramp_s + sc.drop_after_s if sc.drop_s else None
    t_back = t_drop + sc.drop_s if t_drop else None
    res = {"idx": idx, "exam_id": exam["id"], "username": f"lt{idx:04d}", "ok": False}
    await asyncio.sleep(max(0.0, t_login - time.time()))
    limits = httpx.Limits(max_connections=2)  # one browser tab: keep-alive connection(s)
    async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(REQ_TIMEOUT_S),
                                 verify=ssl.create_default_context(), limits=limits) as c:
        r = await rec.call(c, "login", "ramp", "POST", "/api/auth/login",
                           json={"username": res["username"], "password": "loadtest"})
        if r is None:
            return res
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        await rec.call(c, "student_exams", "ramp", "GET", "/api/student/exams", headers=h)
        r = await rec.call(c, "start", "ramp", "POST", f"/api/student/exams/{exam['id']}/start",
                           headers=h, json={"start_code": exam["start_code"]})
        if r is None:
            return res
        view = r.json()
        # seconds this student generates steady-state traffic (for the offered-load check)
        active = max(0.0, t_end - time.time())
        if t_back:
            active = max(0.0, active - sc.drop_s - 10)
        res["active_s"] = active
        aid, qs = view["attempt_id"], view["questions"]
        res["attempt_id"] = aid
        answers: dict = dict(view["answers"])
        seq = max(0, view.get("client_seq", 0))
        synced = seq
        failures = 0
        next_send = None            # when the (re)try is due; None = nothing pending
        next_hb = time.time()
        next_change = time.time() + rnd.uniform(0, CHANGE_S)
        resync_at = None            # S4: first successful save after reconnect
        reconnected = False

        def phase(now):
            if t_back and t_back <= now < t_back + 10:
                return "reconnect"
            return "steady"

        async def save(now):
            nonlocal synced, failures, next_send, resync_at, seq
            snap_seq, snap = seq, dict(answers)
            r = await rec.call(c, "save_answers", phase(now), "PUT",
                               f"/api/student/attempts/{aid}/answers", headers=h,
                               json={"answers": snap, "client_seq": snap_seq})
            if r is None:
                failures += 1
                d = min(RETRY_CAP_S, RETRY_BASE_S * 2 ** (failures - 1))
                next_send = time.time() + d * (1 - JITTER + 2 * JITTER * rnd.random())
                return
            body = r.json()
            failures = 0
            if body["saved"]:
                synced = max(synced, snap_seq)
                if t_back and resync_at is None and now >= t_back:
                    resync_at = time.time()
            elif body["client_seq"] >= snap_seq:  # someone else wrote: bump, resend
                seq = body["client_seq"] + 1
            next_send = time.time() if synced < seq else None

        while True:
            now = time.time()
            if now >= t_end:
                break
            offline = t_drop is not None and t_drop <= now < t_back
            if not offline and t_back and now >= t_back and not reconnected:
                reconnected = True
                # what the page does when the network is back: heartbeat (which nudges
                # the queue) and the queued save, all at the same instant for everyone
                await rec.call(c, "heartbeat", "reconnect", "POST",
                               f"/api/student/attempts/{aid}/heartbeat", headers=h,
                               json={"current_question": 0, "locked": True})
                if synced < seq:
                    await save(now)
                next_hb = time.time() + HEARTBEAT_S
            if now >= next_change:
                next_change += CHANGE_S * rnd.uniform(0.7, 1.3)
                q = rnd.choice(qs)
                if rnd.random() < 0.05:
                    answers.pop(q["id"], None)  # cleared an answer
                else:
                    answers[q["id"]] = new_value(q, rnd)
                seq += 1
                if next_send is None:
                    next_send = now + 1.0  # debounce 1 s
            if not offline:
                if now >= next_hb:
                    next_hb += HEARTBEAT_S
                    await rec.call(c, "heartbeat", phase(now), "POST",
                                   f"/api/student/attempts/{aid}/heartbeat", headers=h,
                                   json={"current_question": rnd.randrange(len(qs)), "locked": True})
                    if rnd.random() < 0.05:
                        await rec.call(c, "events", phase(now), "POST",
                                       f"/api/student/attempts/{aid}/events", headers=h,
                                       json={"events": [{"type": "blocked_shortcut", "detail": "Cmd-key48",
                                                         "client_time": "2026-01-01T00:00:00Z"}]})
                if next_send is not None and now >= next_send:
                    await save(now)
            wake = min(t_end, next_change, next_hb if not offline else t_back,
                       next_send if (next_send and not offline) else t_end)
            await asyncio.sleep(max(0.0, wake - time.time()))

        # end of exam: flush whatever is pending (bounded), then submit with the SAME seq
        # as the last save, so the server keeps what the saves stored (this checks the save
        # path: if a save had been lost the comparison below fails).
        deadline = time.time() + 60
        while synced < seq and time.time() < deadline:
            await save(time.time())
            if synced < seq:
                await asyncio.sleep(max(0.0, (next_send or time.time()) - time.time()))
        res["unsynced_at_submit"] = seq - synced
        receipt = None
        for attempt in range(6):
            r = await rec.call(c, "submit", "submit", "POST", f"/api/student/attempts/{aid}/submit",
                               headers=h, json={"answers": answers, "client_seq": seq})
            if r is not None:
                receipt = r.json()["receipt"]
                break
            await asyncio.sleep(RETRY_BASE_S * (1 + attempt))
        res.update(ok=receipt is not None, receipt=receipt, answers=answers, client_seq=seq,
                   resync_s=(resync_at - t_back) if (resync_at and t_back) else None,
                   needed_resync=bool(t_back))
        return res


async def teacher_poll(base, token, exams, rec: Rec, t_stop):
    async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(30)) as c:
        h = {"Authorization": f"Bearer {token}"}
        nxt = {e["id"]: time.time() + random.uniform(0, LIVE_POLL_S) for e in exams}
        while time.time() < t_stop:
            eid, due = min(nxt.items(), key=lambda kv: kv[1])
            await asyncio.sleep(max(0.0, due - time.time()))
            await rec.call(c, "live", "steady", "GET", f"/api/exams/{eid}/live", headers=h)
            nxt[eid] = due + LIVE_POLL_S


def run_shard(job):
    shard, idxs, exams, sc, t0, base = job
    try:
        import uvloop  # type: ignore
        uvloop.install()
    except ImportError:
        pass
    rec = Rec()

    async def go():
        return await asyncio.gather(*(student(i, exams[(i - 1) % len(exams)], sc, t0, base, rec)
                                      for i in idxs))

    results = asyncio.run(go())
    return rec.rows, rec.err_samples, results


# ---------------------------------------------------------------- setup + verification
def api(client, method, url, **kw):
    r = client.request(method, url, **kw)
    r.raise_for_status()
    return r.json() if r.content else None


def setup(base, sc):
    with httpx.Client(base_url=base, timeout=60) as c:
        tok = api(c, "POST", "/api/auth/login", json={"username": "teacher", "password": "teacher123"})["token"]
        h = {"Authorization": f"Bearer {tok}"}
        last = f"lt{sc.students:04d}"
        r = c.post("/api/auth/login", json={"username": last, "password": "loadtest"})
        if r.status_code != 200:
            sys.exit(f"user {last} cannot log in: start the server with "
                     f"PROCTORA_SEED_LOADTEST_USERS={sc.students} (or more) on a fresh database")
        exams = []
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        for i in range(sc.exams):
            e = api(c, "POST", "/api/exams", headers=h,
                    json={**EXAM_BODY, "title": f"Bench {sc.name} {stamp} #{i + 1}"})
            exams.append(api(c, "POST", f"/api/exams/{e['id']}/open", headers=h))
        return tok, exams


def verify(base, tok, exams, students):
    """The DB must hold exactly the answers each simulated student last sent."""
    h = {"Authorization": f"Bearer {tok}"}
    by_exam = defaultdict(list)
    for s in students:
        by_exam[s["exam_id"]].append(s)
    lost_answers, wrong_answers, missing_attempts, receipt_mismatch, not_submitted = [], [], [], [], []
    extra_attempts = 0
    receipts = []
    compared = 0
    with httpx.Client(base_url=base, timeout=120) as c:
        for e in exams:
            api(c, "POST", f"/api/exams/{e['id']}/close", headers=h)
            res = api(c, "GET", f"/api/exams/{e['id']}/results", headers=h)
            got = {a["username"]: a for a in res["attempts"]}
            mine = {s["username"]: s for s in by_exam[e["id"]]}
            extra_attempts += len(set(got) - set(mine))
            for u, s in mine.items():
                a = got.get(u)
                if a is None or not s.get("attempt_id"):
                    missing_attempts.append(u)
                    continue
                receipts.append(a["receipt"])
                if a["status"] != "submitted" or a["auto_submitted"]:
                    not_submitted.append(u)
                if s.get("receipt") != a["receipt"]:
                    receipt_mismatch.append(u)
                given = {x["question_id"]: x["given"] for x in a["answers"]}
                for qid, v in given.items():
                    want = s.get("answers", {}).get(qid)
                    compared += 1
                    if want != v:
                        (lost_answers if v is None else wrong_answers).append(
                            {"user": u, "question": qid, "expected": str(want)[:60], "stored": str(v)[:60]})
    dup_receipts = len(receipts) - len(set(receipts))
    ok = not (lost_answers or wrong_answers or missing_attempts or receipt_mismatch
              or not_submitted or dup_receipts or extra_attempts)
    return {"ok": ok, "students": len(students), "answers_compared": compared,
            "lost_answers": len(lost_answers), "wrong_answers": len(wrong_answers),
            "missing_attempts": len(missing_attempts), "not_submitted": len(not_submitted),
            "receipt_mismatch": len(receipt_mismatch), "duplicate_receipts": dup_receipts,
            "extra_attempts": extra_attempts,
            "samples": (lost_answers + wrong_answers)[:5] + [{"missing": m} for m in missing_attempts[:5]]}


# ---------------------------------------------------------------- machine + server
def machine():
    cpu = "unknown"
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        cpu = platform.processor() or "unknown"
    mem = None
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal"):
                mem = round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    return {"host": platform.node(), "os": platform.platform(), "cpu": cpu,
            "logical_cpus": os.cpu_count(), "ram_gb": mem, "python": platform.python_version(),
            "httpx": httpx.__version__}


class Spawned:
    def __init__(self, args, sc):
        self.args = args
        env = {**os.environ, "DATABASE_URL": args.database_url,
               "PROCTORA_SEED_LOADTEST_USERS": str(max(sc.students, args.seed_users)),
               "PROCTORA_PBKDF2_ITERATIONS": os.environ.get("PROCTORA_PBKDF2_ITERATIONS", "1000")}
        if args.redis_url:
            env["REDIS_URL"] = args.redis_url
        else:
            env.pop("REDIS_URL", None)
        import tempfile
        self.log_path = os.path.join(tempfile.gettempdir(), f"fp-bench-server-{args.port}.log")
        self.log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(args.port),
             "--workers", str(args.workers), "--log-level", "warning", "--no-access-log"],
            cwd=SERVER_DIR, env=env, stdout=self.log, stderr=self.log)
        for _ in range(600):
            try:
                if httpx.get(f"http://127.0.0.1:{args.port}/api/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("spawned server did not start")

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ---------------------------------------------------------------- report
def thresholds(sc, steady, submit_s, reconnect, verification, offered, achieved, resync):
    checks = [
        ("error rate = 0", steady["ALL"]["errors"] + submit_s["ALL"]["errors"]
         + (reconnect["ALL"]["errors"] if reconnect else 0) == 0,
         f'{steady["ALL"]["errors"]} steady + {submit_s["ALL"]["errors"]} submit'
         + (f' + {reconnect["ALL"]["errors"]} reconnect' if reconnect else "") + " errors"),
        (f"steady p95 <= {sc.p95_ms:.0f} ms", steady["ALL"]["p95_ms"] <= sc.p95_ms,
         f'{steady["ALL"]["p95_ms"]} ms'),
        (f"save p95 <= {sc.p95_ms:.0f} ms", steady.get("save_answers", {}).get("p95_ms", 0) <= sc.p95_ms,
         f'{steady.get("save_answers", {}).get("p95_ms", 0)} ms'),
        (f"submit storm p95 <= {sc.submit_p95_ms:.0f} ms", submit_s["ALL"]["p95_ms"] <= sc.submit_p95_ms,
         f'{submit_s["ALL"]["p95_ms"]} ms'),
        ("no lost answers (DB == what each student sent)", verification["ok"],
         f'{verification["answers_compared"]} answers compared, {verification["lost_answers"]} lost, '
         f'{verification["wrong_answers"]} wrong, {verification["missing_attempts"]} missing attempts, '
         f'{verification["duplicate_receipts"]} duplicate receipts'),
        ("offered load sustained (>= 90 %)", achieved >= 0.9 * offered,
         f"{achieved:.0f} of {offered:.0f} req/s"),
    ]
    if sc.drop_s:
        checks.append((f"reconnect burst p95 <= {sc.reconnect_p95_ms:.0f} ms",
                       bool(reconnect) and reconnect["ALL"]["p95_ms"] <= sc.reconnect_p95_ms,
                       f'{reconnect["ALL"]["p95_ms"] if reconnect else "n/a"} ms'))
        checks.append((f"all students resynced within {sc.resync_max_s:.0f} s",
                       resync["max_s"] is not None and resync["max_s"] <= sc.resync_max_s
                       and resync["missing"] == 0,
                       f'max {resync["max_s"]} s, p95 {resync["p95_s"]} s, {resync["missing"]} never'))
    return [{"check": n, "pass": bool(p), "value": v} for n, p, v in checks]


def table(rows):
    lines = ["| endpoint | count | errors | p50 ms | p95 ms | p99 ms | max ms |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for k, v in rows.items():
        lines.append(f"| {k} | {v['count']} | {v['errors']} | {v['p50_ms']} | {v['p95_ms']} | "
                     f"{v['p99_ms']} | {v['max_ms']} |")
    return "\n".join(lines)


def markdown(report):
    sc = report["scenario"]
    lines = [f"# {sc['name']} {sc['title']}: {'PASS' if report['pass'] else 'FAIL'}", "",
             f"Run {report['started_at']} ({report['wall_s']} s wall). "
             f"{sc['students']} students, {sc['exams']} exams, ramp {sc['ramp_s']} s, "
             f"writing {sc['write_s']} s"
             + (f", network drop at +{sc['drop_after_s']} s for {sc['drop_s']} s" if sc["drop_s"] else "")
             + ".", "",
             f"- Load generator: {report['machine']['cpu']}, {report['machine']['logical_cpus']} logical CPUs, "
             f"{report['machine']['ram_gb']} GB RAM, {report['machine']['os']}, "
             f"{report['client_procs']} client processes",
             f"- Server: {report['server']}",
             f"- Requests: {report['requests']}; offered {report['offered_rps']:.0f} req/s, "
             f"achieved {report['achieved_rps']:.0f} req/s (steady phase)", "",
             "## Checks", "", "| check | result | value |", "|---|---|---|"]
    for t in report["checks"]:
        lines.append(f"| {t['check']} | {'pass' if t['pass'] else '**FAIL**'} | {t['value']} |")
    lines += ["", "## Steady state (writing)", "", table(report["steady"]), "",
              "## Ramp (login, list, start)", "", table(report["ramp"]), "",
              "## End-of-exam submit storm", "", table(report["submit"])]
    if report.get("reconnect"):
        lines += ["", "## Reconnect burst (first 10 s after the network returns)", "",
                  table(report["reconnect"]), "",
                  f"Resync: p50 {report['resync']['p50_s']} s, p95 {report['resync']['p95_s']} s, "
                  f"max {report['resync']['max_s']} s, never {report['resync']['missing']}."]
    if report["error_samples"]:
        lines += ["", "## Error samples", ""] + [f"- {k}: `{v}`" for k, v in report["error_samples"].items()]
    v = report["verification"]
    lines += ["", "## Verification", "",
              f"{v['students']} students, {v['answers_compared']} answers compared with what each "
              f"student last sent: lost {v['lost_answers']}, wrong {v['wrong_answers']}, missing attempts "
              f"{v['missing_attempts']}, not submitted {v['not_submitted']}, receipt mismatch "
              f"{v['receipt_mismatch']}, duplicate receipts {v['duplicate_receipts']}."]
    if report.get("note"):
        lines += ["", "## Note", "", report["note"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--base", default=None, help="server URL (omit with --spawn)")
    ap.add_argument("--server-desc", default="", help="what the server is (workers, DB, Redis, machine)")
    ap.add_argument("--note", default="", help="free text recorded in the result")
    ap.add_argument("--procs", type=int, default=max(1, min(4, mp.cpu_count())),
                    help="client processes (the load generator itself needs CPU)")
    ap.add_argument("--students", type=int, help="override the scenario size (marks the run as non-standard)")
    ap.add_argument("--write-s", type=float, help="override writing time (non-standard)")
    ap.add_argument("--spawn", action="store_true", help="start a fresh server for the run")
    ap.add_argument("--port", type=int, default=8210)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--redis-url", default=None)
    ap.add_argument("--seed-users", type=int, default=0)
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    args = ap.parse_args()

    sc = SCENARIOS[args.scenario]
    standard = True
    if args.students:
        sc = Scenario(**{**asdict(sc), "students": args.students})
        standard = False
    if args.write_s:
        sc = Scenario(**{**asdict(sc), "write_s": args.write_s})
        standard = False
    os.makedirs(args.out, exist_ok=True)

    spawned = None
    if args.spawn:
        if not args.database_url:
            args.database_url = f"sqlite+aiosqlite:////tmp/fp-bench-{args.scenario}-{int(time.time())}.db"
        spawned = Spawned(args, sc)
        args.base = f"http://127.0.0.1:{args.port}"
        db = args.database_url.split(":", 1)[0].split("+")[0]
        server_desc = (f"spawned on this machine: {args.workers} uvicorn worker(s), {db}, "
                       f"{'Redis' if args.redis_url else 'no Redis (in-memory store)'}"
                       + (f"; {args.server_desc}" if args.server_desc else ""))
    else:
        if not args.base:
            sys.exit("--base or --spawn is required")
        server_desc = args.server_desc or "not described (pass --server-desc)"
        db = "sqlite" if "sqlite" in server_desc.lower() else (
            "postgres" if "postgres" in server_desc.lower() else "unknown")

    started = datetime.now(timezone.utc)
    try:
        tok, exams = setup(args.base, sc)
        t0 = time.time() + 2.0
        t_stop = t0 + sc.ramp_s + sc.write_s
        print(f"{sc.name} {sc.title}: {sc.students} students, {sc.exams} exams, ramp {sc.ramp_s}s, "
              f"write {sc.write_s}s, {args.procs} client processes -> {args.base}", flush=True)
        ids = list(range(1, sc.students + 1))
        jobs = [(k, ids[k::args.procs], exams, sc, t0, args.base) for k in range(args.procs)]
        poll_rec = Rec()

        async def run_all():
            loop = asyncio.get_running_loop()
            poller = asyncio.create_task(teacher_poll(args.base, tok, exams, poll_rec, t_stop))
            with mp.get_context("spawn").Pool(args.procs) as pool:
                shards = await loop.run_in_executor(None, pool.map, run_shard, jobs)
            await poller
            return shards

        shards = asyncio.run(run_all())
        wall = time.time() - t0
        rows = list(poll_rec.rows)
        samples = dict(poll_rec.err_samples)
        students = []
        for r, s, res in shards:
            rows.extend(r)
            for k, v in s.items():
                samples.setdefault(k, v)
            students.extend(res)
        verification = verify(args.base, tok, exams, students)
    finally:
        if spawned:
            spawned.stop()

    steady = summarize([r for r in rows if r[1] == "steady"])
    ramp = summarize([r for r in rows if r[1] == "ramp"])
    submit_s = summarize([r for r in rows if r[1] == "submit"])
    recon_rows = [r for r in rows if r[1] == "reconnect"]
    reconnect = summarize(recon_rows) if recon_rows else None
    # Offered steady load = what the clients should have sent if the server kept up:
    # heartbeats (+5 % events) + one save per answer change, over each student's active
    # time, + live-room polls. If the server is slow, clients wait and send less.
    per_student_rps = 1 / HEARTBEAT_S * 1.05 + 1 / CHANGE_S
    span = sc.ramp_s + sc.write_s
    expected = (sum(s.get("active_s", 0) for s in students) * per_student_rps
                + sc.exams * span / LIVE_POLL_S)
    offered = expected / span
    achieved = steady["ALL"]["count"] / span
    rs = [s["resync_s"] for s in students if s.get("needed_resync") and s.get("resync_s") is not None]
    resync = {"p50_s": round(pct(rs, 50) / 1000, 2) if rs else None,
              "p95_s": round(pct(rs, 95) / 1000, 2) if rs else None,
              "max_s": round(max(rs), 2) if rs else None,
              "missing": sum(1 for s in students if s.get("needed_resync") and s.get("ok")
                             and s.get("resync_s") is None)}
    checks = thresholds(sc, steady, submit_s, reconnect, verification, offered, achieved, resync)
    passed = all(c["pass"] for c in checks)
    report = {
        "benchmark": "fairplay-bench/1", "scenario": asdict(sc), "standard": standard,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "wall_s": round(wall, 1),
        "base": args.base, "server": server_desc, "machine": machine(), "client_procs": args.procs,
        "requests": len(rows), "offered_rps": round(offered, 1), "achieved_rps": round(achieved, 1),
        "steady": steady, "ramp": ramp, "submit": submit_s, "reconnect": reconnect,
        "resync": resync if sc.drop_s else None, "verification": verification,
        "error_samples": samples, "checks": checks, "pass": passed,
        "note": " ".join(x for x in (args.note, "" if standard else "NON-STANDARD size/duration override.") if x),
    }
    stem = f"{sc.name}-{started.strftime('%Y%m%dT%H%M%SZ')}-{db}"
    with open(os.path.join(args.out, stem + ".json"), "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    with open(os.path.join(args.out, stem + ".md"), "w") as f:
        f.write(markdown(report))
    print(markdown(report))
    print(f"wrote {os.path.join(args.out, stem)}.json/.md")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
