"""E2E slice 6: paper exam (Scan & Grade) in the browser.
Teacher downloads booklets from the UI -> we "fill them in" (bubble + handwriting stand-in) ->
upload the scan PDF through the UI -> pages matched -> teacher grades in the queue (UI edit) ->
release -> student sees score and their scanned answer. Server on :8000 (AI off)."""
import os
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import pymupdf
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from app.scan.print import build_layout  # noqa: E402

BASE = os.environ.get("FAIRPLAY_BASE", "http://127.0.0.1:8000").rstrip("/")
DPI = 200
S = DPI / 72


def api(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req) as r:
        t = r.read()
        return json.loads(t) if t else None


def login_ui(p, role, u, pw):
    p.goto(f"{BASE}/{role}/login")
    p.fill("#username", u)
    p.fill("#password", pw)
    p.get_by_role("button", name="Sign in").click()
    p.wait_for_url(re.compile(rf".*/{role}/?$"))


def render(pdf_path):
    out = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=DPI)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
            out.append(cv2.cvtColor(img, cv2.COLOR_RGB2BGR).copy())
    return out


def to_pdf(images, path):
    doc = pymupdf.open()
    for img in images:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        h, w = img.shape[:2]
        page = doc.new_page(width=w * 72 / DPI, height=h * 72 / DPI)
        page.insert_image(page.rect, stream=buf.tobytes())
    doc.save(path)


