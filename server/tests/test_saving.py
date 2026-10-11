"""Phase 1b: answer saving is idempotent, ordered and never loses a newer answer.

The rule (CONTRACT "Student API", NOTES "Database writes are small and idempotent"):
a save is applied only if its client_seq is strictly greater than the stored one, so a
late, duplicated or reordered save can never overwrite a newer one. Saves after the
deadline, after submit or after the teacher closed the exam are refused with 409.
"""
from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app import routes_student
from app.timeutil import utcnow

from .conftest import SAMPLE_EXAM, login


def start(client, h, exam):
    r = client.post(f"/api/student/exams/{exam['id']}/start",
                    json={"start_code": exam["start_code"]}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def put(client, h, aid, answers, seq):
    return client.put(f"/api/student/attempts/{aid}/answers",
                      json={"answers": answers, "client_seq": seq}, headers=h)


def stored(client, h, aid):
    return client.get(f"/api/student/attempts/{aid}", headers=h).json()["answers"]


def results_by_user(client, teacher, exam_id):
    res = client.get(f"/api/exams/{exam_id}/results", headers=teacher).json()
    return {a["username"]: a for a in res["attempts"]}


@pytest.fixture
def attempt(client, alice, live_exam):
    a = start(client, alice, live_exam)
    return a["attempt_id"], [q["id"] for q in a["questions"]]


# ---------------------------------------------------------------- ordering / idempotency
def test_duplicate_save_is_idempotent(client, alice, attempt):
    aid, (q1, q2, q3) = attempt
    body = {q1: 1, q3: "paris"}
    r1 = put(client, alice, aid, body, 4)
    assert r1.json()["saved"] is True
    # The exact same save delivered again (lost response + retry) changes nothing and is
    # reported as already saved, so the client can mark it synced.
    for _ in range(3):
        r = put(client, alice, aid, body, 4)
        assert r.status_code == 200
        assert r.json()["client_seq"] == 4
        assert r.json()["saved"] is True
    assert stored(client, alice, aid) == body


def test_same_seq_different_answers_is_not_reported_saved(client, alice, attempt):
    """Two tabs (or two computers) can produce the same client_seq with different
    answers. The second one is NOT stored, so it must not be told saved:true."""
    aid, (q1, *_rest) = attempt
    assert put(client, alice, aid, {q1: 1}, 7).json()["saved"] is True
    r = put(client, alice, aid, {q1: 2}, 7)
    assert r.status_code == 200 and r.json() == {**r.json(), "saved": False, "client_seq": 7}
    assert stored(client, alice, aid) == {q1: 1}


def test_out_of_order_delivery_keeps_newest(client, alice, attempt):
    """Saves 1..50, each a full snapshot, delivered in random order: the newest wins."""
    aid, (q1, q2, q3) = attempt
    snaps = {seq: {q1: seq % 3, q2: seq * 1.5, q3: f"v{seq}"} for seq in range(1, 51)}
    order = list(snaps)
    random.Random(42).shuffle(order)
    for seq in order:
        assert put(client, alice, aid, snaps[seq], seq).status_code == 200
    assert stored(client, alice, aid) == snaps[50]
    # a very late straggler changes nothing
    assert put(client, alice, aid, {q1: 0}, 3).json()["saved"] is False
    assert stored(client, alice, aid) == snaps[50]


def test_seq_can_jump_and_clears_are_saved(client, alice, attempt):
    aid, (q1, q2, q3) = attempt
    assert put(client, alice, aid, {q1: 1, q2: 2}, 1).json()["saved"]
    # client cleared q2 (answer removed from the snapshot) and jumped seq (bump past server)
    assert put(client, alice, aid, {q1: 1}, 1000).json()["saved"]
    assert stored(client, alice, aid) == {q1: 1}
    # null is a stored "not answered"
    assert put(client, alice, aid, {q1: None}, 1001).json()["saved"]
    assert stored(client, alice, aid) == {q1: None}


def test_concurrent_saves_from_two_tabs(client, alice, attempt):
    """Two tabs hammer the same attempt concurrently with interleaved seqs. Whatever
    order the server processes them in, the stored state is the highest seq's snapshot
    and the stored client_seq equals the max."""
    aid, (q1, q2, q3) = attempt
    seqs = list(range(1, 121))
    random.Random(7).shuffle(seqs)

    def send(seq):
        tab = "A" if seq % 2 else "B"
        return seq, put(client, alice, aid, {q1: seq % 3, q3: f"{tab}{seq}"}, seq)

    with ThreadPoolExecutor(max_workers=12) as ex:
        out = list(ex.map(send, seqs))
    assert all(r.status_code == 200 for _, r in out)
    saved = [seq for seq, r in out if r.json()["saved"]]
    assert 120 in saved
    assert stored(client, alice, aid) == {q1: 0, q3: "B120"}
    # the response always reports the stored seq when not saved (never larger than 120)
    assert all(r.json()["client_seq"] <= 120 for _, r in out)


def test_concurrent_save_and_submit_keep_newest(client, teacher, alice, live_exam):
    """A save racing with submit: the final answers are those of the highest client_seq
    among everything accepted, and the submit's receipt is stable."""
    a = start(client, alice, live_exam)
    aid, q1 = a["attempt_id"], a["questions"][0]["id"]
    q3 = a["questions"][2]["id"]
    put(client, alice, aid, {q1: 0}, 1)

    def save(seq):
        return put(client, alice, aid, {q1: 1, q3: f"save{seq}"}, seq)

    def submit():
        return client.post(f"/api/student/attempts/{aid}/submit",
                           json={"answers": {q1: 1, q3: "submit"}, "client_seq": 5}, headers=alice)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(save, s) for s in (2, 3, 4)] + [ex.submit(submit)] + \
               [ex.submit(save, s) for s in (6, 7)]
        rs = [f.result() for f in futs]
    sub = rs[3]
    assert sub.status_code == 200
    saved_after = [r for r in rs[4:] if r.status_code == 200 and r.json()["saved"]]
    res = results_by_user(client, teacher, live_exam["id"])["alice"]
    given = {x["question_id"]: x["given"] for x in res["answers"]}
    # either a newer save landed before the submit (and must be kept), or the submit's
    # body is the newest accepted snapshot; an older save may never win.
    if saved_after:
        newest = max(r.json()["client_seq"] for r in saved_after)
        assert given[q3] == f"save{newest}"
    else:
        assert given[q3] == "submit"
    assert res["receipt"] == sub.json()["receipt"]


