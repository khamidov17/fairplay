"""Audit 2026-10-12 tier 3: L-09 roster chunks, L-11 teacher-scoped student lists,
L-13 practice generation limits."""
import pytest

from app import routes_practice
from app.routes_lms import _chunks

from .conftest import login, make_class


def test_chunks_keep_in_lists_small():
    parts = list(_chunks(list(range(2501)), 1000))
    assert [len(p) for p in parts] == [1000, 1000, 501]


@pytest.mark.no_default_class
def test_scan_roster_lists_only_the_teachers_students(client):
    t = login(client, "teacher")
    k = make_class(client, t, "Mine", members=["alice"])
    e = client.post("/api/exams", headers=t, json={
        "title": "Paper", "duration_min": 30, "delivery": "paper", "class_id": k["id"],
        "questions": [{"type": "short", "prompt": "x", "points": 1, "accepted_answers": ["y"], "topics": ["t"]}]}).json()
    r = client.get(f"/api/exams/{e['id']}/roster", headers=t).json()
    assert [s["username"] for s in r["all_students"]] == ["alice"]       # not bob, not carol


def test_practice_generation_is_capped_and_one_at_a_time(client, monkeypatch):
    t = login(client, "teacher")
    r = client.post("/api/practice/generate", headers=t,
                    json={"usernames": [f"s{i}" for i in range(30)], "count": 20})
    assert r.status_code == 422 and "At most" in r.json()["detail"]
    from app.store import get_store
    client.portal.call(get_store().incr, f"practice-gen:{_uid(client, t)}", 600)
    r = client.post("/api/practice/generate", headers=t, json={"usernames": ["alice"], "count": 2})
    assert r.status_code == 409


def _uid(client, headers):
    import base64
    tok = headers["Authorization"].split()[1].split(".")[0]
    return int(base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)).decode().split(":")[0])


def test_live_room_counts_answered_without_loading_answers(client, teacher, alice, live_exam):
    a = client.post(f"/api/student/exams/{live_exam['id']}/start", headers=alice,
                    json={"start_code": live_exam["start_code"]}).json()
    q = [x["id"] for x in live_exam["questions"]]
    client.put(f"/api/student/attempts/{a['attempt_id']}/answers", headers=alice,
               json={"answers": {q[0]: 1, q[1]: "", q[2]: "Paris"}, "client_seq": 1})
    live = client.get(f"/api/exams/{live_exam['id']}/live", headers=teacher).json()
    assert live["students"][0]["answered"] == 2           # the empty answer doesn't count
