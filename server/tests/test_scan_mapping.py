"""Phase 2 (f): answer sheets not printed by FairPlay -> manual page mapping."""
import json

import cv2
import httpx
import numpy as np
import pymupdf
import pytest

from app import ai

from .test_scan import PAPER_EXAM, _wait


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    monkeypatch.delenv("FAIRPLAY_AI_KEY", raising=False)
    yield
    ai.set_transport(None)


def _sheet(text: str) -> np.ndarray:
    img = np.full((1169, 827, 3), 255, np.uint8)  # A4 at 100 dpi
    cv2.putText(img, text, (60, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (20, 20, 20), 3)
    return img


def _pdf(images) -> bytes:
    doc = pymupdf.open()
    for img in images:
        ok, buf = cv2.imencode(".jpg", img)
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=buf.tobytes())
    return doc.tobytes()


def test_unprinted_upload_map_and_grade(client, teacher, monkeypatch):
    e = client.post("/api/exams", json=PAPER_EXAM, headers=teacher).json()
    client.put(f"/api/exams/{e['id']}/roster", json={"usernames": ["alice", "bob"]}, headers=teacher)
    q = [x["id"] for x in e["questions"]]
    # own answer sheets, 2 pages per student, never printed by FairPlay
    pdf = _pdf([_sheet("alice p1"), _sheet("alice p2"), _sheet("bob p1"), _sheet("bob p2")])
    assert client.post(f"/api/exams/{e['id']}/scans", headers=teacher,
                       files=[("files", ("own.pdf", pdf, "application/pdf"))]).status_code == 409
    r = client.post(f"/api/exams/{e['id']}/scans?unprinted=1", headers=teacher,
                    files=[("files", ("own.pdf", pdf, "application/pdf"))])
    assert r.status_code == 202, r.text
    st = _wait(client, teacher, e["id"])
    b = st["batches"][0]
    assert b["status"] == "done" and b["mode"] == "unprinted" and b["page_count"] == 4 and b["unmatched"] == 4

    m = client.get(f"/api/exams/{e['id']}/scans/map", headers=teacher).json()
    assert m["pages_per_student"] == 1 and len(m["pages"]) == 4
    assert all(pg["preview"].startswith("/api/uploads/") and pg["student_id"] is None for pg in m["pages"])
    assert [s["username"] for s in m["students"]] == ["alice", "bob"]

    # settings: 2 pages per student; q0-q3 on page 1, q4 on page 2
    bad = client.put(f"/api/exams/{e['id']}/scans/map-settings", headers=teacher,
                     json={"pages_per_student": 2, "question_pages": {q[4]: 3}})
    assert bad.status_code == 422
    r = client.put(f"/api/exams/{e['id']}/scans/map-settings", headers=teacher,
                   json={"pages_per_student": 2, "question_pages": {q[0]: 1, q[1]: 1, q[2]: 1, q[3]: 1, q[4]: 2}})
    assert r.status_code == 200 and r.json()["question_pages"][q[4]] == 2

    users = {s["username"]: s["id"] for s in m["students"]}
    pids = [pg["page_id"] for pg in m["pages"]]
    # wrong student id / page beyond the booklet
    assert client.post(f"/api/exams/{e['id']}/scans/map", headers=teacher, json={
        "assignments": [{"page_id": pids[0], "student_id": 999999, "page_no": 1}]}).status_code == 422
    assert client.post(f"/api/exams/{e['id']}/scans/map", headers=teacher, json={
        "assignments": [{"page_id": pids[0], "student_id": users["alice"], "page_no": 3}]}).status_code == 422
    assert client.post(f"/api/exams/{e['id']}/scans/map", headers=teacher, json={
        "assignments": [{"page_id": 987654, "student_id": users["alice"], "page_no": 1}]}).status_code == 404

    # bob's pages were fed in swapped order: fix page numbers by hand
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        text = "".join(c.get("text", "") for c in body["messages"][1]["content"] if c["type"] == "text")
        calls.append(text)
        out = {"transcript": "x", "points": 1, "confidence": 0.8, "steps": [{"text": "x", "status": "correct"}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})

    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    ai.set_transport(httpx.MockTransport(handler))
    r = client.post(f"/api/exams/{e['id']}/scans/map", headers=teacher, json={"assignments": [
        {"page_id": pids[0], "student_id": users["alice"], "page_no": 1},
        {"page_id": pids[1], "student_id": users["alice"], "page_no": 2},
        {"page_id": pids[2], "student_id": users["bob"], "page_no": 2},
        {"page_id": pids[3], "student_id": users["bob"], "page_no": 1},
    ]})
    assert r.status_code == 200, r.text
    assert r.json() == {"assigned": 4, "students": 2, "queued": 10}
    # wait for the background drafts
    import time
    end = time.time() + 30
    while time.time() < end and len(calls) < 10:
        time.sleep(0.1)
    assert len(calls) == 10
    assert all("whole answer page" in c for c in calls)

    # drafts are stored after the call returns (step verification runs first): wait for them
    end = time.time() + 30
    while True:
        queue = client.get(f"/api/exams/{e['id']}/scan/queue", headers=teacher).json()
        items = {(i["username"], i["question_id"]): i for i in queue["items"]}
        if (len(items) == 10 and all(i["draft"] for i in items.values())) or time.time() > end:
            break
        time.sleep(0.1)
    assert len(items) == 10
    pages = {pg["page_id"]: pg for pg in client.get(f"/api/exams/{e['id']}/scans/map", headers=teacher).json()["pages"]}
    assert pages[pids[2]]["page_no"] == 2 and pages[pids[2]]["student_id"] == users["bob"]
    # bob's q4 (page 2) is the 3rd uploaded page; his q0 (page 1) is the 4th
    assert items[("bob", q[4])]["page"] == 2 and items[("bob", q[0])]["page"] == 1
    assert items[("bob", q[4])]["image"] != items[("bob", q[0])]["image"]
    assert items[("alice", q[0])]["image"] == items[("alice", q[3])]["image"]  # same whole page
    # MCQ on own sheets is not bubble-read: it waits for the teacher (with an AI suggestion)
    assert items[("alice", q[0])]["status"] == "needs_review"
    assert items[("alice", q[0])]["draft"]["source"] == "ai"
    # nothing left to fix in the QR list
    assert client.get(f"/api/exams/{e['id']}/scans/unmatched", headers=teacher).json() == []
    # take a page back
    r = client.post(f"/api/exams/{e['id']}/scans/map", headers=teacher,
                    json={"assignments": [{"page_id": pids[3], "student_id": None}]})
    assert r.json()["assigned"] == 0
    assert len(client.get(f"/api/exams/{e['id']}/scans/unmatched", headers=teacher).json()) == 1