@pytest.mark.parametrize("submit_seq,expected", [(3, "save 9"), (12, "submit 12")])
def test_save_landing_between_submit_read_and_write(client, teacher, alice, live_exam, attempt,
                                                    monkeypatch, submit_seq, expected):
    """Deterministic version of the race: submit has read the attempt (stored seq 2), then
    a save with seq 9 lands before submit writes. The older snapshot (submit seq 3) must not
    overwrite it; a newer one (submit seq 12) still wins."""
    import sqlite3

    from app.db import get_database

    url = get_database().engine.url
    if url.get_backend_name() != "sqlite":
        pytest.skip("injects the racing save with a direct SQLite write")
    aid, (q1, q2, q3) = attempt
    put(client, alice, aid, {q3: "save 2"}, 2)
    real = routes_student.grade_attempt
    fired = []

    def grade_then_race(questions, answers, grades):
        if not fired:  # between submit's read and its UPDATE: another request saves seq 9
            fired.append(1)
            con = sqlite3.connect(url.database, timeout=10)
            con.execute("UPDATE attempts SET answers = ?, client_seq = 9 WHERE id = ?",
                        ('{"%s": "save 9"}' % q3, aid))
            con.commit()
            con.close()
        return real(questions, answers, grades)

    monkeypatch.setattr(routes_student, "grade_attempt", grade_then_race)
    r = client.post(f"/api/student/attempts/{aid}/submit",
                    json={"answers": {q3: f"submit {submit_seq}"}, "client_seq": submit_seq},
                    headers=alice)
    assert r.status_code == 200 and fired
    monkeypatch.undo()
    res = results_by_user(client, teacher, live_exam["id"])["alice"]
    given = {x["question_id"]: x["given"] for x in res["answers"]}
    assert given[q3] == expected
    assert res["receipt"] == r.json()["receipt"]
    assert r.json()["answered"] == 1


