"""E2E: any paper (no FairPlay codes) — the AI sorts a mixed stack by student and finds the answers.

Starts (unless BASE is already up) a FairPlay server on :8250 with FAIRPLAY_AI_PROVIDER=openai
pointing at a fake model on :8251. The fake model recognises each synthetic page by its aspect
ratio (every page has its own height) and answers the three prompt kinds:
  fp-page-header-v1  -> the name / page number written on that page
  fp-segment-v1      -> candidate box numbers for each answer (read from the prompt), one
                        low-confidence answer, one crossed-out block
  fp-step-grade-v1   -> a step-level grading suggestion
Flow: paper exam + roster (alice, bob) -> one PDF of 3 handwritten-looking pages, no QR, mixed
students -> automatic grouping (bob's name misspelled) + regions -> "Check answer regions" in
the browser: only alice needs a check; select her low-confidence box, resize it with the
keyboard, confirm with Enter -> the review screen shows the AI suggestion for the exact crop.

Needs a built web UI: by default it runs `npx vite build` into a temp folder (never into
server/static). Usage: python3 e2e/e2e_any_paper.py
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
PORT = int(os.environ.get("E2E_PORT", "8250"))
AI_PORT = int(os.environ.get("E2E_AI_PORT", "8251"))
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = Path(os.environ.get("SHOTS", tempfile.mkdtemp(prefix="fp-e2e-anypaper-")))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
W = 1240  # A4-ish width at 150 dpi

# page -> (height px, header the fake reads, [(y px, text, kind, question no.)])
PAGES = {
    "alice-1": (1700, {"name": "Alice Chen", "first_page": True},
                [(120, "Name: Alice Chen", "name", None), (420, "1)  2 + 2 = 4", "answer", 1),
                 (700, "3)  F = m a : a cart pushed with", "answer", 3), (760, "     20 N, m = 5 kg, a = 4 m/s2", "answer", 3),
                 (1050, "2)  Lyon", "crossed", 2), (1300, "2)  Paris", "answer", 2)]),
    "alice-2": (1760, {"page_number": 2},
                [(200, "so the force makes it accelerate", "answer", 3)]),
    "bob-1": (1820, {"name": "Bob Karimow", "first_page": True},
              [(140, "Bob Karimov  10B", "name", None), (480, "Q2. Paris", "answer", 2), (900, "1) 5", "answer", 1),
               (1250, "3) force = mass x acceleration", "answer", 3)]),
}
CONF = {("alice", 1): 0.95, ("alice", 2): 0.55, ("alice", 3): 0.9}


def handwriting(img, y, text, rng):
    """Script font, slightly rotated and jittered — looks written, not printed."""
    layer = np.full_like(img, 255)
    cv2.putText(layer, text, (110 + int(rng.integers(-8, 9)), y), cv2.FONT_HERSHEY_SCRIPT_SIMPLEX, 1.55,
                (35, 35, 90), 3, cv2.LINE_AA)
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, y), float(rng.uniform(-1.2, 1.2)), 1.0)
    layer = cv2.warpAffine(layer, m, (w, h), borderValue=(255, 255, 255))
    return np.minimum(img, layer)


def page_image(key: str) -> np.ndarray:
    h, _, items = PAGES[key]
    rng = np.random.default_rng(len(key) * 7 + h)
    img = np.full((h, W, 3), 248, np.uint8)
    for y in range(160, h, 52):  # notebook rules + margin
        cv2.line(img, (0, y), (W, y), (215, 200, 185), 1)
    cv2.line(img, (90, 0), (90, h), (190, 190, 235), 1)
    for y, text, kind, _ in items:
        img = handwriting(img, y, text, rng)
        if kind == "crossed":
            cv2.line(img, (100, y - 18), (420, y - 10), (35, 35, 90), 4)
    noise = rng.normal(0, 4, img.shape).astype(np.int16)
    return np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def to_pdf(keys) -> bytes:
    doc = pymupdf.open()
    for k in keys:
        img = page_image(k)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        page = doc.new_page(width=595, height=595 * img.shape[0] / W)
        page.insert_image(page.rect, stream=buf.tobytes())
    return doc.tobytes()


def which(url: str) -> str:
    data = base64.b64decode(url.split(",", 1)[1])
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    r = img.shape[0] / img.shape[1]
    return min(PAGES, key=lambda k: abs(PAGES[k][0] / W - r))


# ---------------------------------------------------------------- fake model
class FakeAI(BaseHTTPRequestHandler):
    calls: dict[str, int] = {"header": 0, "segment": 0, "grade": 0}

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        content = body["messages"][1]["content"]
        text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        imgs = [c["image_url"]["url"] for c in content if c.get("type") == "image_url"]
        if "fp-page-header-v1" in text:
            FakeAI.calls["header"] += 1
            h = PAGES[which(imgs[0])][1]
            out = {"name": h.get("name", ""), "student_id": "", "first_page": h.get("first_page", False),
                   "page_number": h.get("page_number"), "confidence": 0.9}
        elif "fp-segment-v1" in text:
            FakeAI.calls["segment"] += 1
            out = segment([which(u) for u in imgs], text)
        else:
            FakeAI.calls["grade"] += 1
            out = {"legibility": "clear", "transcript": "Paris" if "Capital" in text else "4",
                   "steps": [{"text": "wrote the answer", "status": "correct"}], "points": 1,
                   "explanation": "Good — answer found and graded from the cropped region.", "confidence": 0.84}
        data = json.dumps({"choices": [{"message": {"content": json.dumps(out)}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def segment(pages: list[str], text: str) -> dict:
    cands: dict[int, list] = {}
    for m in re.finditer(r"^Page (\d+): (.*)$", text, re.M):
        for c in re.finditer(r"(\d+): \[([^\]]+)\]", m.group(2)):
            cands.setdefault(int(m.group(1)) - 1, []).append((int(c.group(1)), [float(v) for v in c.group(2).split(",")]))

    def find(pi, y):
        for n, b in cands.get(pi, []):
            if b[1] - 0.012 <= y <= b[3] + 0.012:
                return n
        return None

    student = pages[0].split("-")[0]
    out: dict = {"answers": [], "crossed_out": [], "not_found": []}
    per_q: dict[int, list] = {}
    for pi, key in enumerate(pages):
        h, _, items = PAGES[key]
        for y, txt, kind, qn in items:
            n = find(pi, (y - 12) / h)
            if n is None:
                continue
            if kind == "name":
                out["name"] = {"text": txt.replace("Name: ", ""), "candidates": [n], "confidence": 0.9}
            elif kind == "crossed":
                out["crossed_out"].append({"question": str(qn), "candidates": [n]})
            elif n not in per_q.setdefault(qn, []):
                per_q[qn].append(n)
    labels = {1: "1)", 2: "Q2", 3: "3)"}
    for qn, ns in per_q.items():
        out["answers"].append({"question": labels[qn], "label": labels[qn], "candidates": ns,
                               "confidence": CONF.get((student, qn), 0.9)})
    return out


# ---------------------------------------------------------------- helpers
def api(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req) as r:
        t = r.read()
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


def start_server():
    if up():
        print("using the server already running on", BASE)
        return None
    tmp = Path(tempfile.mkdtemp(prefix="fp-e2e-anypaper-srv-"))
    static = tmp / "static"
    print("building the web UI into", static)
    subprocess.run(["npx", "vite", "build", "--outDir", str(static), "--emptyOutDir", "--logLevel", "error"],
                   cwd=ROOT / "web", check=True)
    env = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{tmp / 'e2e.db'}",
               FAIRPLAY_UPLOADS_DIR=str(tmp / "uploads"), PROCTORA_STATIC_DIR=str(static),
               PROCTORA_PBKDF2_ITERATIONS="1000", FAIRPLAY_AI_PROVIDER="openai", FAIRPLAY_AI_KEY="dummy",
               FAIRPLAY_AI_BASE_URL=f"http://127.0.0.1:{AI_PORT}/v1", FAIRPLAY_AI_MODEL="fake-vision")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level",
                             "warning"], cwd=ROOT / "server", env=env)
    for _ in range(150):
        if up():
            return proc
        time.sleep(0.2)
    proc.kill()
    raise SystemExit("server did not start")


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def wait_done(eid, T):
    end = time.time() + 120
    while time.time() < end:
        st = api("GET", f"/api/exams/{eid}/scans", None, T)
        if st["batches"] and all(b["status"] in ("done", "failed") for b in st["batches"]):
            return st
        time.sleep(0.4)
    raise SystemExit("scan did not finish")


def wait_drafts(eid, T, n):
    end = time.time() + 60
    while time.time() < end:
        q = api("GET", f"/api/exams/{eid}/scan/queue", None, T)
        if sum(1 for i in q["items"] if i["draft"]) >= n:
            return q
        time.sleep(0.3)
    raise SystemExit("drafts did not appear")


def main():
    from playwright.sync_api import sync_playwright

    srv = ThreadingHTTPServer(("127.0.0.1", AI_PORT), FakeAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = start_server()
    try:
        run(sync_playwright)
    finally:
        srv.shutdown()
        if proc:
            proc.terminate()
    print("screenshots in", SHOTS)


def run(sync_playwright):
    T = api("POST", "/api/auth/login", {"username": "teacher", "password": "teacher123"})["token"]
    exam = api("POST", "/api/exams", {
        "title": "E2E Any Paper Physics", "course": "PHYS 9", "duration_min": 30, "delivery": "paper",
        "questions": [
            {"type": "numeric", "prompt": "What is 2 + 2?", "points": 2, "answer_number": 4, "topics": ["arithmetic"]},
            {"type": "short", "prompt": "Capital of France?", "points": 2, "accepted_answers": ["Paris"],
             "topics": ["geography"]},
            {"type": "essay", "prompt": "State Newton's second law and give an example.", "points": 4,
             "topics": ["mechanics"]},
        ]}, T)
    eid, qids = exam["id"], [q["id"] for q in exam["questions"]]
    api("PUT", f"/api/exams/{eid}/roster", {"usernames": ["alice", "bob"]}, T)
    for k in PAGES:
        cv2.imwrite(str(SHOTS / f"page-{k}.jpg"), page_image(k))

    # one stack, two students, no codes, no "pages per student"
    upload(f"/api/exams/{eid}/scans?unprinted=1", [("stack.pdf", to_pdf(["alice-1", "alice-2", "bob-1"]),
                                                    "application/pdf")], T)
    st = wait_done(eid, T)
    check(st["batches"][0]["status"] == "done", "stack processed")
    check(FakeAI.calls["header"] == 3 and FakeAI.calls["segment"] == 2,
          f"3 page headers read, 2 students segmented ({FakeAI.calls})")
    sets = api("GET", f"/api/exams/{eid}/scans/sets", None, T)
    by = {s["student"]["username"] if s["student"] else None: s for s in sets["sets"]}
    check(set(by) == {"alice", "bob"}, "pages grouped into alice and bob from the written names")
    check(len(by["alice"]["pages"]) == 2 and len(by["bob"]["pages"]) == 1, "alice has 2 pages, bob 1")
    check(by["bob"]["name_read"] == "Bob Karimow" and by["bob"]["match"]["confidence"] >= 0.88,
          "misspelled 'Bob Karimow' fuzzy-matched to Bob Karimov")
    a_q3 = [r for r in by["alice"]["regions"] if r["question_id"] == qids[2] and not r["crossed_out"]]
    check(len(a_q3) == 2 and len({r["page_id"] for r in a_q3}) == 2, "alice's answer 3 continues onto page 2")
    check(any(r["crossed_out"] for r in by["alice"]["regions"]), "crossed-out work found and kept out of grading")
    check(by["alice"]["needs_check"] and not by["bob"]["needs_check"], "only alice needs a check (unsure box)")
    q = wait_drafts(eid, T, 6)
    check(sum(1 for i in q["items"] if i["draft"] and i["draft"]["source"] in ("ai", "key")) == 6,
          "AI suggestion for every answer (6)")
    before = next(r for r in by["alice"]["regions"] if r["question_id"] == qids[1] and not r["crossed_out"])

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 1000})
        P = ctx.new_page()
        errors: list[str] = []
        P.on("pageerror", lambda e: errors.append(str(e)))
        P.goto(f"{BASE}/teacher/login")
        P.fill("#username", "teacher")
        P.fill("#password", "teacher123")
        P.get_by_role("button", name="Sign in").click()
        P.wait_for_url(re.compile(r".*/teacher/?$"))

        P.goto(f"{BASE}/teacher/exams/{eid}/scan?step=upload")
        P.get_by_role("heading", name="Check answer regions").wait_for()
        P.get_by_role("button", name=re.compile(r"Needs check \(1\)")).wait_for()
        P.locator(".rc-canvas img").first.wait_for()
        P.wait_for_timeout(500)
        P.screenshot(path=str(SHOTS / "90-check-regions.png"), full_page=True)
        students = P.locator(".rc-students").inner_text()
        check("Alice Chen" in students and "Bob" not in students, "needs-check filter lists only alice")
        box = P.get_by_role("button", name=re.compile(r"^Box Q2 · 55%"))
        check(box.count() == 1, "low-confidence box shown with its confidence")
        box.click()
        P.evaluate("document.activeElement && document.activeElement.blur()")
        for _ in range(4):
            P.keyboard.press("Shift+ArrowDown")
        for _ in range(6):
            P.keyboard.press("Shift+ArrowRight")
        P.screenshot(path=str(SHOTS / "91-box-adjusted.png"), full_page=True)
        P.keyboard.press("Enter")  # save + confirm
        P.get_by_text(re.compile(r"Confirmed Alice Chen\. AI is grading 1 answer again")).wait_for()
        P.screenshot(path=str(SHOTS / "92-confirmed.png"), full_page=True)

        sets = api("GET", f"/api/exams/{eid}/scans/sets", None, T)
        a = next(s for s in sets["sets"] if s["student"] and s["student"]["username"] == "alice")
        after = next(r for r in a["regions"] if r["question_id"] == qids[1] and not r["crossed_out"])
        check(a["status"] == "confirmed" and sets["needs_check"] == 0, "alice confirmed, nothing left to check")
        check(after["source"] == "teacher" and after["box"][3] > before["box"][3] and after["box"][2] > before["box"][2],
              f"box made taller and wider ({before['box']} -> {after['box']})")
        wait_drafts(eid, T, 6)

        # review screen: the AI suggestion on the exact crop
        P.goto(f"{BASE}/teacher/exams/{eid}/scan?step=grade")
        pane = P.locator(".rv-pane")
        pane.wait_for()
        P.get_by_role("button", name=re.compile(r"^Question 2")).click()
        P.wait_for_timeout(600)
        txt = pane.inner_text()
        check("Answer region found" in txt, "review shows where the answer was found")
        check(pane.get_by_label("Points", exact=True).input_value() in ("1", "2"), "AI suggestion pre-fills the points")
        img = pane.locator(".rv-image-box img")
        nat = img.evaluate("i => [i.naturalWidth, i.naturalHeight]")
        check(nat[1] < 400, f"review image is the answer crop, not the page ({nat})")
        P.screenshot(path=str(SHOTS / "93-review-crop.png"), full_page=True)
        check(not errors, f"no page errors ({errors[:2]})")
        b.close()


if __name__ == "__main__":
    main()
