"""E2E homework mode: paper assignment -> student hands in phone photos -> AI grades -> teacher
reviews and releases -> student sees feedback and progress -> teacher progress dashboard.

Starts (unless BASE is already up) a FairPlay server on :8280 with FAIRPLAY_AI_PROVIDER=openai and
FAIRPLAY_AI_BASE_URL pointing at a tiny local fake model on :8281 (OpenAI-compatible, answers with
step-level JSON). Flow, in the browser:

  teacher creates "Week 3 homework" for a class (/teacher/homework/new) -> alice (phone-sized
  browser) photographs 2 pages with the camera input and hands in -> pages are flattened and the
  fake AI grades both questions in the background -> teacher accepts the drafts in the review
  screen (Enter) and releases feedback -> alice sees the "Feedback ready" badge, the step feedback
  and "My progress" -> the teacher's progress dashboard shows the topic and the misconception.

Needs a built web UI: by default it runs `npx vite build` into a temp folder (never into
server/static). Usage: python3 e2e/e2e_homework.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("E2E_PORT", "8280"))
AI_PORT = int(os.environ.get("E2E_AI_PORT", "8281"))
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = Path(os.environ.get("SHOTS", tempfile.mkdtemp(prefix="fp-e2e-hw-")))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"


class FakeAI(BaseHTTPRequestHandler):
    calls: list[tuple[str, int]] = []

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        content = body["messages"][1]["content"]
        text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        FakeAI.calls.append((text, sum(1 for c in content if c.get("type") != "text")))
        if "fp-segment-v1" in text:
            # answer finder: question 1 is on the first photo, question 2 on the second
            cands: dict[int, list[int]] = {}
            for m in re.finditer(r"^Page (\d+): (.*)$", text, re.M):
                cands[int(m.group(1))] = [int(c.group(1)) for c in re.finditer(r"(\d+): \[", m.group(2))]
            out = {"answers": [{"question": str(pg), "label": f"{pg})", "candidates": ns, "confidence": 0.9}
                               for pg, ns in sorted(cands.items()) if pg in (1, 2) and ns],
                   "crossed_out": [], "not_found": []}
        elif "Factor" in text:
            out = {"legibility": "clear", "transcript": "x^2 - 5x + 6 = (x + 2)(x + 3)",
                   "steps": [{"text": "x^2 - 5x + 6", "status": "correct"},
                             {"text": "(x + 2)(x + 3)", "status": "error", "note": "signs of the factors"}],
                   "points": 2, "explanation": "Check the signs: -2 and -3 multiply to +6 and add to -5.",
                   "teacher_note": "Sign error in the factors.", "misconception": {"tag": "sign error", "text": ""},
                   "confidence": 0.9}
        else:
            out = {"legibility": "clear", "transcript": "x = 4",
                   "steps": [{"text": "2x = 8", "status": "correct"}, {"text": "x = 4", "status": "correct"}],
                   "points": 2, "explanation": "Correct — nicely shown.", "confidence": 0.95}
        data = json.dumps({"choices": [{"message": {"content": json.dumps(out)}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def api(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req) as r:
        t = r.read()
        return json.loads(t) if t else None


def up() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=1):
            return True
    except Exception:  # noqa: BLE001
        return False


def start_server() -> subprocess.Popen | None:
    if up():
        print("using the server already running on", BASE)
        return None
    tmp = Path(tempfile.mkdtemp(prefix="fp-e2e-hw-"))
    static = tmp / "static"
    print("building the web UI into", static)
    subprocess.run(["npx", "vite", "build", "--outDir", str(static), "--emptyOutDir", "--logLevel", "error"],
                   cwd=ROOT / "web", check=True)
    env = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{tmp / 'e2e.db'}",
               FAIRPLAY_UPLOADS_DIR=str(tmp / "uploads"), PROCTORA_STATIC_DIR=str(static),
               PROCTORA_PBKDF2_ITERATIONS="1000", FAIRPLAY_AI_PROVIDER="openai", FAIRPLAY_AI_KEY="dummy",
               FAIRPLAY_AI_BASE_URL=f"http://127.0.0.1:{AI_PORT}/v1", FAIRPLAY_AI_MODEL="fake-vision",
               FAIRPLAY_PUBLIC_URL=BASE)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level",
                             "warning"], cwd=ROOT / "server", env=env)
    for _ in range(100):
        if up():
            return proc
        time.sleep(0.2)
    proc.kill()
    raise SystemExit("server did not start")


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def phone_photo(path: Path, lines: list[str]) -> None:
    """A sheet of paper with handwriting-ish text, slightly rotated, on a dark desk."""
    desk = np.full((2400, 1800, 3), (58, 62, 70), np.uint8)
    pts = np.array([[230, 180], [1600, 250], [1560, 2210], [170, 2140]], np.int32)
    cv2.fillPoly(desk, [pts], (238, 240, 236))
    for i, t in enumerate(lines):
        cv2.putText(desk, t, (330, 480 + i * 170), cv2.FONT_HERSHEY_SIMPLEX, 2.4, (40, 40, 70), 5)
    cv2.imwrite(str(path), desk)


def login(page, role, user):
    page.goto(f"{BASE}/{role}/login")
    page.fill("#username", user)
    page.fill("#password", f"{user}123")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(re.compile(rf".*/{role}/?$"))


def main():
    from playwright.sync_api import sync_playwright

    ai_srv = ThreadingHTTPServer(("127.0.0.1", AI_PORT), FakeAI)
    threading.Thread(target=ai_srv.serve_forever, daemon=True).start()
    proc = start_server()
    try:
        run(sync_playwright)
    finally:
        ai_srv.shutdown()
        if proc:
            proc.terminate()
    print("screenshots in", SHOTS)


def run(sync_playwright):
    T = api("POST", "/api/auth/login", {"username": "teacher", "password": "teacher123"})["token"]
    cls = api("POST", "/api/classes", {"name": "Algebra 1 (e2e)"}, T)
    api("POST", f"/api/classes/{cls['id']}/members", {"text": "alice\nbob"}, T)

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        errors: list[str] = []

        # ---------------- teacher creates the homework in the browser
        P = b.new_context(viewport={"width": 1440, "height": 950}).new_page()
        P.on("pageerror", lambda e: errors.append(str(e)))
        login(P, "teacher", "teacher")
        P.goto(f"{BASE}/teacher/homework/new")
        P.get_by_label("Title", exact=True).fill("Week 3 homework")
        P.get_by_role("checkbox", name=re.compile("Algebra 1 \\(e2e\\)")).check()
        P.get_by_label("Question 1", exact=True).fill("Factor x^2 - 5x + 6 and show your work.")
        P.get_by_label("Points", exact=True).first.fill("4")
        P.get_by_label("Topics", exact=True).first.fill("factoring")
        P.keyboard.press("Enter")
        P.get_by_role("button", name="+ Number answer").click()
        P.get_by_label("Question 2", exact=True).fill("Solve 2x + 3 = 11.")
        P.get_by_label("Points", exact=True).nth(1).fill("2")
        P.get_by_label("Topics", exact=True).nth(1).fill("linear equations")
        P.keyboard.press("Enter")
        P.get_by_label("Correct number").fill("4")
        P.screenshot(path=str(SHOTS / "90-new-homework.png"), full_page=True)
        P.get_by_role("button", name="Create homework").click()
        P.wait_for_url(re.compile(r".*/teacher/homework/e_[^/?]+\?tab=key"))
        hid = re.search(r"/teacher/homework/(e_[^/?]+)", P.url).group(1)
        hw = api("GET", f"/api/homework/{hid}", None, T)
        check(hw["title"] == "Week 3 homework" and [c["name"] for c in hw["classes"]] == ["Algebra 1 (e2e)"],
              "homework created for the class")
        check([q["topics"] for q in hw["questions"]] == [["factoring"], ["linear equations"]], "topics saved")
        api("PUT", f"/api/exams/{hid}/answer-key", {"subject": "math"}, T)
        P.get_by_role("tab", name="Students").click()
        P.get_by_text("Not submitted").first.wait_for()

        # ---------------- alice hands in 2 photos from a phone-sized browser
        S = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True).new_page()
        S.on("pageerror", lambda e: errors.append(str(e)))
        login(S, "student", "alice")
        S.get_by_role("link", name="Week 3 homework").click()
        S.get_by_role("heading", name="Week 3 homework").wait_for()
        p1, p2 = SHOTS / "alice-p1.jpg", SHOTS / "alice-p2.jpg"
        phone_photo(p1, ["1) x^2-5x+6", "= (x+2)(x+3)"])
        phone_photo(p2, ["2) 2x + 3 = 11", "2x = 8", "x = 4"])
        cam = S.get_by_test_id("camera-input")
        check(cam.get_attribute("capture") == "environment" and cam.get_attribute("multiple") is not None,
              "camera input uses the back camera, several photos")
        cam.set_input_files([str(p2), str(p1)])  # taken in the wrong order ...
        S.get_by_role("button", name="Move page 2 earlier").click()  # ... fixed before handing in
        S.screenshot(path=str(SHOTS / "91-student-pages.png"), full_page=True)
        S.get_by_role("button", name="Hand in 2 pages").click()
        S.get_by_text(re.compile(r"Handed in\. Your teacher")).wait_for()

        end = time.time() + 90
        row = {}
        while time.time() < end:
            hw = api("GET", f"/api/homework/{hid}", None, T)
            row = next(s for s in hw["students"] if s["username"] == "alice")
            if row["status"] == "ai_graded" and not row["processing"]:
                break
            time.sleep(0.5)
        check(row.get("status") == "ai_graded" and len(row["pages"]) == 2, f"AI graded alice's 2 pages ({row.get('status')})")
        seg = [c for c in FakeAI.calls if "fp-segment-v1" in c[0]]
        grade = [c for c in FakeAI.calls if "fp-segment-v1" not in c[0]]
        check(len(seg) == 1 and seg[0][1] == 2, "answer finder looked at both photos once")
        check(len(grade) == 2 and all(n == 1 for _, n in grade),
              "each question graded on its own cropped answer (not the whole page set)")
        S.reload()
        S.get_by_role("heading", name="What you handed in").wait_for()
        S.screenshot(path=str(SHOTS / "92-student-handed-in.png"), full_page=True)

        # ---------------- teacher reviews in the shared review screen and releases
        P.goto(f"{BASE}/teacher/homework/{hid}?tab=students")
        P.get_by_text("AI graded").first.wait_for()
        P.screenshot(path=str(SHOTS / "93-teacher-students.png"), full_page=True)
        P.get_by_role("tab", name=re.compile("^Review")).click()
        pane = P.locator(".rv-pane")
        pane.wait_for()
        check(pane.locator(".rv-image-box img").count() == 1, "review shows the cropped answer")
        check(pane.get_by_label("Points", exact=True).input_value() == "2", "AI suggestion pre-fills 2 points")
        P.screenshot(path=str(SHOTS / "94-review.png"), full_page=True)
        P.evaluate("document.activeElement && document.activeElement.blur()")
        P.keyboard.press("Enter")  # accept question 1
        P.wait_for_timeout(1000)
        P.get_by_role("button", name=re.compile(r"^Question 2")).click()
        P.wait_for_timeout(600)
        P.evaluate("document.activeElement && document.activeElement.blur()")
        P.keyboard.press("Enter")  # accept question 2
        P.wait_for_timeout(1000)
        hw = api("GET", f"/api/homework/{hid}", None, T)
        row = next(s for s in hw["students"] if s["username"] == "alice")
        check(row["status"] == "reviewed" and row["percent"] == round(4 / 6 * 100, 1),
              f"teacher accepted both drafts ({row['status']}, {row['percent']}%)")
        P.get_by_role("tab", name="Students").click()
        P.get_by_role("button", name="Release feedback").click()
        P.get_by_text("Feedback released. Students see it now.").wait_for()
        check(api("GET", f"/api/homework/{hid}", None, T)["grades_released"], "feedback released")

        # ---------------- alice: badge, step feedback, progress
        S.goto(f"{BASE}/student")
        badge = S.locator(".hw-badge")
        badge.wait_for()
        check("Feedback ready" in badge.inner_text(), "home shows the Feedback ready badge")
        S.screenshot(path=str(SHOTS / "95-student-home-badge.png"), full_page=True)
        S.locator(".hw-home-list").get_by_role("link", name="Week 3 homework").first.click()
        S.get_by_text("Your feedback is ready").wait_for()
        S.get_by_role("link", name="See feedback").click()
        S.get_by_text("Check the signs").first.wait_for()
        txt = S.locator("body").inner_text()
        S.screenshot(path=str(SHOTS / "96-student-feedback.png"), full_page=True)
        check("Check the signs" in txt and "signs of the factors" in txt, "student sees explanation and step notes")
        check(api("GET", "/api/student/notifications", None,
                  api("POST", "/api/auth/login", {"username": "alice", "password": "alice123"})["token"]
                  )["feedback_ready"] == 0, "badge cleared after opening")
        S.goto(f"{BASE}/student/progress")
        work_on = S.get_by_role("region", name="To work on")
        work_on.get_by_text("factoring").wait_for()
        S.screenshot(path=str(SHOTS / "97-student-progress.png"), full_page=True)
        check("sign error" in S.locator("body").inner_text(), "my progress lists the mistake to watch")

        # ---------------- teacher progress dashboard
        P.goto(f"{BASE}/teacher/progress?class={cls['id']}")
        table = P.get_by_role("table", name=re.compile("Mastery per student"))
        table.wait_for()
        check(table.get_by_role("columnheader", name="factoring").count() == 1, "heatmap has the factoring topic")
        cell = table.get_by_label("Alice Chen, factoring: 50%")
        check("hm-struggling" in (cell.get_attribute("class") or ""), "alice is struggling in factoring (50%)")
        body = P.locator("body").inner_text()
        check("sign error" in body and "Re-teach factoring" in body, "misconception + re-teach suggestion shown")
        P.screenshot(path=str(SHOTS / "98-teacher-progress.png"), full_page=True)
        P.get_by_role("link", name="Alice Chen").first.click()
        P.get_by_role("heading", name="Recurring mistakes").wait_for()
        check(P.get_by_role("img", name=re.compile("Answer to: Factor")).count() >= 1,
              "student page shows her own answer as an example")
        P.screenshot(path=str(SHOTS / "99-teacher-student-progress.png"), full_page=True)
        check(not errors, f"no page errors ({errors[:2]})")
        b.close()


if __name__ == "__main__":
    main()
