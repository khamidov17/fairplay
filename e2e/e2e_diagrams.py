"""E2E: graph / chart / diagram questions, graded with a fake vision model, through the browser.

Starts (unless BASE is already up) a FairPlay server on :8270 with FAIRPLAY_AI_PROVIDER=openai
and FAIRPLAY_AI_BASE_URL pointing at a fake OpenAI-compatible model on :8271. The fake "reads" the
student's drawing with the same OpenCV helper the server uses for its cross-check, so its answers
depend on what was really drawn. Flow:

  teacher creates a paper exam -> exam editor: marks question 1 as "Data chart" (default
  checklist), types the points to plot, saves -> answer-key step: uploads a reference drawing and
  saves the checklist -> prints booklets -> students "draw" (alice plots the five points, bob only
  draws axes) -> upload scan -> AI drafts (2 images per call: drawing + reference) -> review
  screen: the drawing with ✓/✗ marks, the checklist; ticking "Axis labels" recomputes the points;
  bob is flagged by the picture check -> release -> alice's feedback shows the marked drawing and
  the checklist results.

Needs a built web UI: by default it runs `npx vite build` into a temp folder (never into
server/static). Usage: python3 e2e/e2e_diagrams.py
"""
from __future__ import annotations

import base64
import io
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
import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from app.scan import diagram_cv  # noqa: E402
from app.scan.print import build_layout  # noqa: E402

PORT = int(os.environ.get("E2E_PORT", "8270"))
AI_PORT = int(os.environ.get("E2E_AI_PORT", "8271"))
BASE = f"http://127.0.0.1:{PORT}"
DPI = 200
S = DPI / 72
SHOTS = Path(os.environ.get("SHOTS", tempfile.mkdtemp(prefix="fp-e2e-diagrams-")))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
EXPECTED = [(1, 2), (2, 4), (3, 5), (4, 7), (6, 9)]


