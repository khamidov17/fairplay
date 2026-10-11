"""Phase 1a: audit rows for each action and the per-attempt session timeline."""
import json
import time

from sqlalchemy import create_engine, text

from app import audit
from app.store import get_store

from .conftest import SAMPLE_EXAM, login


def rows(tmp_path, **where):
    eng = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    cond = " AND ".join(f"{k} = :{k}" for k in where) or "1=1"
    with eng.connect() as c:
        out = [dict(r._mapping) for r in c.execute(
            text(f"SELECT * FROM audit_events WHERE {cond} ORDER BY at, id"), where)]
    eng.dispose()
    for r in out:
        if isinstance(r["detail"], str):
            r["detail"] = json.loads(r["detail"])
    return out


def actions(tmp_path, **where):
    return [r["action"] for r in rows(tmp_path, **where)]


def open_exam(client, teacher):
    e = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    return client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()


def test_login_rows(client, tmp_path):
    login(client, "alice")
    client.post("/api/auth/login", json={"username": "alice", "password": "nope"})
    client.post("/api/auth/login", json={"username": "ghost", "password": "nope"})
    acts = rows(tmp_path)
    ok = [r for r in acts if r["action"] == "login_ok"]
    failed = [r for r in acts if r["action"] == "login_failed"]
    assert ok and ok[-1]["actor_role"] == "student" and ok[-1]["ip"] == "testclient"
    assert ok[-1]["user_agent"]  # httpx sends one
    assert {f["detail"]["username"] for f in failed} == {"alice", "ghost"}
    assert [f["actor_id"] for f in failed if f["detail"]["username"] == "ghost"] == [None]


def test_session_actions_are_audited(client, teacher, alice, tmp_path):
    e = open_exam(client, teacher)
    eid = e["id"]
    bob = login(client, "bob")
    assert client.post(f"/api/student/exams/{eid}/start", json={"start_code": "0000" if e["start_code"] != "0000" else "1111"},
                       headers=alice).status_code == 403
    a = client.post(f"/api/student/exams/{eid}/start", json={"start_code": e["start_code"]}, headers=alice).json()
    aid = a["attempt_id"]
    client.post(f"/api/student/exams/{eid}/start", json={"start_code": e["start_code"]}, headers=alice)  # resume
    b = client.post(f"/api/student/exams/{eid}/start", json={"start_code": e["start_code"]}, headers=bob).json()
    qid = e["questions"][0]["id"]
    for seq in range(1, 6):  # five saves -> one coalesced row
        assert client.put(f"/api/student/attempts/{aid}/answers",
                          json={"answers": {qid: 1}, "client_seq": seq}, headers=alice).json()["saved"]
    assert actions(tmp_path, attempt_id=aid).count("answer_saved") == 0  # still in the store
    client.get(f"/api/student/exams/{eid}/package", headers=alice)
    client.get(f"/api/student/exams/{eid}/package", headers=alice)  # coalesced: once
    client.get(f"/api/student/exams/{eid}/package-key", headers=alice)
    client.post(f"/api/student/attempts/{aid}/exit-code", json={"code": "nope"}, headers=alice)
    client.post(f"/api/student/attempts/{aid}/exit-code", json={"code": e["exit_code"]}, headers=alice)
    sub = client.post(f"/api/student/attempts/{aid}/submit", json={"answers": {qid: 1}, "client_seq": 6},
                      headers=alice).json()
    client.post(f"/api/exams/{eid}/extend", json={"minutes": 5}, headers=teacher)
    client.post(f"/api/exams/{eid}/close", headers=teacher)  # bob is auto-submitted
    client.put(f"/api/exams/{eid}/attempts/{aid}/grades/{e['questions'][2]['id']}",
               json={"points": 4, "comment": "ok"}, headers=teacher)
    client.put(f"/api/exams/{eid}/attempts/{aid}/comment", json={"comment": "Well done"}, headers=teacher)
    client.post(f"/api/exams/{eid}/release", json={"force": True}, headers=teacher)

    mine = actions(tmp_path, attempt_id=aid)
    assert mine[0] == "attempt_started"
    for act in ("attempt_resumed", "answer_saved", "exit_code_rejected", "exit_code_used",
                "submitted", "grade_changed", "comment_changed"):
        assert act in mine, act
    saved = [r for r in rows(tmp_path, attempt_id=aid) if r["action"] == "answer_saved"]
    assert len(saved) == 1 and saved[0]["detail"]["count"] == 5
    assert saved[0]["detail"]["first_seq"] == 1 and saved[0]["detail"]["last_seq"] == 5
    assert saved[0]["exam_id"] == eid
    subrow = next(r for r in rows(tmp_path, attempt_id=aid) if r["action"] == "submitted")
    assert subrow["detail"]["receipt"] == sub["receipt"] and subrow["detail"]["late"] is False
    grade = next(r for r in rows(tmp_path, attempt_id=aid) if r["action"] == "grade_changed")
    assert grade["detail"]["points"] == 4 and grade["detail"]["question_index"] == 2
    assert grade["actor_role"] == "teacher"

    auto = rows(tmp_path, attempt_id=b["attempt_id"])
    assert [r["action"] for r in auto] == ["attempt_started", "auto_submitted"]
    assert auto[-1]["actor_role"] == "system" and auto[-1]["detail"]["receipt"]

    exam_level = actions(tmp_path, exam_id=eid)
    for act in ("exam_opened", "start_code_wrong", "package_downloaded", "package_key_released",
                "deadline_extended", "exam_closed", "grades_released"):
        assert act in exam_level, act
    assert exam_level.count("package_downloaded") == 1
    closed = next(r for r in rows(tmp_path, exam_id=eid) if r["action"] == "exam_closed")
    assert closed["detail"]["auto_submitted"] == 1


