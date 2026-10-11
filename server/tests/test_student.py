from .conftest import SAMPLE_EXAM, login


def start(client, h, exam, code=None):
    return client.post(f"/api/student/exams/{exam['id']}/start",
                       json={"start_code": code if code is not None else exam["start_code"]},
                       headers=h)


def wrong(code):
    return "1000" if code != "1000" else "1001"


def test_student_exam_list(client, teacher, alice, live_exam):
    draft = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    lst = client.get("/api/student/exams", headers=alice).json()
    ids = [x["id"] for x in lst]
    assert live_exam["id"] in ids and draft["id"] not in ids
    item = next(x for x in lst if x["id"] == live_exam["id"])
    assert item == {"id": live_exam["id"], "title": "Midterm", "duration_min": 30,
                    "question_count": 3, "status": "live", "attempt": None,
                    "course": "", "room": "", "scheduled_at": None,
                    "mode": "questions", "delivery": "computer", "class_names": []}
    a = start(client, alice, live_exam).json()
    item = next(x for x in client.get("/api/student/exams", headers=alice).json()
                if x["id"] == live_exam["id"])
    assert item["attempt"] == {"attempt_id": a["attempt_id"], "status": "in_progress",
                               "submitted_at": None, "receipt": None, "released": False,
                               "percent": None}


def test_start_not_open(client, teacher, alice):
    e = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    r = client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": "1234"}, headers=alice)
    assert r.status_code == 409 and r.json() == {"detail": "Exam is not open"}


def test_start_wrong_then_right_and_resume(client, alice, live_exam):
    r = start(client, alice, live_exam, wrong(live_exam["start_code"]))
    assert r.status_code == 403 and r.json() == {"detail": "Wrong start code"}
    r = start(client, alice, live_exam)
    assert r.status_code == 200
    a = r.json()
    assert set(a) == {"attempt_id", "exam_id", "title", "duration_min", "started_at", "deadline_at",
                      "server_time", "questions", "answers", "status", "course",
                      "mode", "external_url", "allowed_domains", "finish_url_contains",
                      "client_seq",
                      "exit_check"}  # phase 1a: verifier only, never the exit code
    assert set(a["exit_check"]) == {"alg", "iterations", "salt", "hash"}
    assert a["status"] == "in_progress" and a["answers"] == {} and a["title"] == "Midterm"
    for q in a["questions"]:
        for secret in ("correct_index", "answer_number", "tolerance", "accepted_answers"):
            assert secret not in q
    assert a["questions"][0]["options"] == ["3", "4", "5"]
    # resume via start again and via GET
    again = start(client, alice, live_exam).json()
    assert again["attempt_id"] == a["attempt_id"] and again["started_at"] == a["started_at"]
    g = client.get(f"/api/student/attempts/{a['attempt_id']}", headers=alice)
    assert g.status_code == 200 and g.json()["attempt_id"] == a["attempt_id"]


def test_rate_limit_after_five_wrong(client, alice, bob, live_exam):
    bad = wrong(live_exam["start_code"])
    for _ in range(5):
        assert start(client, alice, live_exam, bad).status_code == 403
    r = start(client, alice, live_exam)  # even the right code is blocked now
    assert r.status_code == 429
    assert r.json() == {"detail": "Too many wrong codes, wait 60 seconds"}
    assert start(client, bob, live_exam).status_code == 200  # per student


def test_wrong_counter_resets_on_success(client, alice, live_exam):
    bad = wrong(live_exam["start_code"])
    for _ in range(4):
        assert start(client, alice, live_exam, bad).status_code == 403
    assert start(client, alice, live_exam).status_code == 200
    for _ in range(4):
        assert start(client, alice, live_exam, bad).status_code == 403
    assert start(client, alice, live_exam).status_code == 200


def test_attempt_belongs_to_student(client, alice, bob, live_exam):
    a = start(client, alice, live_exam).json()
    aid = a["attempt_id"]
    assert client.get(f"/api/student/attempts/{aid}", headers=bob).status_code == 404
    assert client.put(f"/api/student/attempts/{aid}/answers", json={"answers": {}, "client_seq": 9},
                      headers=bob).status_code == 404
    assert client.post(f"/api/student/attempts/{aid}/heartbeat", json={}, headers=bob).status_code == 404
    assert client.post(f"/api/student/attempts/{aid}/submit", json={}, headers=bob).status_code == 404