# ---------------------------------------------------------------- refusals
def test_save_after_submit_is_409(client, alice, attempt):
    aid, (q1, *_r) = attempt
    put(client, alice, aid, {q1: 1}, 1)
    client.post(f"/api/student/attempts/{aid}/submit", json={"answers": {q1: 1}, "client_seq": 2},
                headers=alice)
    r = put(client, alice, aid, {q1: 2}, 3)
    assert r.status_code == 409 and r.json() == {"detail": "Attempt already submitted"}
    # even a stale/duplicate save after submit is refused, not reported "saved: false"
    r = put(client, alice, aid, {q1: 1}, 1)
    assert r.status_code == 409


def test_save_after_close_is_409(client, teacher, alice, live_exam, attempt):
    aid, (q1, *_r) = attempt
    put(client, alice, aid, {q1: 1}, 1)
    assert client.post(f"/api/exams/{live_exam['id']}/close", headers=teacher).status_code == 200
    r = put(client, alice, aid, {q1: 2}, 2)
    assert r.status_code == 409 and r.json() == {"detail": "Attempt already submitted"}
    res = results_by_user(client, teacher, live_exam["id"])["alice"]
    assert res["auto_submitted"] is True
    assert res["answers"][0]["given"] == 1


def test_save_after_deadline_is_409(client, alice, attempt, monkeypatch):
    aid, (q1, *_r) = attempt
    assert put(client, alice, aid, {q1: 1}, 1).json()["saved"]
    later = utcnow() + timedelta(minutes=31)
    monkeypatch.setattr(routes_student, "utcnow", lambda: later)
    r = put(client, alice, aid, {q1: 2}, 2)
    assert r.status_code == 409 and r.json() == {"detail": "Time is up"}
    monkeypatch.undo()
    assert stored(client, alice, aid) == {q1: 1}


def test_save_to_someone_elses_or_unknown_attempt_is_404(client, alice, bob, attempt):
    aid, (q1, *_r) = attempt
    assert put(client, bob, aid, {q1: 1}, 1).status_code == 404
    assert put(client, alice, "a_doesnotexist", {q1: 1}, 1).status_code == 404
    assert stored(client, alice, aid) == {}


def test_save_validation(client, alice, attempt):
    aid, (q1, *_r) = attempt
    assert put(client, alice, aid, {q1: [1, 2]}, 1).status_code == 422
    assert put(client, alice, aid, {q1: {"a": 1}}, 1).status_code == 422
    assert put(client, alice, aid, {"x" * 65: 1}, 1).status_code == 422
    assert put(client, alice, aid, {f"q{i}": 1 for i in range(1001)}, 1).status_code == 422
    assert put(client, alice, aid, {q1: "x" * 50_001}, 1).status_code == 422
    r = client.put(f"/api/student/attempts/{aid}/answers", json={"answers": {q1: 1}, "client_seq": "x"},
                   headers=alice)
    assert r.status_code == 422
    assert stored(client, alice, aid) == {}


# ---------------------------------------------------------------- content
def test_large_and_unicode_answers_round_trip(client, teacher, alice, live_exam, attempt):
    aid, (q1, q2, q3) = attempt
    big = ("Ünïcödé — 数学 — математика — o'zbekcha — 🧮 ‍ \"quotes\" \\ back\nline\t" * 800)[:50_000]
    assert len(big) == 50_000
    answers = {q1: 1, q2: -0.000123, q3: big}
    r = put(client, alice, aid, answers, 1)
    assert r.status_code == 200 and r.json()["saved"] is True
    assert stored(client, alice, aid) == answers
    client.post(f"/api/student/attempts/{aid}/submit", json={"answers": answers, "client_seq": 1},
                headers=alice)
    res = results_by_user(client, teacher, live_exam["id"])["alice"]
    given = {x["question_id"]: x["given"] for x in res["answers"]}
    assert given == answers


def test_emoji_and_rtl_short_answer_grading(client, teacher, alice):
    exam = {**SAMPLE_EXAM, "questions": [
        {"type": "short", "prompt": "Say hi", "points": 1, "topics": ["t"],
         "accepted_answers": ["Salom 👋", "مرحبا"]}]}
    e = client.post("/api/exams", json=exam, headers=teacher).json()
    e = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    a = start(client, alice, e)
    aid, q = a["attempt_id"], a["questions"][0]["id"]
    client.post(f"/api/student/attempts/{aid}/submit",
                json={"answers": {q: "  salom   👋 "}, "client_seq": 1}, headers=alice)
    res = results_by_user(client, teacher, e["id"])["alice"]
    assert res["answers"][0]["given"] == "  salom   👋 " and res["answers"][0]["correct"] is True


