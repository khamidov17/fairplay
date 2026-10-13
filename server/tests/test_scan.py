"""Slice 6 D — Scan & Grade: print booklets, read synthetic scans, AI drafts, approve, release."""
import json
import random
import time

import cv2
import httpx
import numpy as np
import pymupdf
import pytest

from app import ai
from app.scan import pipeline
from app.scan.print import build_layout

DPI = 200
S = DPI / 72

PAPER_EXAM = {
    "title": "Paper Midterm",
    "duration_min": 50,
    "course": "MATH 201",
    "delivery": "paper",
    "questions": [
        {"type": "mcq", "prompt": "Which is 2+2?", "points": 2, "options": ["3", "4", "5", "6"],
         "correct_index": 1, "topics": ["arithmetic"]},
        {"type": "numeric", "prompt": "Write pi to two decimals.", "points": 3, "answer_number": 3.14,
         "tolerance": 0.01, "topics": ["constants"]},
        {"type": "short", "prompt": "Capital of France?", "points": 2, "accepted_answers": ["Paris"],
         "topics": ["geography"]},
        {"type": "essay", "prompt": "Explain the integral test.", "points": 5, "answer_lines": 8,
         "topics": ["series"]},
        {"type": "essay", "prompt": "Show your work: integrate x e^x.", "points": 4, "answer_lines": 22,
         "topics": ["integration by parts"]},
    ],
}


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    monkeypatch.delenv("FAIRPLAY_AI_KEY", raising=False)
    yield
    ai.set_transport(None)