def test_client_seq_ordering(client, alice, live_exam):
    a = start(client, alice, live_exam).json()
    aid, qs = a["attempt_id"], a["questions"]
    url = f"/api/student/attempts/{aid}/answers"
    r = client.put(url, json={"answers": {qs[0]["id"]: 1}, "client_seq": 2}, headers=alice)
    assert r.status_code == 200 and r.json()["saved"] is True and r.json()["client_seq"] == 2
    assert r.json()["server_time"].endswith("Z")
    # stale and duplicate saves are ignored
    r = client.put(url, json={"answers": {qs[0]["id"]: 0}, "client_seq": 1}, headers=alice)
    assert r.status_code == 200 and r.json()["saved"] is False
    r = client.put(url, json={"answers": {qs[0]["id"]: 0}, "client_seq": 2}, headers=alice)
    assert r.json()["saved"] is False
    assert client.get(f"/api/student/attempts/{aid}", headers=alice).json()["answers"] == {qs[0]["id"]: 1}
    r = client.put(url, json={"answers": {qs[0]["id"]: 2, qs[2]["id"]: "x"}, "client_seq": 3},
                   headers=alice)
    assert r.json()["saved"] is True
    assert client.get(f"/api/student/attempts/{aid}", headers=alice).json()["answers"] == {
        qs[0]["id"]: 2, qs[2]["id"]: "x"}


def test_heartbeat_and_events(client, teacher, alice, live_exam):
    a = start(client, alice, live_exam).json()
    aid = a["attempt_id"]
    r = client.post(f"/api/student/attempts/{aid}/heartbeat",
                    json={"current_question": 2, "locked": True}, headers=alice)
    assert r.status_code == 200
    hb = r.json()
    assert hb["ok"] is True and hb["status"] == "in_progress" and hb["deadline_at"] == a["deadline_at"]
    evs = [{"type": "blocked_shortcut", "detail": "Cmd-key48", "client_time": "2026-01-01T00:00:00Z"},
           {"type": "focus_lost", "detail": "", "client_time": "2026-01-01T00:00:01Z"}]
    r = client.post(f"/api/student/attempts/{aid}/events", json={"events": evs}, headers=alice)
    assert r.json() == {"ok": True, "stored": 2}
    r = client.post(f"/api/student/attempts/{aid}/events", json={"events": evs[:1]}, headers=alice)
    assert r.json() == {"ok": True, "stored": 1}


def test_submit_idempotent_and_after_submit(client, alice, live_exam):
    a = start(client, alice, live_exam).json()
    aid, qs = a["attempt_id"], a["questions"]
    body = {"answers": {qs[0]["id"]: 1}, "client_seq": 5}
    r1 = client.post(f"/api/student/attempts/{aid}/submit", json=body, headers=alice)
    assert r1.status_code == 200
    s1 = r1.json()
    assert s1["status"] == "submitted" and s1["submitted_at"].endswith("Z")
    rc = s1["receipt"]
    assert len(rc) == 9 and rc[4] == "-" and rc.replace("-", "").isalnum() and rc.upper() == rc
    r2 = client.post(f"/api/student/attempts/{aid}/submit", json={"answers": {}, "client_seq": 9},
                     headers=alice)
    assert r2.json() == s1
    # after submit: saves 409, heartbeat says submitted, events still allowed, start says 409
    r = client.put(f"/api/student/attempts/{aid}/answers", json={"answers": {}, "client_seq": 99},
                   headers=alice)
    assert r.status_code == 409
    hb = client.post(f"/api/student/attempts/{aid}/heartbeat", json={}, headers=alice).json()
    assert hb["status"] == "submitted"
    r = client.post(f"/api/student/attempts/{aid}/events",
                    json={"events": [{"type": "x", "detail": "y", "client_time": "t"}]}, headers=alice)
    assert r.status_code == 200
    r = start(client, alice, live_exam)
    assert r.status_code == 409 and r.json() == {"detail": "Already submitted"}


def test_submit_uses_newer_stored_answers(client, teacher, alice, live_exam):
    a = start(client, alice, live_exam).json()
    aid, qs = a["attempt_id"], a["questions"]
    client.put(f"/api/student/attempts/{aid}/answers",
               json={"answers": {qs[0]["id"]: 1}, "client_seq": 10}, headers=alice)
    client.post(f"/api/student/attempts/{aid}/submit",
                json={"answers": {qs[0]["id"]: 0}, "client_seq": 3}, headers=alice)
    res = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    assert res["attempts"][0]["answers"][0]["given"] == 1