# ---------------------------------------------------------------- nothing is lost
def test_every_saved_answer_appears_in_results(client, teacher, live_exam):
    """Each of three students saves a different final set over several saves (some
    retried, some out of order); results show exactly each student's last snapshot."""
    users = ["alice", "bob", "carol"]
    finals = {}
    for i, u in enumerate(users):
        h = login(client, u)
        a = start(client, h, live_exam)
        aid, (q1, q2, q3) = a["attempt_id"], [q["id"] for q in a["questions"]]
        snaps = [{q1: (i + s) % 3, q2: i * 10 + s, q3: f"{u}-{s}"} for s in range(1, 6)]
        order = [2, 1, 3, 3, 5, 4, 5]  # duplicates and out-of-order
        for s in order:
            put(client, h, aid, snaps[s - 1], s)
        finals[u] = snaps[4]
        if u != "carol":  # carol never presses submit: the teacher's close submits for her
            client.post(f"/api/student/attempts/{aid}/submit", json={}, headers=h)
    client.post(f"/api/exams/{live_exam['id']}/close", headers=teacher)
    by_user = results_by_user(client, teacher, live_exam["id"])
    for u in users:
        got = {x["question_id"]: x["given"] for x in by_user[u]["answers"]}
        assert list(got.values()) == list(finals[u].values()), u
        assert by_user[u]["status"] == "submitted" and by_user[u]["receipt"]
    assert by_user["carol"]["auto_submitted"] is True


def test_resume_view_reports_stored_client_seq(client, alice, attempt):
    """A fresh computer (no local copy) must learn the stored client_seq, otherwise its
    first saves and its submit carry seq 1, 2, ... and are silently ignored."""
    aid, (q1, *_r) = attempt
    assert client.get(f"/api/student/attempts/{aid}", headers=alice).json()["client_seq"] == -1
    put(client, alice, aid, {q1: 1}, 41)
    assert client.get(f"/api/student/attempts/{aid}", headers=alice).json()["client_seq"] == 41


def test_submit_after_switching_computer_keeps_new_answers(client, teacher, alice, live_exam, attempt):
    """Laptop A saved up to seq 41 and died. On laptop B the client continues from the
    stored seq (resume view) so the final submit is applied."""
    aid, (q1, q2, q3) = attempt
    put(client, alice, aid, {q1: 0, q3: "from laptop A"}, 41)
    view = client.get(f"/api/student/attempts/{aid}", headers=alice).json()
    seq = view["client_seq"] + 1
    r = client.post(f"/api/student/attempts/{aid}/submit",
                    json={"answers": {**view["answers"], q3: "from laptop B"}, "client_seq": seq},
                    headers=alice)
    assert r.status_code == 200
    given = {x["question_id"]: x["given"]
             for x in results_by_user(client, teacher, live_exam["id"])["alice"]["answers"]}
    assert given[q3] == "from laptop B" and given[q1] == 0


def test_submit_reports_how_many_answers_the_server_kept(client, alice, live_exam, attempt):
    """The done page must not claim answers reached the server when they did not: the
    submit response says how many answered questions the server holds."""
    aid, (q1, q2, q3) = attempt
    put(client, alice, aid, {q1: 1, q2: 3.14}, 10)
    # a stale submit (seq 3 < 10): the server keeps its own two answers, not the body's three
    r = client.post(f"/api/student/attempts/{aid}/submit",
                    json={"answers": {q1: 1, q2: 3.14, q3: "paris"}, "client_seq": 3}, headers=alice)
    assert r.status_code == 200 and r.json()["answered"] == 2
    again = client.post(f"/api/student/attempts/{aid}/submit", json={}, headers=alice).json()
    assert again["answered"] == 2 and again["receipt"] == r.json()["receipt"]


def test_results_show_late_submissions(client, teacher, alice, live_exam, attempt, monkeypatch):
    aid, (q1, *_r) = attempt
    later = utcnow() + timedelta(minutes=45)
    monkeypatch.setattr(routes_student, "utcnow", lambda: later)
    client.post(f"/api/student/attempts/{aid}/submit", json={"answers": {q1: 1}, "client_seq": 1},
                headers=alice)
    monkeypatch.undo()
    res = results_by_user(client, teacher, live_exam["id"])["alice"]
    assert res["late"] is True