def _render(pdf: bytes) -> list[np.ndarray]:
    out = []
    with pymupdf.open(stream=pdf) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=DPI)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
            out.append(cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    return out


def _fill_bubble(img, box, idx):
    cx, cy, r = box["bubbles"][idx]
    cv2.circle(img, (int(cx * S), int(cy * S)), int(r * S * 0.85), (35, 35, 35), -1)


def _write(img, box, text):
    x0, y0, x1, y1 = box["rect"]
    cv2.putText(img, text, (int(x0 * S) + 25, int(y0 * S) + 70), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                (30, 30, 60), 3)


def _boxes(layout, page_no):
    return {b["qid"]: b for b in layout["pages"][page_no - 1]["boxes"]}


def _to_pdf(images: list[np.ndarray]) -> bytes:
    doc = pymupdf.open()
    for img in images:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        h, w = img.shape[:2]
        page = doc.new_page(width=w * 72 / DPI, height=h * 72 / DPI)
        page.insert_image(page.rect, stream=buf.tobytes())
    return doc.tobytes()


def _wait(client, teacher, exam_id, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/exams/{exam_id}/scans", headers=teacher)
        assert r.status_code == 200, r.text
        body = r.json()
        if body["batches"] and all(b["status"] in ("done", "failed") for b in body["batches"]):
            return body
        time.sleep(0.2)
    raise AssertionError("scan processing did not finish")


def _users(client, teacher, exam_id):
    r = client.get(f"/api/exams/{exam_id}/roster", headers=teacher)
    return {u["username"]: u for u in r.json()["all_students"]}


@pytest.fixture
def paper(client, teacher):
    r = client.post("/api/exams", json=PAPER_EXAM, headers=teacher)
    assert r.status_code == 201, r.text
    e = r.json()
    r = client.put(f"/api/exams/{e['id']}/roster", json={"usernames": ["alice", "bob"]}, headers=teacher)
    assert r.status_code == 200, r.text
    assert [s["username"] for s in r.json()["students"]] == ["alice", "bob"]
    assert r.json()["source"] == "picked"
    return e


def _scan_class(client, teacher, e):
    """Print, mark, rotate, shuffle and upload; returns (layout, users, upload json)."""
    r = client.get(f"/api/exams/{e['id']}/print.pdf", headers=teacher)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    layout, _ = build_layout(e["id"], e["questions"])
    n = layout["page_count"]
    assert n == 2
    pages = _render(r.content)
    assert len(pages) == 2 * n  # alice + bob
    q = [x["id"] for x in e["questions"]]
    p1 = _boxes(layout, 1)
    p2 = _boxes(layout, 2)
    alice1, alice2, bob1, bob2 = pages
    _fill_bubble(alice1, p1[q[0]], 1)                 # B: correct
    _write(alice1, p1[q[1]], "3.14")
    _write(alice1, p1[q[2]], "Paris")
    _write(alice1, p1[q[3]], "terms positive decreasing")
    _write(alice2, p2[q[4]], "x e^x - e^x + C")
    _fill_bubble(bob1, p1[q[0]], 0)                   # A and C: multiple
    _fill_bubble(bob1, p1[q[0]], 2)
    _write(bob1, p1[q[1]], "2.5")
    _write(bob1, p1[q[3]], "it converges")
    # bob leaves the short answer and page 2 blank
    # blank copy (unnamed) for carol
    r = client.get(f"/api/exams/{e['id']}/print.pdf?blank=1", headers=teacher)
    assert r.status_code == 200
    blank1, blank2 = _render(r.content)
    cv2.putText(blank1, "Carol Wang", (int(layout["name_box"][0] * S) + 10, int(layout["name_box"][3] * S) - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (30, 30, 30), 3)
    _fill_bubble(blank1, p1[q[0]], 3)
    alice2 = cv2.rotate(alice2, cv2.ROTATE_90_CLOCKWISE)  # fed in sideways
    bob1 = cv2.rotate(bob1, cv2.ROTATE_180)               # upside down
    white = np.full_like(alice1, 255)                     # a page without any code
    named = [alice1, alice2, bob1, bob2, white]
    random.Random(7).shuffle(named)
    scans = named + [blank1, blank2]                      # blank booklet stays in order
    pdf = _to_pdf(scans)
    r = client.post(f"/api/exams/{e['id']}/scans", headers=teacher,
                    files=[("files", ("class.pdf", pdf, "application/pdf"))])
    assert r.status_code == 202, r.text
    return layout, _users(client, teacher, e["id"]), r.json()


def _attempt(client, teacher, exam_id, username):
    res = client.get(f"/api/exams/{exam_id}/results", headers=teacher).json()
    return next((a for a in res["attempts"] if a["username"] == username), None)


def test_print_requires_layout_and_roster(client, teacher, paper):
    # no scans before printing
    r = client.post(f"/api/exams/{paper['id']}/scans", headers=teacher,
                    files=[("files", ("x.pdf", b"%PDF-1.4 nothing", "application/pdf"))])
    assert r.status_code == 409
    info = client.get(f"/api/exams/{paper['id']}/print", headers=teacher).json()
    assert info["pages_per_student"] == 2 and info["student_count"] == 2 and info["printed_at"] is None
    r = client.get(f"/api/exams/{paper['id']}/print.pdf?blank=1", headers=teacher)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    with pymupdf.open(stream=r.content) as doc:
        assert doc.page_count == 2
        assert "Blank copy" in doc[0].get_text()
    r = client.get(f"/api/exams/{paper['id']}/print.pdf", headers=teacher)
    with pymupdf.open(stream=r.content) as doc:
        assert doc.page_count == 4
        assert "Alice Chen" in doc[0].get_text() and "Bob Karimov" in doc[2].get_text()
    # answer space edits change the layout
    q4 = paper["questions"][4]["id"]
    r = client.put(f"/api/exams/{paper['id']}/print-settings", json={"answer_lines": {q4: 3}}, headers=teacher)
    assert r.status_code == 200, r.text
    assert r.json()["layout_changed"] is True
    assert next(x for x in r.json()["questions"] if x["question_id"] == q4)["answer_lines"] == 3
    r = client.put(f"/api/exams/{paper['id']}/print-settings", json={"answer_lines": {q4: 99}}, headers=teacher)
    assert r.status_code == 422
    # not an image or pdf
    client.get(f"/api/exams/{paper['id']}/print.pdf", headers=teacher)
    r = client.post(f"/api/exams/{paper['id']}/scans", headers=teacher,
                    files=[("files", ("x.txt", b"hello world, not a scan", "text/plain"))])
    assert r.status_code == 422
    # roster validation + default
    r = client.put(f"/api/exams/{paper['id']}/roster", json={"usernames": ["nobody"]}, headers=teacher)
    assert r.status_code == 422
    r = client.put(f"/api/exams/{paper['id']}/roster", json={"usernames": []}, headers=teacher)
    assert r.json()["source"] == "class" and len(r.json()["students"]) >= 3   # the exam's class
    info = client.get(f"/api/exams/{paper['id']}/print", headers=teacher).json()
    assert info["roster_source"] == "class" and info["booklet_parts"] == 1 and info["needs_roster"] is False
    # overview lists the paper exam
    ov = client.get("/api/scan/exams", headers=teacher).json()
    assert [x["exam_id"] for x in ov] == [paper["id"]]
    assert ov[0]["printed"] is True


def test_scan_match_read_assign_and_fallback_drafts(client, teacher, paper):
    e = paper
    layout, users, up = _scan_class(client, teacher, e)
    st = _wait(client, teacher, e["id"])
    b = st["batches"][0]
    assert b["batch_id"] == up["batch_id"] and b["status"] == "done", b
    assert b["page_count"] == 7 and b["processed"] == 7
    assert b["matched"] == 4 and b["unmatched"] == 3, b
    assert st["students_scanned"] == 2 and st["students_expected"] == 2

    q = [x["id"] for x in e["questions"]]
    exam = client.get(f"/api/exams/{e['id']}", headers=teacher).json()
    assert exam["status"] == "closed"  # collected

    # alice: bubble B read -> automatic 2 points; other answers pending review
    al = _attempt(client, teacher, e["id"], "alice")
    assert al["status"] == "submitted"
    ans = {a["question_id"]: a for a in al["answers"]}
    assert ans[q[0]]["given"] == 1 and ans[q[0]]["points_awarded"] == 2 and not ans[q[0]]["pending"]
    for qid in q[1:]:
        assert ans[qid]["pending"] and ans[qid]["image"].startswith("/api/uploads/")
    assert al["needs_grading"] == 4 and al["score"] == 2
    # crops are real images
    img = client.get(ans[q[1]]["image"])
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"

    # queue: bob's multiple marks need review; drafts are fallbacks (AI off) — never grades
    queue = client.get(f"/api/exams/{e['id']}/scan/queue", headers=teacher).json()
    assert queue["ai_enabled"] is False
    items = {(i["username"], i["question_id"]): i for i in queue["items"]}
    assert items[("alice", q[0])]["status"] == "auto" and items[("alice", q[0])]["mcq"] == 1
    assert items[("alice", q[0])]["auto_points"] == 2
    bob_mcq = items[("bob", q[0])]
    assert bob_mcq["marks"] == "multiple" and bob_mcq["status"] == "needs_review"
    assert bob_mcq["draft"]["source"] == "bubbles"
    assert items[("bob", q[2])]["blank"] is True and items[("bob", q[2])]["draft"]["source"] == "blank"
    assert items[("bob", q[4])]["blank"] is True
    assert items[("alice", q[1])]["blank"] is False
    d = items[("alice", q[3])]["draft"]
    assert d["source"] == "fallback" and d["points"] == 0 and d["confidence"] == 0
    assert d["comment"].startswith("AI is off") and d["low_confidence"] is True
    assert items[("alice", q[3])]["grade"] is None
    assert queue["groups"] == []  # "grade by hand" placeholders are never grouped for bulk approval
    aid = items[("alice", q[3])]["attempt_id"]
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{aid}/{q[3]}/approve", headers=teacher)
    assert r.status_code == 409  # nothing to approve: enter the points
    r = client.post(f"/api/exams/{e['id']}/scan/approve-bulk", headers=teacher,
                    json={"items": [{"attempt_id": aid, "question_id": q[3]}]})
    assert r.json() == {"approved": 0, "skipped": 1}
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{aid}/{q[3]}/approve",
                    json={"points": 4, "comment": "Good"}, headers=teacher)
    assert r.status_code == 200 and r.json()["grade"]["points"] == 4

    # unmatched: the white page + 2 pages of the blank copy
    um = client.get(f"/api/exams/{e['id']}/scans/unmatched", headers=teacher).json()
    assert len(um) == 3
    nocode = [p for p in um if p["qr_found"] == 0]
    blanks = sorted([p for p in um if p["qr_found"] > 0], key=lambda p: p["seq"])
    assert len(nocode) == 1 and nocode[0]["reason"].startswith("No FairPlay code")
    assert nocode[0]["preview"].startswith("/api/uploads/")
    assert [p["page_no"] for p in blanks] == [1, 2] and blanks[0]["name_crop"]
    assert "Blank copy" in blanks[0]["reason"]

    # manual assign: blank copy page 1 -> carol, follow-on page 2 assigned too
    carol = users["carol"]
    r = client.post(f"/api/exams/{e['id']}/scans/pages/{blanks[0]['page_id']}/assign",
                    json={"student_id": carol["id"], "page_no": 1}, headers=teacher)
    assert r.status_code == 200, r.text
    assert [(x["page_no"]) for x in r.json()["assigned"]] == [1, 2]
    ca = _attempt(client, teacher, e["id"], "carol")
    cans = {a["question_id"]: a for a in ca["answers"]}
    assert cans[q[0]]["given"] == 3 and cans[q[0]]["points_awarded"] == 0  # D, wrong
    assert cans[q[4]]["image"]
    # the no-code page: assign by hand (cropped assuming an upright full-page scan)
    r = client.post(f"/api/exams/{e['id']}/scans/pages/{nocode[0]['page_id']}/assign",
                    json={"student_id": users["bob"]["id"], "page_no": 2}, headers=teacher)
    assert r.status_code == 200, r.text
    bo = _attempt(client, teacher, e["id"], "bob")
    assert next(a for a in bo["answers"] if a["question_id"] == q[4])["image"]
    assert client.get(f"/api/exams/{e['id']}/scans/unmatched", headers=teacher).json() == []
    r = client.post(f"/api/exams/{e['id']}/scans/pages/{nocode[0]['page_id']}/ignore", headers=teacher)
    assert r.status_code == 409
    r = client.post(f"/api/exams/{e['id']}/scans/pages/{nocode[0]['page_id']}/assign",
                    json={"student_id": users["bob"]["id"], "page_no": 9}, headers=teacher)
    assert r.status_code == 422

    # release blocked while drafts wait for approval
    r = client.post(f"/api/exams/{e['id']}/release", json={}, headers=teacher)
    assert r.status_code == 409


def _fake_ai(style: str, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if style == "anthropic":
            text = "".join(c.get("text", "") for c in body["messages"][0]["content"] if c["type"] == "text")
            assert any(c["type"] == "image" for c in body["messages"][0]["content"])
        else:
            text = "".join(c.get("text", "") for c in body["messages"][1]["content"] if c["type"] == "text")
            assert any(c["type"] == "image_url" for c in body["messages"][1]["content"])
        if "pi to two decimals" in text:
            out = {"transcript": "3.14", "points": 3, "comment": "Correct value.", "confidence": 0.95,
                   "mistake_tags": []}
        elif "integral test" in text:
            out = {"transcript": "terms positive decreasing", "points": 3, "comment": "Missing the integral.",
                   "confidence": 0.6, "mistake_tags": ["Incomplete"]}
        elif "Capital" in text:
            out = {"transcript": "Paris", "points": 2, "comment": "", "confidence": 0.9, "mistake_tags": []}
        else:
            out = {"transcript": "x e^x - e^x + C", "points": 9, "comment": "Good.", "confidence": 0.8,
                   "mistake_tags": []}
        payload = json.dumps(out)
        if style == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": payload}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Here: " + payload}}]})
    return httpx.MockTransport(handler)