# ---------------------------------------------------------------- fake vision model
class FakeAI(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        content = body["messages"][1]["content"]
        text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        imgs = [base64.b64decode(c["image_url"]["url"].split(",", 1)[1]) for c in content if c.get("type") == "image_url"]
        FakeAI.calls.append({"text": text, "images": len(imgs)})
        if "GRAPH / DIAGRAM QUESTION" in text:
            (SHOTS / f"drawing-{len(FakeAI.calls)}.jpg").write_bytes(imgs[0])
            cv = diagram_cv.analyze(imgs[0])
            dots = sorted(cv.get("plot_dots") or [])
            # a model that reads the dots it sees; it believes the points are right either way
            readings = [{"label": f"P{i + 1}", "x": x, "y": y, "at": d} for i, ((x, y), d) in enumerate(zip(EXPECTED, dots))]
            if not dots:  # nothing plotted: it still "reads" the expected values somewhere
                readings = [{"label": f"P{i + 1}", "x": x, "y": y, "at": [0.2 + 0.1 * i, 0.6 - 0.08 * i]}
                            for i, (x, y) in enumerate(EXPECTED)]
            ax = (cv.get("axes") or {})
            ox, oy = (ax.get("origin") or [0.1, 0.9])
            out = {"legibility": "clear", "transcript": "scatter graph of the table",
                   "steps": [{"text": "axes and scale drawn", "status": "correct"},
                             {"text": "points plotted", "status": "correct"}],
                   "features": [
                       {"id": "f1", "status": "present", "points": 1, "note": "Scatter graph",
                        "box": [max(0, ox - 0.03), 0.05, 0.95, min(1, oy + 0.03)]},
                       {"id": "f2", "status": "missing", "points": 0, "note": "No units on either axis",
                        "point": [0.55, min(0.98, oy + 0.04)]},
                       {"id": "f3", "status": "present", "points": 1, "note": "Even steps"},
                       {"id": "f4", "status": "present", "points": 3, "note": "All five points in place",
                        "box": [ox + 0.02, 0.15, 0.7, oy - 0.02]},
                       {"id": "f5", "status": "missing", "points": 0, "note": "No line of best fit"},
                       {"id": "f6", "status": "missing", "points": 0, "note": "No title"}],
                   "readings": readings, "points": 5,
                   "explanation": "Clear scatter graph — add units to both axis labels, a line of best fit and a title.",
                   "teacher_note": "Units, best-fit line and title missing.",
                   "misconception": {"tag": "units", "text": ""}, "confidence": 0.9}
        else:
            out = {"legibility": "clear", "transcript": "N", "points": 1, "confidence": 0.95,
                   "explanation": "Correct.", "steps": [{"text": "N", "status": "correct"}]}
        data = json.dumps({"choices": [{"message": {"content": json.dumps(out)}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------- helpers
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
    tmp = Path(tempfile.mkdtemp(prefix="fp-e2e-dg-"))
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


def draw_graph(img, x0, y0, w, h, points, seed=1):
    """A 'hand-drawn' graph inside (x0, y0, w, h): wobbly axes, ticks, filled dots, a pencil label."""
    rng = np.random.default_rng(seed)
    ink = (45, 40, 40)
    ox, oy = x0 + 110, y0 + h - 110
    sx, sy = min(110, (w - 220) // 10), min(90, (h - 220) // 10)

    def P(x, y):
        return int(ox + x * sx + rng.normal(0, 1.2)), int(oy - y * sy + rng.normal(0, 1.2))
    cv2.line(img, (ox - 40, oy + 2), (ox + 10 * sx + 60, oy - 2), ink, 4)
    cv2.line(img, (ox - 1, oy + 40), (ox + 2, oy - 10 * sy - 50), ink, 4)
    for i in range(1, 11):
        cv2.line(img, (ox + i * sx, oy - 9), (ox + i * sx, oy + 9), ink, 3)
        cv2.line(img, (ox - 9, oy - i * sy), (ox + 9, oy - i * sy), ink, 3)
        cv2.putText(img, str(i), (ox + i * sx - 8, oy + 42), cv2.FONT_HERSHEY_SIMPLEX, 0.8, ink, 2)
        cv2.putText(img, str(i), (ox - 52, oy - i * sy + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.8, ink, 2)
    cv2.putText(img, "time", (ox + 10 * sx - 40, oy + 85), cv2.FONT_HERSHEY_SIMPLEX, 1.0, ink, 2)
    cv2.putText(img, "distance", (ox - 95, oy - 10 * sy - 70), cv2.FONT_HERSHEY_SIMPLEX, 1.0, ink, 2)
    for x, y in points:
        cv2.circle(img, P(x, y), 10, (30, 30, 30), -1)


# ---------------------------------------------------------------- run
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


def login(page, role, user, pw):
    page.goto(f"{BASE}/{role}/login")
    page.fill("#username", user)
    page.fill("#password", pw)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(re.compile(rf".*/{role}/?$"))


def run(sync_playwright):
    T = api("POST", "/api/auth/login", {"username": "teacher", "password": "teacher123"})["token"]
    exam = api("POST", "/api/exams", {
        "title": "E2E Lab Graphs", "course": "PHYS 101", "duration_min": 40, "delivery": "paper",
        "questions": [
            {"type": "essay", "prompt": "Plot distance against time from the table and describe the trend.",
             "points": 8, "answer_lines": 30, "topics": ["graphs"]},
            {"type": "short", "prompt": "Unit of force?", "points": 1, "accepted_answers": ["N"], "topics": ["units"]},
        ]}, T)
    eid = exam["id"]
    api("PUT", f"/api/exams/{eid}/roster", {"usernames": ["alice", "bob"]}, T)

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 950})
        P = ctx.new_page()
        errors: list[str] = []
        P.on("pageerror", lambda e: errors.append(str(e)))
        login(P, "teacher", "teacher", "teacher123")

        # 1) exam editor: mark question 1 as a data chart, default checklist, points to plot
        P.goto(f"{BASE}/teacher/exams/{eid}/edit")
        q1 = P.get_by_role("region", name="Question 1")
        q1.locator(".t-q-toggle").click()
        kind = q1.get_by_label("Answer kind")
        kind.wait_for()
        P.wait_for_function("() => document.querySelectorAll('option[value=data_chart]').length > 0")
        kind.select_option("data_chart")
        check(q1.get_by_label("Feature 4", exact=True).input_value() == "Points plotted correctly",
              "editor: data chart default checklist filled in")
        check(q1.get_by_text("8 / 8 pts").count() == 1, "editor: checklist points sized to the question (8)")
        q1.get_by_label("Feature 4 points to plot", exact=True).fill("; ".join(f"{x}, {y}" for x, y in EXPECTED))
        q1.get_by_label("Feature 1 expected", exact=True).fill("scatter graph")
        P.screenshot(path=str(SHOTS / "90-editor-diagram.png"), full_page=True)
        P.get_by_role("button", name="Save exam").click()
        P.wait_for_timeout(1500)
        ex = api("GET", f"/api/exams/{eid}", None, T)
        dq = ex["questions"][0].get("diagram") or {}
        check(dq.get("kind") == "data_chart" and dq["features"][3]["points_xy"] == [list(p) for p in EXPECTED],
              "editor saved the diagram kind and the points to plot")
        check([f["points"] for f in dq["features"]] == [1, 1, 1, 3, 1, 1], "saved checklist: 1+1+1+3+1+1 = 8 points")
        qids = [q["id"] for q in ex["questions"]]

        # 2) answer-key step: reference drawing
        ref = np.full((900, 1200, 3), 250, np.uint8)
        draw_graph(ref, 0, 0, 1200, 900, EXPECTED, seed=5)
        ref_path = SHOTS / "reference.png"
        cv2.imwrite(str(ref_path), ref)
        P.goto(f"{BASE}/teacher/exams/{eid}/scan?step=key")
        card = P.locator(".ak-q").first
        card.get_by_label("Upload reference drawing").wait_for(state="attached")
        card.get_by_label("Upload reference drawing").set_input_files(str(ref_path))
        card.get_by_role("img", name="Reference drawing").wait_for()
        card.get_by_role("button", name="Save checklist").click()
        card.get_by_role("status").wait_for()
        P.screenshot(path=str(SHOTS / "91-answer-key-diagram.png"), full_page=True)
        lst = api("GET", f"/api/exams/{eid}/diagrams", None, T)
        d0 = lst["questions"][0]
        check(d0["source"] == "key" and (d0["diagram"]["reference_image"] or "").startswith("/api/uploads/"),
              "answer-key step saved the reference drawing")

        # 3) print, draw, upload (AI drafts right away)
        pdf, _ = api("GET", f"/api/exams/{eid}/print.pdf", None, T, raw=True)
        layout, _ = build_layout(eid, ex["questions"])
        pages = render(pdf)
        per = layout["page_count"]
        boxes = {bx["qid"]: (pi, bx) for pi, pg in enumerate(layout["pages"]) for bx in pg["boxes"]}
        pi, bx = boxes[qids[0]]
        x0, y0, x1, y1 = [int(v * S) for v in bx["rect"]]
        draw_graph(pages[pi], x0 + 30, y0 + 30, x1 - x0 - 60, y1 - y0 - 60, EXPECTED, seed=1)          # alice
        draw_graph(pages[per + pi], x0 + 30, y0 + 30, x1 - x0 - 60, y1 - y0 - 60, [], seed=2)          # bob: no points
        for k in (0, 1):
            pj, sb = boxes[qids[1]]
            sx0, sy0, _, _ = sb["rect"]
            cv2.putText(pages[k * per + pj], "N", (int(sx0 * S) + 25, int(sy0 * S) + 70), cv2.FONT_HERSHEY_SIMPLEX,
                        1.4, (30, 30, 60), 3)
        upload(f"/api/exams/{eid}/scans", [("class.pdf", to_pdf(pages), "application/pdf")], T)
        wait_batches(eid, T, 1)
        diag_calls = [c for c in FakeAI.calls if "GRAPH / DIAGRAM QUESTION" in c["text"]]
        check(len(diag_calls) == 2, f"two drawings sent to the vision model ({len(diag_calls)})")
        check(all(c["images"] == 2 for c in diag_calls), "each call carries the drawing + the reference")
        check(all("Points plotted correctly (3 pts)" in c["text"] and "(1, 2), (2, 4)" in c["text"] for c in diag_calls),
              "prompt carries the checklist and the expected points")
        q = api("GET", f"/api/exams/{eid}/scan/queue", None, T)
        items = {(i["username"], i["question_id"]): i for i in q["items"]}
        da = items[("alice", qids[0])]["draft"]
        db = items[("bob", qids[0])]["draft"]
        chk = da["diagram"]["check"]
        check(chk["axes_found"] and chk["points_matched"] == 5 and chk["agrees"],
              f"OpenCV found the axes and all 5 plotted points on alice's photo ({chk['points_matched']})")
        check(db["needs_review"] and "diagram_check_disagrees" in db["review_reasons"],
              "bob: the picture check disagrees with the AI -> needs review")

        # 4) review screen
        P.goto(f"{BASE}/teacher/exams/{eid}/scan?step=grade")
        pane = P.locator(".rv-pane")
        pane.wait_for()
        if "Alice" not in pane.locator(".rv-who strong").inner_text():
            P.locator(".rv-chip", has_text="Alice").click()
            P.wait_for_timeout(400)
        overlay = pane.get_by_test_id("diagram-overlay")
        overlay.wait_for()
        check(overlay.locator(".dg-mark").count() == 3, "drawing shows 3 feature marks")
        check(overlay.locator(".dg-mark.is-ok").count() == 2 and overlay.locator('[data-status="missing"]').count() == 1,
              "marks: 2 ✓ and 1 ✗ (axis units)")
        check(pane.get_by_label("Points", exact=True).input_value() == "5", "AI suggestion pre-fills 5 points")
        check(pane.get_by_role("link", name="Reference drawing").count() == 1, "link to the reference drawing")
        P.screenshot(path=str(SHOTS / "92-review-diagram.png"), full_page=True)
        pane.get_by_label("Axis labels with units", exact=True).check()
        check(pane.get_by_label("Points", exact=True).input_value() == "6", "ticking a feature recomputes 6 points")
        check(overlay.locator('[data-feature="f2"]').get_attribute("data-status") == "present", "its mark turns ✓")
        P.screenshot(path=str(SHOTS / "93-review-diagram-ticked.png"), full_page=True)
        pane.get_by_label("Points", exact=True).click()
        P.keyboard.press("Enter")
        P.wait_for_timeout(1200)
        if "Bob" not in pane.locator(".rv-who strong").inner_text():
            P.locator(".rv-chip", has_text="Bob").click()
            P.wait_for_timeout(400)
        check(pane.get_by_role("note").filter(has_text="Picture check").count() == 1, "bob: picture check warning shown")
        pane.get_by_label("Points plotted correctly", exact=True).uncheck()
        check(pane.get_by_label("Points", exact=True).input_value() == "2", "bob: unticking the points -> 2")
        P.screenshot(path=str(SHOTS / "94-review-bob.png"), full_page=True)
        pane.get_by_label("Points", exact=True).click()
        P.keyboard.press("Enter")
        P.wait_for_timeout(1200)
        q = api("GET", f"/api/exams/{eid}/scan/queue", None, T)
        grades = {(i["username"], i["question_id"]): i["grade"] for i in q["items"] if i["grade"]}
        ga = grades.get(("alice", qids[0])) or {}
        gb = grades.get(("bob", qids[0])) or {}
        check(ga.get("points") == 6 and ga.get("source") == "ai_edited" and
              [f["status"] for f in ga["diagram"]["features"]][:4] == ["present", "present", "present", "present"],
              "alice graded 6 with feature statuses")
        check(gb.get("points") == 2 and gb["diagram"]["features"][3]["status"] == "wrong", "bob graded 2, points wrong")
        for who in ("alice", "bob"):
            att = next(i for i in q["items"] if i["username"] == who and i["question_id"] == qids[1])
            api("POST", f"/api/exams/{eid}/scan/answers/{att['attempt_id']}/{qids[1]}/approve", {}, T)

        # 5) release -> student feedback
        api("POST", f"/api/exams/{eid}/release", {"force": True}, T)
        Sx = b.new_context(viewport={"width": 1280, "height": 900}).new_page()
        Sx.on("pageerror", lambda e: errors.append(str(e)))
        login(Sx, "student", "alice", "alice123")
        Sx.goto(f"{BASE}/student/grades/{eid}")
        sec = Sx.get_by_role("region", name="Checklist for question 1")
        sec.wait_for()
        check(sec.locator(".dg-mark").count() == 3, "student sees the marked drawing")
        txt = sec.inner_text()
        check("Points plotted correctly" in txt and "No line of best fit" in txt, "student sees the checklist results")
        check("No units on either axis" not in txt, "the AI note the teacher overruled is not shown")
        check(Sx.get_by_text("See the correct drawing").count() == 1, "student can open the correct drawing")
        Sx.screenshot(path=str(SHOTS / "95-student-diagram.png"), full_page=True)
        check(not errors, f"no page errors ({errors[:2]})")
        b.close()


if __name__ == "__main__":
    main()