def main():
    T = api("POST", "/api/auth/login", {"username": "teacher", "password": "teacher123"})["token"]
    exam = api("POST", "/api/exams", {
        "title": "E2E Paper Chemistry", "course": "CHEM 101", "duration_min": 40, "delivery": "paper",
        "questions": [
            {"type": "mcq", "prompt": "Which is a noble gas?", "points": 2, "options": ["O2", "Ne", "N2", "H2"],
             "correct_index": 1, "topics": ["periodic table"]},
            {"type": "essay", "prompt": "Balance: H2 + O2 -> H2O. Show your work.", "points": 4,
             "answer_lines": 8, "topics": ["stoichiometry"],
             "rubric": [{"label": "Correct coefficients", "points": 3}, {"label": "Work shown", "points": 1}]},
        ]}, T)
    eid = exam["id"]
    api("PUT", f"/api/exams/{eid}/roster", {"usernames": ["alice", "bob"]}, T)
    qids = [q["id"] for q in exam["questions"]]
    layout, _ = build_layout(eid, exam["questions"])

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        P = ctx.new_page()
        P.on("pageerror", lambda e: print("TEACHER PAGE ERROR:", e))
        login_ui(P, "teacher", "teacher", "teacher123")

        # 1. print from the UI
        P.goto(f"{BASE}/teacher/exams/{eid}/scan?step=print")
        P.wait_for_timeout(1200)
        with P.expect_download() as dl:
            P.get_by_role("button", name=re.compile(r"^Download booklets")).click()
        pdf_path = dl.value.path()
        pages = render(pdf_path)
        per = layout["page_count"]
        print("booklets downloaded:", len(pages), "pages for 2 students")
        P.screenshot(path="shots/70-paper-print.png", full_page=True)

        # 2. students "write" on paper
        boxes = {b_["qid"]: b_ for b_ in layout["pages"][0]["boxes"]}
        for pg in layout["pages"][1:]:
            for b_ in pg["boxes"]:
                boxes.setdefault(b_["qid"], b_)
        def fill(img, idx):
            cx, cy, r = boxes[qids[0]]["bubbles"][idx]
            cv2.circle(img, (int(cx * S), int(cy * S)), int(r * S * 0.85), (35, 35, 35), -1)
        def write(img, text):
            x0, y0, x1, y1 = boxes[qids[1]]["rect"]
            cv2.putText(img, text, (int(x0 * S) + 25, int(y0 * S) + 70), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (30, 30, 60), 3)
        alice, bob = pages[:per], pages[per:]
        fill(alice[0], 1)
        fill(bob[0], 0)
        essay_page_a = alice[layout["pages"].index(next(p for p in layout["pages"] if any(x["qid"] == qids[1] for x in p["boxes"])))]
        essay_page_b = bob[layout["pages"].index(next(p for p in layout["pages"] if any(x["qid"] == qids[1] for x in p["boxes"])))]
        write(essay_page_a, "2H2 + O2 -> 2H2O")
        write(essay_page_b, "H2 + O2 -> H2O2")
        bob[0] = cv2.rotate(bob[0], cv2.ROTATE_180)  # fed upside down
        scan_path = "/tmp/claude-0/e2e_paper_scan.pdf"
        to_pdf(bob + alice, scan_path)

        # 3. upload through the UI
        P.goto(f"{BASE}/teacher/exams/{eid}/scan?step=upload")
        P.wait_for_timeout(1000)
        P.locator("input[type=file]").set_input_files(scan_path)
        end = time.time() + 90
        while time.time() < end:
            st = api("GET", f"/api/exams/{eid}/scans", None, T)
            if st["batches"] and all(x["status"] in ("done", "failed") for x in st["batches"]):
                break
            time.sleep(1)
        P.reload()
        P.wait_for_timeout(1500)
        P.screenshot(path="shots/71-paper-upload.png", full_page=True)
        unmatched = api("GET", f"/api/exams/{eid}/scans/unmatched", None, T)
        res = api("GET", f"/api/exams/{eid}/results", None, T)
        print("scans processed:", [x["status"] for x in st["batches"]],
              "| unmatched pages:", len(unmatched.get("pages", unmatched) if isinstance(unmatched, dict) else unmatched),
              "| attempts:", sorted(a["username"] for a in res["attempts"]))
        mcq = {a["username"]: next(x for x in a["answers"] if x["question_id"] == qids[0]) for a in res["attempts"]}
        print("bubbles read -> alice:", mcq["alice"]["points_awarded"], "pts, bob:", mcq["bob"]["points_awarded"], "pts")

        # 4. grade in the queue UI: edit each essay answer
        P.goto(f"{BASE}/teacher/exams/{eid}/scan?step=grade")
        P.wait_for_timeout(1500)
        P.get_by_role("button", name=re.compile("Balance")).first.click() if P.get_by_role("button", name=re.compile("Balance")).count() else None
        P.wait_for_timeout(800)
        P.screenshot(path="shots/72-paper-queue.png", full_page=True)
        graded = 0
        for pts, comment in (("4", "Balanced correctly."), ("1", "Water is H2O, not H2O2.")):
            edit = P.get_by_role("button", name="Edit")
            if edit.count() == 0:
                break
            edit.first.click()
            P.wait_for_timeout(300)
            P.get_by_label(re.compile(r"^Points \(out of")).first.fill(pts)
            P.get_by_label("Comment for the student").first.fill(comment)
            P.get_by_role("button", name="Save grade").first.click()
            P.wait_for_timeout(1200)
            graded += 1
        # confirm the automatic bubble reads
        for _ in range(4):
            c = P.get_by_role("button", name=re.compile(r"^(Confirm|Approve)$"))
            if c.count() == 0 or not c.first.is_enabled():
                break
            c.first.click()
            P.wait_for_timeout(800)
        print("essays graded in the queue UI:", graded)
        res = api("GET", f"/api/exams/{eid}/results", None, T)
        print("pending after grading:", res["pending_grading"],
              "| scores:", {a["username"]: a["score"] for a in res["attempts"]})

        # 5. release through the UI
        P.goto(f"{BASE}/teacher/exams/{eid}?tab=results")
        P.wait_for_timeout(1500)
        P.get_by_role("button", name="Release grades").first.click()
        P.wait_for_timeout(400)
        P.get_by_role("button", name=re.compile("^(Release|Yes)", re.I)).last.click()
        P.wait_for_timeout(1200)
        P.screenshot(path="shots/73-paper-results.png", full_page=True)

        # 6. student sees score + their scanned answer
        Sx = b.new_context(viewport={"width": 1280, "height": 860}).new_page()
        login_ui(Sx, "student", "alice", "alice123")
        Sx.goto(f"{BASE}/student/grades/{eid}")
        Sx.wait_for_timeout(1500)
        txt = Sx.locator("body").inner_text()
        scans = Sx.locator("img.stu-fbq-scan").count()
        Sx.screenshot(path="shots/74-paper-feedback.png", full_page=True)
        print("alice feedback:", (re.search(r"(\d+)\s*/\s*6", txt) or [None])[0], "| scan images shown:", scans,
              "| comment:", "Balanced correctly." in txt)
        b.close()


if __name__ == "__main__":
    main()