def test_ai_drafts_approve_bulk_regrade_release(client, teacher, paper, alice, monkeypatch):
    e = paper
    q = [x["id"] for x in e["questions"]]
    _, users, up = _scan_class(client, teacher, e)
    _wait(client, teacher, e["id"])

    # turn AI on (Anthropic style) and re-run drafts for the batch
    calls: list = []
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "anthropic")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "test-key")
    ai.set_transport(_fake_ai("anthropic", calls))
    r = client.post(f"/api/exams/{e['id']}/scans/{up['batch_id']}/rerun-ai", headers=teacher)
    assert r.status_code == 202, r.text
    assert r.json()["queued"] == 9  # alice 4 + bob 5 (mcq multiple, numeric, short blank, 2 essays)
    _wait(client, teacher, e["id"])
    # only answers with ink go to the AI (blank boxes and bubbles use rules)
    assert len(calls) == 6

    queue = client.get(f"/api/exams/{e['id']}/scan/queue", headers=teacher).json()
    items = {(i["username"], i["question_id"]): i for i in queue["items"]}
    dn = items[("alice", q[1])]["draft"]
    assert dn["source"] == "ai" and dn["points"] == 3 and dn["transcript"] == "3.14"
    assert dn["key_match"] is True                     # matches the key: a hint, nothing more
    de = items[("alice", q[3])]["draft"]
    assert de["source"] == "ai" and de["points"] == 3 and de["low_confidence"] is True
    assert de["mistake_tags"] == ["incomplete"]
    assert items[("alice", q[4])]["draft"]["points"] == 4  # clamped to the question's points
    assert items[("bob", q[1])]["draft"]["transcript"] == "3.14"  # (the fake reads every box the same)
    # nothing is applied yet
    assert all(i["grade"] is None for i in queue["items"] if i["status"] != "auto")
    al = _attempt(client, teacher, e["id"], "alice")
    assert al["score"] == 2 and al["needs_grading"] == 4

    # groups: alice + bob numeric drafts are identical -> one group of 2
    g = [x for x in queue["groups"] if x["question_id"] == q[1]]
    assert len(g) == 1 and g[0]["count"] == 2 and "3.14" in g[0]["label"]
    r = client.post(f"/api/exams/{e['id']}/scan/approve-bulk", json={"items": g[0]["items"]}, headers=teacher)
    assert r.json() == {"approved": 2, "skipped": 0}
    r = client.post(f"/api/exams/{e['id']}/scan/approve-bulk", json={"items": g[0]["items"]}, headers=teacher)
    assert r.json() == {"approved": 0, "skipped": 2}

    # regrade (OpenAI-compatible style) replaces the draft only
    calls2: list = []
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    ai.set_transport(_fake_ai("openai", calls2))
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[2]}/regrade",
                    json={"hint": "accept lowercase"}, headers=teacher)
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["transcript"] == "Paris" and r.json()["draft"]["key_match"] is True
    assert r.json()["draft"]["points"] == 2 and r.json()["draft"]["source"] == "ai"
    assert "accept lowercase" in calls2[0]["messages"][1]["content"][0]["text"]

    # approve as-is, edit (override), validation
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[2]}/approve", headers=teacher)
    assert r.status_code == 200 and r.json()["grade"]["points"] == 2
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[3]}/approve",
                    json={"points": 4.5, "comment": "Nice reasoning."}, headers=teacher)
    assert r.status_code == 200 and r.json()["grade"]["points"] == 4.5
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[4]}/approve",
                    json={"points": 40}, headers=teacher)
    assert r.status_code == 422
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[4]}/approve", headers=teacher)
    assert r.json()["needs_grading"] == 0
    al = _attempt(client, teacher, e["id"], "alice")
    assert al["score"] == 2 + 3 + 2 + 4.5 + 4 and al["needs_grading"] == 0

    # bulk approve the rest (bob)
    queue = client.get(f"/api/exams/{e['id']}/scan/queue", headers=teacher).json()
    rest = [{"attempt_id": i["attempt_id"], "question_id": i["question_id"]}
            for i in queue["items"] if i["status"] == "needs_review"]
    assert len(rest) == 4
    r = client.post(f"/api/exams/{e['id']}/scan/approve-bulk", json={"items": rest}, headers=teacher)
    assert r.json()["approved"] == 4
    bo = _attempt(client, teacher, e["id"], "bob")
    assert bo["needs_grading"] == 0 and bo["score"] == 3 + 3  # numeric (fake "3.14") + essay draft 3

    # release -> student feedback shows the grades and the scanned answer
    r = client.post(f"/api/exams/{e['id']}/release", json={}, headers=teacher)
    assert r.status_code == 200, r.text
    fb = client.get(f"/api/student/grades/{e['id']}", headers=alice).json()
    assert fb["score"] == 15.5 and fb["total_points"] == 16
    fq = {x["question_id"]: x for x in fb["questions"]}
    assert fq[q[0]]["status"] == "correct" and fq[q[0]]["given"] == 1
    assert fq[q[3]]["points_awarded"] == 4.5 and fq[q[3]]["comment"] == "Nice reasoning."
    assert fq[q[3]]["image"].startswith("/api/uploads/")
    assert fq[q[2]]["given"] == "Paris"
    grades = client.get("/api/student/grades", headers=alice).json()
    assert grades[0]["exam_id"] == e["id"] and grades[0]["percent"] == 96.9
    gb = client.get("/api/gradebook", headers=teacher).json()
    alice_row = next(s for s in gb["students"] if s["username"] == "alice")
    assert alice_row["cells"][e["id"]]["state"] == "graded"


