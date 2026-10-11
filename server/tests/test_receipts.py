"""Phase 1b: submission receipts are stable, unique and verifiable by the teacher."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.routes_receipts import normalize_receipt
from app.services import RECEIPT_ALPHABET, new_receipt

from .conftest import SAMPLE_EXAM, login
from .test_slice2 import make_user


def start(client, h, exam):
    r = client.post(f"/api/student/exams/{exam['id']}/start",
                    json={"start_code": exam["start_code"]}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def submit(client, h, aid, answers=None, seq=1):
    return client.post(f"/api/student/attempts/{aid}/submit",
                       json={"answers": answers or {}, "client_seq": seq}, headers=h)


def test_receipt_format():
    for _ in range(500):
        r = new_receipt()
        assert len(r) == 9 and r[4] == "-"
        assert all(c in RECEIPT_ALPHABET for c in r.replace("-", ""))
        # no characters that are easy to misread aloud or on paper
        assert not set(r) & set("0O1IL")


def test_receipt_stable_across_repeated_submits(client, alice, live_exam):
    a = start(client, alice, live_exam)
    aid, q1 = a["attempt_id"], a["questions"][0]["id"]
    first = submit(client, alice, aid, {q1: 1}, 3).json()
    for body in ({}, {"answers": {q1: 2}, "client_seq": 99}, {"answers": {}, "client_seq": 0}):
        again = client.post(f"/api/student/attempts/{aid}/submit", json=body, headers=alice).json()
        assert again == first
    # also the same receipt in the student's exam list
    lst = client.get("/api/student/exams", headers=alice).json()
    assert next(x for x in lst if x["id"] == live_exam["id"])["attempt"]["receipt"] == first["receipt"]


def test_concurrent_submits_yield_one_receipt(client, teacher, alice, live_exam):
    """Double click / retry after a lost response: many submits at once, one receipt."""
    a = start(client, alice, live_exam)
    aid, q1 = a["attempt_id"], a["questions"][0]["id"]
    with ThreadPoolExecutor(max_workers=8) as ex:
        rs = list(ex.map(lambda i: submit(client, alice, aid, {q1: 1}, 5), range(16)))
    assert all(r.status_code == 200 for r in rs)
    receipts = {r.json()["receipt"] for r in rs}
    submitted_at = {r.json()["submitted_at"] for r in rs}
    assert len(receipts) == 1 and len(submitted_at) == 1
    res = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    assert [x["receipt"] for x in res["attempts"]] == list(receipts)


def test_receipts_unique_across_attempts_and_exams(client, teacher, live_exam):
    seen = set()
    users = ["alice", "bob", "carol"] + [f"stu{i}" for i in range(9)]
    for u in users[3:]:
        make_user(client, u, "student")
    exams = [live_exam]
    for _ in range(2):
        e = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
        exams.append(client.post(f"/api/exams/{e['id']}/open", headers=teacher).json())
    for e in exams:
        for u in users:
            h = login(client, u)
            a = start(client, h, e)
            rc = submit(client, h, a["attempt_id"]).json()["receipt"]
            assert rc not in seen
            seen.add(rc)
    assert len(seen) == len(users) * len(exams)


def test_auto_submitted_attempts_get_receipts(client, teacher, alice, bob, live_exam):
    a = start(client, alice, live_exam)
    b = start(client, bob, live_exam)
    client.post(f"/api/exams/{live_exam['id']}/close", headers=teacher)
    res = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    rcs = {x["attempt_id"]: x["receipt"] for x in res["attempts"]}
    assert rcs[a["attempt_id"]] and rcs[b["attempt_id"]] and rcs[a["attempt_id"]] != rcs[b["attempt_id"]]
    # a submit arriving after the close returns the receipt the close created
    late = submit(client, alice, a["attempt_id"], {}, 50).json()
    assert late["receipt"] == rcs[a["attempt_id"]]


# ---------------------------------------------------------------- teacher verification
def test_normalize_receipt():
    assert normalize_receipt("k7q2-m9xd") == "K7Q2-M9XD"
    assert normalize_receipt(" K7Q2 M9XD ") == "K7Q2-M9XD"
    assert normalize_receipt("K7Q2M9XD") == "K7Q2-M9XD"
    assert normalize_receipt("K7Q2-M9X") is None
    assert normalize_receipt("K7Q2-M9XD-") == "K7Q2-M9XD"
    assert normalize_receipt("../../etc") is None


def test_teacher_verifies_receipt(client, teacher, alice, bob, live_exam):
    a = start(client, alice, live_exam)
    q1, q3 = a["questions"][0]["id"], a["questions"][2]["id"]
    s = submit(client, alice, a["attempt_id"], {q1: 1, q3: "paris"}, 2).json()
    start(client, bob, live_exam)  # bob still writing: no receipt yet
    url = f"/api/exams/{live_exam['id']}/receipts"
    r = client.get(f"{url}/{s['receipt']}", headers=teacher)
    assert r.status_code == 200
    assert r.json() == {
        "valid": True, "receipt": s["receipt"], "exam_id": live_exam["id"],
        "attempt_id": a["attempt_id"], "username": "alice", "name": "Alice Chen",
        "status": "submitted", "submitted_at": s["submitted_at"], "auto_submitted": False,
        "late": False, "answered": 2, "question_count": 3}
    # typed loosely by a person
    loose = s["receipt"].lower().replace("-", " ")
    assert client.get(f"{url}/{loose}", headers=teacher).json()["attempt_id"] == a["attempt_id"]
    # unknown / malformed receipts
    for bad in ("ZZZZ-ZZZZ", "nope", "AAAA-AAA"):
        r = client.get(f"{url}/{bad}", headers=teacher)
        assert r.status_code == 404 and r.json() == {"detail": "Receipt not found"}


def test_receipt_lookup_is_scoped_to_exam_and_owner(client, teacher, alice, live_exam):
    a = start(client, alice, live_exam)
    rc = submit(client, alice, a["attempt_id"]).json()["receipt"]
    other_exam = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    # right receipt, wrong exam -> not found (a receipt proves one exam only)
    r = client.get(f"/api/exams/{other_exam['id']}/receipts/{rc}", headers=teacher)
    assert r.status_code == 404
    # another teacher cannot look into this exam at all
    t2 = make_user(client, "teacher2", "teacher", "Other Teacher")
    r = client.get(f"/api/exams/{live_exam['id']}/receipts/{rc}", headers=t2)
    assert r.status_code == 404 and r.json() == {"detail": "Exam not found"}
    # students cannot use it
    r = client.get(f"/api/exams/{live_exam['id']}/receipts/{rc}", headers=alice)
    assert r.status_code == 403
    assert client.get(f"/api/exams/{live_exam['id']}/receipts/{rc}").status_code == 401
