"""E2E phase 1b: no answer is lost when the network or the server goes away.

Scenarios (each on its own exam, student alice, a real Chromium):
  A  offline mid-exam: Wi-Fi drops (context.set_offline), the student keeps answering,
     the header shows the offline state, a reload is attempted while offline, then the
     network comes back and the page is opened again: every answer reaches the server
     with its final value (checked through the teacher API, not the student's browser).
  B  API down but the page reloads (server answers static files, /api fails): the exam
     resumes from the copy on this computer, the student keeps answering, then syncs.
  C  server restart mid-exam (uvicorn killed and restarted on the same SQLite file): the
     student keeps answering during the outage and nothing is lost; submit works after.
  D  submit while offline: the app keeps retrying; on reconnect the submit goes through
     exactly once, one receipt, shown on the done page and verifiable by the teacher.
  E  the submit reaches the server but its response is lost: the retry gets the SAME
     receipt (idempotent submit), the done page shows it.
  F  requests hang instead of failing (half-open TCP after a Wi-Fi switch): the client
     times them out and recovers; a hung submit is retried and goes through once.

The script starts its own server (so it can restart it) unless FAIRPLAY_BASE is set,
in which case scenario C is skipped.

  cd web && npx vite build --outDir /tmp/fp-static-rel
  PROCTORA_STATIC_DIR=/tmp/fp-static-rel python3 e2e/e2e_network.py
  # or against a running server (no restart scenario):
  FAIRPLAY_BASE=http://127.0.0.1:8210 python3 e2e/e2e_network.py

FAIRPLAY_ONLY=A,D runs a subset. Exit code 0 = all scenarios passed.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from playwright.sync_api import Error as PwError
from playwright.sync_api import expect, sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(os.path.dirname(HERE), "server")
PORT = int(os.environ.get("FAIRPLAY_PORT", "8211"))
EXTERNAL = bool(os.environ.get("FAIRPLAY_BASE"))
BASE = os.environ.get("FAIRPLAY_BASE", f"http://127.0.0.1:{PORT}").rstrip("/")
SHOTS = os.environ.get("FAIRPLAY_SHOTS", os.path.join(tempfile.gettempdir(), "fp-e2e-network-shots"))
os.makedirs(SHOTS, exist_ok=True)

ESSAY = ("Ünïcödé essay — 数学 — математика — o'zbekcha 🧮. "
         "Line two has \"quotes\" and a backslash \\ and a tab\tend.")


# ---------------------------------------------------------------- server control
class Server:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.db = os.path.join(tempfile.mkdtemp(prefix="fp-e2e-net-"), "fp.db")
        self.log = open(os.path.join(SHOTS, "server.log"), "ab")

    def start(self):
        static = os.environ.get("PROCTORA_STATIC_DIR")
        if not static or not os.path.exists(os.path.join(static, "index.html")):
            sys.exit("Set PROCTORA_STATIC_DIR to a built web app (cd web && npx vite build --outDir /tmp/fp-static-rel)")
        env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{self.db}",
               "PROCTORA_STATIC_DIR": static}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level", "warning"],
            cwd=SERVER_DIR, env=env, stdout=self.log, stderr=self.log)
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{BASE}/api/health", timeout=1)
                return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("server did not start")

    def kill(self):
        if self.proc:
            self.proc.send_signal(signal.SIGKILL)  # a crash, not a graceful shutdown
            self.proc.wait()
            self.proc = None


# ---------------------------------------------------------------- teacher API
def api(method, path, token=None, body=None):
    req = urllib.request.Request(f"{BASE}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def teacher_token():
    s, r = api("POST", "/api/auth/login", body={"username": "teacher", "password": "teacher123"})
    assert s == 200, r
    return r["token"]


EXAM = {
    "duration_min": 60,
    "questions": [
        {"type": "mcq", "prompt": "Pick C", "points": 1, "options": ["A", "B", "C", "D"],
         "correct_index": 2, "topics": ["net"]},
        {"type": "numeric", "prompt": "pi to 2 places", "points": 1, "answer_number": 3.14,
         "tolerance": 0.005, "topics": ["net"]},
        {"type": "short", "prompt": "Capital of Uzbekistan", "points": 1,
         "accepted_answers": ["Tashkent", "Toshkent"], "topics": ["net"]},
        {"type": "essay", "prompt": "Write anything", "points": 5, "rubric": [], "topics": ["net"]},
    ],
}


def new_live_exam(tok, title):
    s, e = api("POST", "/api/exams", tok, {**EXAM, "title": title})
    assert s == 201, e
    s, e = api("POST", f"/api/exams/{e['id']}/open", tok)
    assert s == 200, e
    return e


def server_answers(tok, exam_id, username="alice"):
    """What the server holds, by question index, read through the TEACHER API."""
    s, res = api("GET", f"/api/exams/{exam_id}/results", tok)
    assert s == 200, res
    a = next(x for x in res["attempts"] if x["username"] == username)
    return a, [x["given"] for x in a["answers"]]


# ---------------------------------------------------------------- student UI
def login(pg, user="alice", pw="alice123"):
    pg.goto(f"{BASE}/student/login")
    pg.fill("#username", user)
    pg.fill("#password", pw)
    pg.get_by_role("button", name="Sign in").click()
    pg.wait_for_url(re.compile(r".*/student(/)?$"))


def start_exam(pg, exam):
    pg.goto(f"{BASE}/student/exams/{exam['id']}/checkin")
    for i, d in enumerate(exam["start_code"]):
        pg.get_by_label(f"Digit {i + 1} of 4").fill(d)
    pg.get_by_role("button", name="Start exam", exact=True).click()
    pg.wait_for_url(re.compile(r".*/student/exam/a_.+"))
    pg.get_by_text("Question 1 of 4").first.wait_for()
    return pg.url.rsplit("/", 1)[-1]


def goto_q(pg, n):
    """Go to question n (1-based) with Previous/Next."""
    for _ in range(8):
        txt = pg.locator(".stu-question-meta b").inner_text()
        cur = int(re.search(r"Question (\d+)", txt).group(1))
        if cur == n:
            return
        pg.get_by_role("button", name=re.compile("^(Next|Previous)$")).filter(
            has_text="Next" if cur < n else "Previous").click()
    raise AssertionError(f"could not reach question {n}")


def answer(pg, n, value):
    goto_q(pg, n)
    if n == 1:
        opt = pg.get_by_role("radio", name=value, exact=True)
        if opt.get_attribute("aria-checked") != "true":
            opt.click()
    elif n == 4:
        pg.locator("textarea").fill(value)
    else:
        pg.locator("main input[type=text]").fill(value)


def wait_synced(pg, timeout=40_000):
    expect(pg.get_by_text("All answers saved")).to_be_visible(timeout=timeout)


def expect_pending(pg):
    expect(pg.get_by_text("Saved on this computer")).to_be_visible(timeout=5_000)


def submit_via_ui(pg):
    goto_q(pg, 4)
    pg.get_by_role("button", name="Review and submit").click()
    pg.get_by_role("button", name="Submit exam").click()
    pg.get_by_role("button", name="Yes, submit").click()


def done_receipt(pg, timeout=40_000):
    pg.wait_for_url(re.compile(r".*/student/done/.+"), timeout=timeout)
    rc = pg.get_by_test_id("receipt")
    expect(rc).to_have_text(re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$"), timeout=10_000)
    return rc.inner_text()


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("   ok:", msg)


# ---------------------------------------------------------------- scenarios
def scenario_a(b, tok):
    print("A  offline mid-exam, reload attempt while offline, reconnect")
    exam = new_live_exam(tok, "Net A offline")
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    login(pg)
    start_exam(pg, exam)
    exam_url = pg.url
    answer(pg, 1, "B")
    answer(pg, 2, "3.1")
    wait_synced(pg)
    _, given = server_answers(tok, exam["id"])
    check(given == [1, 3.1, None, None], f"online answers on the server {given}")

    ctx.set_offline(True)
    answer(pg, 1, "C")
    answer(pg, 2, "3.14")
    answer(pg, 3, "Toshkent")
    answer(pg, 4, ESSAY)
    expect_pending(pg)
    pg.wait_for_timeout(7000)  # several retry rounds while offline
    expect_pending(pg)
    pg.screenshot(path=f"{SHOTS}/A1-offline.png", full_page=True)
    _, given = server_answers(tok, exam["id"])
    check(given == [1, 3.1, None, None], "server unchanged while offline")

    reload_ok = True
    try:
        pg.reload(timeout=8000)
    except PwError as e:
        reload_ok = False
        print("   note: reload while offline fails in a plain browser (no service worker):",
              str(e).splitlines()[0][:90])
    if reload_ok:
        print("   note: the page reloaded while offline")

    ctx.set_offline(False)
    pg.goto(exam_url)
    pg.get_by_text("Question 1 of 4").first.wait_for(timeout=15_000)
    wait_synced(pg)
    _, given = server_answers(tok, exam["id"])
    check(given == [2, 3.14, "Toshkent", ESSAY], "after reconnect every answer has its final value")
    goto_q(pg, 4)
    check(pg.locator("textarea").input_value() == ESSAY, "essay typed offline still on screen after reopening")
    submit_via_ui(pg)
    rc = done_receipt(pg)
    a, given = server_answers(tok, exam["id"])
    check(a["status"] == "submitted" and a["receipt"] == rc, f"submitted, receipt {rc} matches")
    check(given == [2, 3.14, "Toshkent", ESSAY], "submitted answers are the final ones")
    ctx.close()


def scenario_b(b, tok):
    print("B  API unreachable, page reload resumes from this computer, then syncs")
    exam = new_live_exam(tok, "Net B api down")
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    login(pg)
    start_exam(pg, exam)
    answer(pg, 1, "A")
    wait_synced(pg)
    blocked = {"n": 0}

    def block(route):
        blocked["n"] += 1
        route.abort("connectionrefused")

    pg.route("**/api/**", block)
    answer(pg, 2, "2.71")
    answer(pg, 3, "Samarkand")
    expect_pending(pg)
    pg.reload()
    pg.get_by_text("Question 1 of 4").first.wait_for(timeout=15_000)
    expect_pending(pg)
    goto_q(pg, 3)
    check(pg.locator("main input[type=text]").input_value() == "Samarkand",
          "after reload with the API down, answers come from this computer")
    answer(pg, 3, "Tashkent")
    answer(pg, 4, ESSAY)
    pg.wait_for_timeout(4000)
    check(blocked["n"] > 0, f"{blocked['n']} API calls failed while it was down")
    _, given = server_answers(tok, exam["id"])
    check(given == [0, None, None, None], "server unchanged while API down")
    pg.unroute("**/api/**")
    wait_synced(pg, timeout=60_000)
    _, given = server_answers(tok, exam["id"])
    check(given == [0, 2.71, "Tashkent", ESSAY], "after the API is back every answer has its final value")
    ctx.close()


def scenario_c(b, tok, srv):
    print("C  server killed and restarted on the same database mid-exam")
    exam = new_live_exam(tok, "Net C restart")
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    login(pg)
    start_exam(pg, exam)
    answer(pg, 1, "D")
    answer(pg, 2, "1")
    wait_synced(pg)
    srv.kill()
    answer(pg, 2, "3.14")
    answer(pg, 3, "Tashkent")
    answer(pg, 4, ESSAY)
    expect_pending(pg)
    pg.wait_for_timeout(5000)
    pg.screenshot(path=f"{SHOTS}/C1-server-down.png", full_page=True)
    srv.start()
    tok = teacher_token()  # tokens are HMAC-signed: the old one would also still work
    wait_synced(pg, timeout=60_000)
    _, given = server_answers(tok, exam["id"])
    check(given == [3, 3.14, "Tashkent", ESSAY], "after restart every answer has its final value")
    submit_via_ui(pg)
    rc = done_receipt(pg)
    s, v = api("GET", f"/api/exams/{exam['id']}/receipts/{rc}", tok)
    check(s == 200 and v["username"] == "alice" and v["answered"] == 4,
          f"receipt {rc} verified by the teacher after the restart")
    ctx.close()
    return tok


def scenario_d(b, tok):
    print("D  submit while offline, then reconnect: exactly one submit, one receipt")
    exam = new_live_exam(tok, "Net D offline submit")
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    submits = []
    pg.on("response", lambda r: submits.append(r.status) if r.url.endswith("/submit") else None)
    login(pg)
    start_exam(pg, exam)
    answer(pg, 1, "C")
    answer(pg, 2, "3.14")
    wait_synced(pg)
    ctx.set_offline(True)
    answer(pg, 3, "Tashkent")
    answer(pg, 4, ESSAY)  # never synced before the submit
    submit_via_ui(pg)
    expect(pg.get_by_text(re.compile("Cannot reach the server.*Trying again"))).to_be_visible(timeout=10_000)
    pg.wait_for_timeout(8000)  # several submit retries while offline
    pg.screenshot(path=f"{SHOTS}/D1-submit-offline.png", full_page=True)
    check("/student/done/" not in pg.url, "still on the exam page while offline")
    a, _ = server_answers(tok, exam["id"])
    check(a["status"] == "in_progress" and a["receipt"] is None, "server has no submission yet")
    ctx.set_offline(False)
    rc = done_receipt(pg)
    pg.screenshot(path=f"{SHOTS}/D2-done.png", full_page=True)
    pg.wait_for_timeout(4000)  # anything still retrying would show up now
    check(submits.count(200) == 1, f"exactly one submit reached the server (responses {submits})")
    a, given = server_answers(tok, exam["id"])
    check(a["status"] == "submitted" and a["receipt"] == rc, f"one receipt {rc}, shown on the done page")
    check(given == [2, 3.14, "Tashkent", ESSAY], "answers typed offline are in the submission")
    s, v = api("GET", f"/api/exams/{exam['id']}/receipts/{rc}", tok)
    check(s == 200 and v["attempt_id"] == a["attempt_id"], "teacher verifies the receipt")
    check("All 4 answers reached the server" in pg.locator("body").inner_text(),
          "done page says all 4 answers reached the server")
    ctx.close()


def scenario_e(b, tok):
    print("E  submit reached the server but the response was lost: same receipt on retry")
    exam = new_live_exam(tok, "Net E lost response")
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    seen = []

    def lose_first(route):
        resp = route.fetch()  # the request really reaches the server
        seen.append(resp.json().get("receipt"))
        if len(seen) == 1:
            route.abort("connectionreset")  # ... but the student never hears back
        else:
            route.fulfill(response=resp)

    login(pg)
    start_exam(pg, exam)
    answer(pg, 1, "C")
    answer(pg, 3, "Tashkent")
    wait_synced(pg)
    pg.route("**/submit", lose_first)
    submit_via_ui(pg)
    rc = done_receipt(pg)
    check(len(seen) >= 2 and len(set(seen)) == 1, f"server answered {len(seen)} submits, one receipt {seen[0]}")
    check(rc == seen[0], "done page shows that receipt")
    a, _ = server_answers(tok, exam["id"])
    check(a["receipt"] == rc, "teacher results show the same receipt")
    ctx.close()


def scenario_f(b, tok):
    print("F  requests hang instead of failing (half-open connection): saves and submit recover")
    exam = new_live_exam(tok, "Net F hanging requests")
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    login(pg)
    start_exam(pg, exam)
    held = []
    # The first save never gets an answer (a dead connection); later requests work.
    # (unroute() would release it, so the route stays and only holds the first one.)
    pg.route("**/answers", lambda route: held.append(route) if not held else route.continue_())
    answer(pg, 1, "C")
    answer(pg, 3, "Tashkent")
    pg.wait_for_timeout(3000)
    check(len(held) == 1, "the first save is hanging")
    answer(pg, 4, ESSAY)
    t0 = time.time()
    wait_synced(pg, timeout=60_000)
    print(f"   recovered {time.time() - t0:.0f} s later (request timeout 15 s, then retry)")
    _, given = server_answers(tok, exam["id"])
    check(given == [2, None, "Tashkent", ESSAY], "answers reached the server despite the hung save")
    hung_submits = []
    pg.route("**/submit", lambda route: hung_submits.append(route) if not hung_submits else route.continue_())
    submit_via_ui(pg)
    rc = done_receipt(pg, timeout=60_000)
    check(len(hung_submits) == 1, "the first submit hung and was retried after its timeout")
    a, _ = server_answers(tok, exam["id"])
    check(a["receipt"] == rc, f"submitted once, receipt {rc}")
    for r in held + hung_submits:
        try:
            r.abort()
        except Exception:
            pass
    ctx.close()


def only():
    return [s.strip().upper() for s in os.environ.get("FAIRPLAY_ONLY", "").split(",") if s.strip()]


def main():
    srv = None
    if not EXTERNAL:
        srv = Server()
        srv.start()
    failures = 0
    try:
        tok = teacher_token()
        with sync_playwright() as p:
            b = p.chromium.launch()
            steps = [("A", lambda: scenario_a(b, tok)), ("B", lambda: scenario_b(b, tok))]
            if srv:
                steps.append(("C", lambda: scenario_c(b, tok, srv)))
            else:
                print("C  skipped (FAIRPLAY_BASE set: this script cannot restart that server)")
            steps += [("D", lambda: scenario_d(b, tok)), ("E", lambda: scenario_e(b, tok)),
                      ("F", lambda: scenario_f(b, tok))]
            if only():
                steps = [s for s in steps if s[0] in only()]
            for name, fn in steps:
                try:
                    fn()
                    print(f"{name}  PASS")
                except Exception as e:  # keep going so one run reports every scenario
                    failures += 1
                    print(f"{name}  FAIL: {type(e).__name__}: {str(e)[:400]}")
            b.close()
    finally:
        if srv:
            srv.kill()
    print("screenshots and server log:", SHOTS)
    print("ALL PASS" if not failures else f"{failures} scenario(s) failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