def test_ai_unavailable_falls_back(client, teacher, paper, monkeypatch):
    e = paper
    q = [x["id"] for x in e["questions"]]
    _, _, up = _scan_class(client, teacher, e)
    _wait(client, teacher, e["id"])
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    ai.set_transport(httpx.MockTransport(lambda req: httpx.Response(500, json={"error": "boom"})))
    al = _attempt(client, teacher, e["id"], "alice")
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[3]}/regrade", headers=teacher)
    d = r.json()["draft"]
    assert d["source"] == "fallback" and d["points"] == 0 and "grade by hand" in d["comment"]


def test_pipeline_bubbles_relative_threshold():
    """Unit check of the bubble reader on a synthetic canonical page."""
    canon = np.full((pipeline.CANON_H, pipeline.CANON_W), 255, np.uint8)
    bubbles = [[150 + 36 * i, 300, 8] for i in range(4)]
    for cx, cy, r in bubbles:
        cv2.circle(canon, (int(cx * S), int(cy * S)), int(r * S), 60, 2)
    one = canon.copy()
    cv2.circle(one, (int(bubbles[2][0] * S), int(300 * S)), int(7 * S), 30, -1)
    assert pipeline.read_bubbles(one, bubbles)["marks"] == "one"
    assert pipeline.read_bubbles(one, bubbles)["mcq"] == 2
    assert pipeline.read_bubbles(canon, bubbles)["marks"] == "none"
    faint = canon.copy()
    cx, cy = int(bubbles[1][0] * S), int(300 * S)
    cv2.line(faint, (cx - 12, cy - 12), (cx + 12, cy + 12), 40, 3)
    cv2.line(faint, (cx - 12, cy + 12), (cx + 12, cy - 12), 40, 3)
    res = pipeline.read_bubbles(faint, bubbles)
    assert res["marks"] in ("unclear", "one") and res["mcq"] == 1


