"""E2E phase 2: AI paper grading with a fake AI endpoint, driven through the browser.

Starts (unless BASE is already up) a FairPlay server on :8240 with
FAIRPLAY_AI_PROVIDER=openai, a dummy FAIRPLAY_AI_KEY and FAIRPLAY_AI_BASE_URL pointing at a
tiny local fake model on :8241 that answers with step-level JSON. Flow:

  teacher creates a paper exam -> prints booklets (API) -> students "write" -> upload scan
  -> answer key + rubric -> "Grade the rest with AI" -> side-by-side review in the browser:
  Enter accepts, a number key changes the points, a step mark is flipped, a criterion is
  ticked -> export .xlsx -> phone capture page (no login) sends a photo -> release ->
  student feedback shows the explanation and step marks.

Needs a built web UI: by default it runs `npx vite build` into a temp folder (never into
server/static). Usage: python3 e2e/e2e_ai_grading.py
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from app.scan.print import build_layout  # noqa: E402

PORT = int(os.environ.get("E2E_PORT", "8240"))
AI_PORT = int(os.environ.get("E2E_AI_PORT", "8241"))
BASE = f"http://127.0.0.1:{PORT}"
DPI = 200
S = DPI / 72
SHOTS = Path(os.environ.get("SHOTS", tempfile.mkdtemp(prefix="fp-e2e-shots-")))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"


# ---------------------------------------------------------------- fake model (OpenAI-compatible)
class FakeAI(BaseHTTPRequestHandler):
    calls: list[str] = []

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        text = "".join(c.get("text", "") for c in body["messages"][1]["content"] if c.get("type") == "text")
        FakeAI.calls.append(text)
        if "Balance" in text:
            out = {"legibility": "clear", "transcript": "2H2 + O2 -> 2H2O",
                   "steps": [{"text": "count H and O atoms", "status": "correct"},
                             {"text": "2H2 + O2 -> 2H2O", "status": "correct"},
                             {"text": "check both sides", "status": "missing", "note": "no check shown"}],
                   "criteria": [{"id": "c1", "points": 3}, {"id": "c2", "points": 0, "note": "no work"}],
                   "points": 3, "explanation": "Balanced correctly — show how you checked the atoms next time.",
                   "teacher_note": "Work not shown.", "misconception": {"tag": "no work shown", "text": ""},
                   "confidence": 0.86}
        else:
            out = {"legibility": "partly", "transcript": "8.3",
                   "steps": [{"text": "n = m / M", "status": "correct"}, {"text": "= 8.3", "status": "error",
                                                                           "note": "used M = 2.4"}],
                   "points": 0, "explanation": "Use the molar mass 24 g/mol for magnesium.",
                   "misconception": {"tag": "wrong formula", "text": ""}, "confidence": 0.55}
        data = json.dumps({"choices": [{"message": {"content": json.dumps(out)}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def api(method, path, body=None, token=None, raw=False):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req) as r:
        t = r.read()
        if raw:
            return t, dict(r.headers)
        return json.loads(t) if t else None


def upload(path, files, token):
    boundary = "----fp" + str(time.time_ns())
    buf = io.BytesIO()
    for name, data, ctype in files:
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                  f"Content-Type: {ctype}\r\n\r\n".encode())
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(BASE + path, method="POST", data=buf.getvalue(), headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


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
    tmp = Path(tempfile.mkdtemp(prefix="fp-e2e-"))
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


def render(pdf_bytes):
    out = []
    with pymupdf.open(stream=pdf_bytes) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=DPI)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
            out.append(cv2.cvtColor(img, cv2.COLOR_RGB2BGR).copy())
    return out


def to_pdf(images) -> bytes:
    doc = pymupdf.open()
    for img in images:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        h, w = img.shape[:2]
        page = doc.new_page(width=w * 72 / DPI, height=h * 72 / DPI)
        page.insert_image(page.rect, stream=buf.tobytes())
    return doc.tobytes()


def wait_batches(eid, T, n):
    end = time.time() + 120
    while time.time() < end:
        st = api("GET", f"/api/exams/{eid}/scans", None, T)
        if len(st["batches"]) >= n and all(x["status"] in ("done", "failed") for x in st["batches"]):
            return st
        time.sleep(0.5)
    raise SystemExit("scans did not finish")


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


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
    exam = api("POST", "/api/exams", {
        "title": "E2E AI Chemistry", "course": "CHEM 101", "duration_min": 40, "delivery": "paper",
        "questions": [
            {"type": "essay", "prompt": "Balance: H2 + O2 -> H2O. Show your work.", "points": 4,
             "answer_lines": 8, "topics": ["stoichiometry"]},
            {"type": "short", "prompt": "Moles in 2 g of Mg?", "points": 2, "accepted_answers": ["0.083"],
             "topics": ["moles"]},
        ]}, T)
    eid = exam["id"]
    qids = [q["id"] for q in exam["questions"]]
    api("PUT", f"/api/exams/{eid}/roster", {"usernames": ["alice", "bob"]}, T)
    pdf, _ = api("GET", f"/api/exams/{eid}/print.pdf", None, T, raw=True)
    layout, _ = build_layout(eid, exam["questions"])
    pages = render(pdf)
    per = layout["page_count"]
    boxes = {b["qid"]: (pi, b) for pi, pg in enumerate(layout["pages"]) for b in pg["boxes"]}

    def write(booklet, qid, text):
        pi, b = boxes[qid]
        x0, y0, _, _ = b["rect"]
        cv2.putText(booklet[pi], text, (int(x0 * S) + 25, int(y0 * S) + 70), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                    (30, 30, 60), 3)

    alice, bob = pages[:per], pages[per:]
    write(alice, qids[0], "2H2 + O2 -> 2H2O")
    write(alice, qids[1], "8.3")
    write(bob, qids[0], "H2 + O2 -> H2O2")
    write(bob, qids[1], "0.083")

    # answer key + rubric first; the upload then gets step-level AI suggestions right away
    api("PUT", f"/api/exams/{eid}/answer-key", {"subject": "chemistry"}, T)
    api("PUT", f"/api/exams/{eid}/answer-key/questions/{qids[0]}", {
        "solution": "2H2 + O2 -> 2H2O", "partial_credit": "1 point for the right products",
        "criteria": [{"id": "c1", "label": "Correct coefficients", "points": 3},
                     {"id": "c2", "label": "Work shown", "points": 1}]}, T)
    upload(f"/api/exams/{eid}/scans", [("class.pdf", to_pdf(alice + bob[:1]), "application/pdf")], T)
    wait_batches(eid, T, 1)
    check(len(FakeAI.calls) == 4, f"AI drafted {len(FakeAI.calls)} answers via the fake model during the upload")
    run_ = api("POST", f"/api/exams/{eid}/scan/ai-grade", {}, T)
    check(run_["queued"] == 0, "Grade the rest with AI: nothing left to draft")
    check(all("fp-step-grade-v1" in c and "Correct coefficients" in c for c in FakeAI.calls if "Balance" in c),
          "prompt carries the version and the rubric")

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 950}, accept_downloads=True)
        P = ctx.new_page()
        errors = []
        P.on("pageerror", lambda e: errors.append(str(e)))
        P.goto(f"{BASE}/teacher/login")
        P.fill("#username", "teacher")
        P.fill("#password", "teacher123")
        P.get_by_role("button", name="Sign in").click()
        P.wait_for_url(re.compile(r".*/teacher/?$"))

        P.goto(f"{BASE}/teacher/exams/{eid}/scan?step=grade")
        pane = P.locator(".rv-pane")
        pane.wait_for()
        P.screenshot(path=str(SHOTS / "80-review.png"), full_page=True)
        first = pane.locator("strong").first.inner_text()
        check(pane.get_by_label("Points", exact=True).input_value() == "3", "AI suggestion pre-fills 3 points")
        check(pane.get_by_role("button", name=re.compile(r"Step 3: missing")).count() == 1, "step marks shown")
        P.keyboard.press("Enter")  # accept as is, next
        P.wait_for_timeout(1000)
        second = pane.locator("strong").first.inner_text()
        check(second != first, f"Enter accepted {first.split()[0]} and moved to {second.split()[0]}")
        # bob: tick "Work shown" (recomputes), flip a step, change feedback, save with Enter
        pane.get_by_label("Work shown", exact=True).check()
        pane.get_by_role("button", name=re.compile(r"Step 3: missing")).click()
        pane.get_by_label("Feedback for the student").fill("Water is H2O, not H2O2.")
        pane.get_by_label("Points", exact=True).click()
        P.keyboard.press("Enter")  # submits the form from the points field
        P.wait_for_timeout(1000)
        # question 2: number key sets the points on the low-confidence answer
        P.get_by_role("button", name=re.compile(r"^Question 2")).click()
        P.wait_for_timeout(600)
        P.evaluate("document.activeElement && document.activeElement.blur()")
        P.keyboard.press("1")
        P.keyboard.press("Enter")
        P.wait_for_timeout(1000)
        P.screenshot(path=str(SHOTS / "81-review-q2.png"), full_page=True)

        q = api("GET", f"/api/exams/{eid}/scan/queue", None, T)
        grades = {(i["username"], i["question_id"]): i["grade"] for i in q["items"] if i["grade"]}
        g1 = grades.get(("alice", qids[0])) or {}
        check(g1.get("source") == "ai_accepted" and g1.get("ai_suggested_points") == 3, "alice: ai_accepted")
        check((g1.get("grading_seconds") or 0) > 0, "grading_seconds recorded")
        g2 = grades.get(("bob", qids[0])) or {}
        check(g2.get("source") == "ai_edited" and g2.get("points") == 4 and g2.get("graded_by") == "teacher",
              "bob: criterion ticked -> 4 points, ai_edited")
        check(any(s.get("teacher_flipped") for s in g2.get("steps") or []), "flipped step stored")
        g3 = grades.get(("alice", qids[1])) or {}
        check(g3.get("points") == 1 and g3.get("source") == "ai_edited", "number key set 1 point")

        # export
        P.goto(f"{BASE}/teacher/exams/{eid}/scan?step=release")
        P.get_by_role("button", name="Export Excel").wait_for()
        with P.expect_download() as dl:
            P.get_by_role("button", name="Export Excel").click()
        import openpyxl
        xlsx = SHOTS / dl.value.suggested_filename
        dl.value.save_as(str(xlsx))
        wb = openpyxl.load_workbook(xlsx)
        check(xlsx.suffix == ".xlsx" and wb.sheetnames == ["Grades", "Feedback"], f"xlsx export ({xlsx.name})")
        P.get_by_role("heading", name="Where students struggled").wait_for()
        P.screenshot(path=str(SHOTS / "82-release-struggles.png"), full_page=True)

        # phone capture: bob's missing page 2 as a photo, from a phone-sized browser without login
        link = api("POST", f"/api/exams/{eid}/capture", {}, T)
        phone = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True).new_page()
        phone.goto(BASE + link["path"])
        phone.get_by_role("heading", name="E2E AI Chemistry").wait_for()
        photo_path = SHOTS / "bob-page2.jpg"
        desk = np.full((2600, 2000, 3), 55, np.uint8)
        pg = cv2.resize(bob[1] if len(bob) > 1 else bob[0], (1600, 2262))
        desk[170:170 + 2262, 200:1800] = pg
        cv2.imwrite(str(photo_path), desk)
        phone.get_by_label("Take photo").set_input_files(str(photo_path))
        phone.get_by_role("button", name="Send 1 photo").click()
        phone.get_by_text(re.compile("Sent 1 photo")).wait_for()
        phone.screenshot(path=str(SHOTS / "83-phone.png"), full_page=True)
        st = wait_batches(eid, T, 2)
        check(st["batches"][0]["origin"] == "phone" and st["batches"][0]["status"] == "done", "phone photo processed")

        # release + student feedback with step marks
        api("POST", f"/api/exams/{eid}/release", {"force": True}, T)
        Sx = b.new_context(viewport={"width": 1280, "height": 900}).new_page()
        Sx.goto(f"{BASE}/student/login")
        Sx.fill("#username", "alice")
        Sx.fill("#password", "alice123")
        Sx.get_by_role("button", name="Sign in").click()
        Sx.wait_for_url(re.compile(r".*/student/?$"))
        Sx.goto(f"{BASE}/student/grades/{eid}")
        Sx.get_by_text("Your work, step by step").first.wait_for()
        txt = Sx.locator("body").inner_text()
        Sx.screenshot(path=str(SHOTS / "84-student-feedback.png"), full_page=True)
        check("show how you checked the atoms" in txt and "Missing step" in txt, "student sees explanation + steps")
        check(not errors, f"no page errors ({errors[:2]})")
        b.close()


if __name__ == "__main__":
    main()