def test_heartbeat_gap_and_timeline(client, teacher, alice, tmp_path):
    e = open_exam(client, teacher)
    a = client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": e["start_code"]},
                    headers=alice).json()
    aid = a["attempt_id"]
    hb = f"/api/student/attempts/{aid}/heartbeat"
    client.post(hb, json={"current_question": 0, "locked": True}, headers=alice)
    client.post(hb, json={"current_question": 0, "locked": True}, headers=alice)  # no gap
    assert "connection_lost" not in actions(tmp_path, attempt_id=aid)
    # pretend the last heartbeat was 2 minutes ago
    client.portal.call(get_store().set, f"aud:hb:{aid}", repr(time.time() - 120), 3600)
    client.post(hb, json={"current_question": 1, "locked": True}, headers=alice)
    acts = actions(tmp_path, attempt_id=aid)
    assert "connection_lost" in acts and "connection_restored" in acts
    restored = next(r for r in rows(tmp_path, attempt_id=aid) if r["action"] == "connection_restored")
    assert 110 <= restored["detail"]["gap_s"] <= 130

    client.post(f"/api/student/attempts/{aid}/events",
                json={"events": [{"type": "other_app_foreground", "detail": "Safari"}]}, headers=alice)
    client.put(f"/api/student/attempts/{aid}/answers",
               json={"answers": {e["questions"][0]["id"]: 1}, "client_seq": 1}, headers=alice)
    tl = client.get(f"/api/exams/{e['id']}/attempts/{aid}/timeline", headers=teacher)
    assert tl.status_code == 200, tl.text
    tl = tl.json()
    assert tl["student"] == {"username": "alice", "name": "Alice Chen"} and tl["status"] == "in_progress"
    evs = tl["events"]
    times = [x["at"] for x in evs]
    assert times == sorted(times)
    acts = [x["action"] for x in evs]
    for act in ("login_ok", "exam_opened", "attempt_started", "connection_lost", "connection_restored",
                "other_app_foreground", "answer_saved"):
        assert act in acts, (act, acts)
    # (the faked gap starts before the attempt, so only relative order is checked here)
    assert acts.index("exam_opened") < acts.index("attempt_started")
    assert acts.index("connection_lost") < acts.index("connection_restored")
    for x in evs:
        assert x["summary"] and x["source"] in ("audit", "integrity", "heartbeat")
    flag = next(x for x in evs if x["action"] == "other_app_foreground")
    assert flag["source"] == "integrity" and "Safari" in flag["summary"] and flag["tone"] == "warn"
    saved = next(x for x in evs if x["action"] == "answer_saved")
    assert saved["summary"].startswith("Answers saved 1 time")
    # bob's sign-in does not appear in alice's timeline
    login(client, "bob")
    tl2 = client.get(f"/api/exams/{e['id']}/attempts/{aid}/timeline", headers=teacher).json()
    assert sum(1 for x in tl2["events"] if x["action"] == "login_ok") == sum(
        1 for x in evs if x["action"] == "login_ok")

    # a stale heartbeat shows up as a trailing "no heartbeat" line
    client.portal.call(get_store().set, f"aud:hb:{aid}", repr(time.time() - 90), 3600)
    tl3 = client.get(f"/api/exams/{e['id']}/attempts/{aid}/timeline", headers=teacher).json()
    assert any(x["action"] == "no_heartbeat" for x in tl3["events"])


def test_summaries_are_plain_english():
    assert audit.summarize("submitted", {"receipt": "K7Q2-M9XD", "late": True}) == \
        "Submitted (receipt K7Q2-M9XD) after the deadline"
    assert audit.summarize("connection_restored", {"gap_s": 125}) == "Connection restored after 2 min 5 s"
    assert audit.summarize("answer_saved", {"count": 3, "first_seq": 4, "last_seq": 6}) == \
        "Answers saved 3 times (save #4–#6)"
    assert audit.summarize("grade_changed", {"question_index": 0, "points": 2, "old_points": 1}) == \
        "Teacher graded question 1: 2 points (was 1)"
    for act in audit.ACTIONS:
        assert audit.summarize(act, {}) and "_" not in audit.summarize(act, {})


def test_audit_rows_survive_exam_delete(client, teacher, alice, tmp_path):
    e = open_exam(client, teacher)
    client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": e["start_code"]}, headers=alice)
    client.post(f"/api/exams/{e['id']}/close", headers=teacher)
    assert client.delete(f"/api/exams/{e['id']}", headers=teacher).status_code == 204
    assert {"exam_opened", "attempt_started", "auto_submitted", "exam_closed"} <= set(actions(tmp_path, exam_id=e["id"]))