def test_reassigning_a_page_moves_its_answers(client, teacher, paper):
    """Audit L-12: a matched page given to another student leaves the first one's paper."""
    from sqlalchemy import select
    from app import db as dbmod
    from app.scan.models import ScanPage
    e = paper
    _layout, users, _up = _scan_class(client, teacher, e)
    _wait(client, teacher, e["id"])
    alice_id, bob_id = users["alice"]["id"], users["bob"]["id"]
    q = [x["id"] for x in e["questions"]]
    before = _attempt(client, teacher, e["id"], "alice")
    assert any(a["question_id"] == q[1] and a.get("image") for a in before["answers"])

    async def alice_page1():
        async with dbmod._db.sessionmaker() as s:
            return (await s.execute(select(ScanPage.id).where(
                ScanPage.exam_id == e["id"], ScanPage.student_id == alice_id, ScanPage.page_no == 1))).scalar_one()
    pid = client.portal.call(alice_page1)
    r = client.post(f"/api/exams/{e['id']}/scans/pages/{pid}/assign", headers=teacher,
                    json={"student_id": bob_id, "page_no": 1, "follow_on": False})
    assert r.status_code == 200, r.text
    after = _attempt(client, teacher, e["id"], "alice")
    page1 = {q[0], q[1], q[2], q[3]}
    assert not [a for a in after["answers"] if a["question_id"] in page1 and a.get("image")]
    assert after["score"] == 0                       # her bubble point went with the page
    bob = _attempt(client, teacher, e["id"], "bob")
    assert any(a["question_id"] == q[1] and a.get("image") for a in bob["answers"])