def test_close_auto_submits(client, teacher, alice, bob, live_exam):
    a = start(client, alice, live_exam).json()
    b = start(client, bob, live_exam).json()
    client.post(f"/api/student/attempts/{b['attempt_id']}/submit", json={}, headers=bob)
    bob_receipt = client.get("/api/student/exams", headers=bob).json()[0]["attempt"]["receipt"]
    client.put(f"/api/student/attempts/{a['attempt_id']}/answers",
               json={"answers": {a["questions"][0]["id"]: 1}, "client_seq": 1}, headers=alice)
    # heartbeat first so the attempt meta is cached, then close
    client.post(f"/api/student/attempts/{a['attempt_id']}/heartbeat", json={}, headers=alice)
    assert client.post(f"/api/exams/{live_exam['id']}/close", headers=teacher).status_code == 200
    hb = client.post(f"/api/student/attempts/{a['attempt_id']}/heartbeat", json={}, headers=alice)
    assert hb.json()["status"] == "submitted"
    res = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    by_user = {x["username"]: x for x in res["attempts"]}
    assert by_user["alice"]["status"] == "submitted" and by_user["alice"]["auto_submitted"] is True
    assert by_user["alice"]["receipt"] and by_user["alice"]["score"] == 2
    assert by_user["bob"]["auto_submitted"] is False and by_user["bob"]["receipt"] == bob_receipt
    lst = client.get("/api/student/exams", headers=alice).json()
    assert lst[0]["status"] == "closed" and lst[0]["attempt"]["status"] == "submitted"
    r = start(client, alice, live_exam)
    assert r.status_code == 409


def test_live_endpoint(client, teacher, alice, bob, live_exam):
    assert login(client, "carol")  # carol has no attempt -> not listed
    a = start(client, alice, live_exam).json()
    start(client, bob, live_exam)
    qs = a["questions"]
    client.put(f"/api/student/attempts/{a['attempt_id']}/answers",
               json={"answers": {qs[0]["id"]: 1, qs[2]["id"]: "paris", qs[1]["id"]: None},
                     "client_seq": 1}, headers=alice)
    client.post(f"/api/student/attempts/{a['attempt_id']}/heartbeat",
                json={"current_question": 2, "locked": True}, headers=alice)
    client.post(f"/api/student/attempts/{a['attempt_id']}/events",
                json={"events": [{"type": "focus_lost", "detail": "", "client_time": "t"},
                                 {"type": "blocked_shortcut", "detail": "Cmd-key48",
                                  "client_time": "t"}]}, headers=alice)
    live = client.get(f"/api/exams/{live_exam['id']}/live", headers=teacher).json()
    assert live["exam_id"] == live_exam["id"] and live["status"] == "live"
    assert live["start_code"] == live_exam["start_code"] and live["duration_min"] == 30
    assert [s["username"] for s in live["students"]] == ["alice", "bob"]
    s = live["students"][0]
    assert set(s) == {"attempt_id", "username", "name", "status", "started_at", "submitted_at",
                      "deadline_at", "answered", "question_count", "current_question", "locked",
                      "last_heartbeat_at", "online", "event_count", "last_event", "needs_look"}
    assert s["name"] == "Alice Chen" and s["status"] == "in_progress"
    assert s["answered"] == 2 and s["question_count"] == 3
    assert s["current_question"] == 2 and s["locked"] is True and s["online"] is True
    assert s["last_heartbeat_at"].endswith("Z")
    assert s["event_count"] == 2 and s["last_event"] == "blocked_shortcut Cmd-key48"
    b = live["students"][1]
    assert b["online"] is False and b["last_heartbeat_at"] is None and b["event_count"] == 0
    assert b["last_event"] is None and b["answered"] == 0


def test_bare_submit_keeps_saved_answers(client, teacher, alice, live_exam):
    a = start(client, alice, live_exam).json()
    aid, qs = a["attempt_id"], a["questions"]
    client.put(f"/api/student/attempts/{aid}/answers",
               json={"answers": {qs[0]["id"]: 1}, "client_seq": 0}, headers=alice)
    client.post(f"/api/student/attempts/{aid}/submit", json={}, headers=alice)
    res = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    assert res["attempts"][0]["answers"][0]["given"] == 1 and res["attempts"][0]["score"] == 2