def test_key_match_never_raises_the_draft():
    """Audit L-07: what the AI read can match the key while the AI itself gives 0."""
    import asyncio
    from app.scan import grading as sg

    async def fake(system, prompt, images=None, max_tokens=0):
        return {"transcript": "the answer is 3.14", "points": 0, "comment": "No working shown.",
                "confidence": 0.3, "mistake_tags": []}
    q = {"id": "q1", "type": "numeric", "prompt": "pi", "points": 3, "answer_number": 3.14, "tolerance": 0.005}
    orig_en, orig_c = sg.ai.enabled, sg.ai.complete_json
    sg.ai.enabled, sg.ai.complete_json = (lambda: True), fake
    try:
        d = asyncio.run(sg.draft_for(q, {"image": "x"}, b"img"))
    finally:
        sg.ai.enabled, sg.ai.complete_json = orig_en, orig_c
    assert d["key_match"] is True and d["source"] == "ai"
    assert d["points"] == 0 and d["confidence"] == 0.3 and d["low_confidence"] is True


def test_booklets_come_in_parts_and_never_for_everyone(client, teacher, monkeypatch):
    """Audit L-10: no whole-school booklet PDF by default; big rosters print in parts."""
    from app import routes_scan
    body = {k: v for k, v in PAPER_EXAM.items()}
    r = client.post("/api/exams", json=body, headers=teacher)
    e = r.json()
    from app.db import get_database
    from app.models import Exam

    async def no_class():
        async with get_database().sessionmaker() as s:
            x = await s.get(Exam, e["id"])
            x.class_id = None
            await s.commit()
    client.portal.call(no_class)
    info = client.get(f"/api/exams/{e['id']}/print", headers=teacher).json()
    assert info["roster_source"] == "all" and info["needs_roster"] is True
    r = client.get(f"/api/exams/{e['id']}/print.pdf", headers=teacher)
    assert r.status_code == 409 and "Pick the students" in r.text
    assert client.get(f"/api/exams/{e['id']}/print.pdf?blank=1", headers=teacher).status_code == 200

    client.put(f"/api/exams/{e['id']}/roster", json={"usernames": ["alice", "bob", "carol"]}, headers=teacher)
    monkeypatch.setattr(routes_scan, "BOOKLETS_PER_PDF", 2)
    assert client.get(f"/api/exams/{e['id']}/print", headers=teacher).json()["booklet_parts"] == 2
    r1 = client.get(f"/api/exams/{e['id']}/print.pdf?part=1", headers=teacher)
    r2 = client.get(f"/api/exams/{e['id']}/print.pdf?part=2", headers=teacher)
    assert r1.headers["x-fairplay-parts"] == "2" and "part2-of-2" in r2.headers["content-disposition"]
    with pymupdf.open(stream=r1.content) as d1, pymupdf.open(stream=r2.content) as d2:
        assert d1.page_count == 2 * d2.page_count            # 2 students, then 1
    assert client.get(f"/api/exams/{e['id']}/print.pdf?part=3", headers=teacher).status_code == 422
