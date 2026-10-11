from app.grading import grade, grade_question, normalize_short

from .conftest import SAMPLE_EXAM


def test_unit_grading():
    mcq = {"id": "q_1", "type": "mcq", "correct_index": 1, "points": 2}
    assert grade_question(mcq, 1) and grade_question(mcq, 1.0)
    assert not grade_question(mcq, 0) and not grade_question(mcq, "1") and not grade_question(mcq, True)
    num = {"id": "q_2", "type": "numeric", "answer_number": 3.14, "tolerance": 0.01}
    assert grade_question(num, 3.14) and grade_question(num, 3.15) and grade_question(num, 3.13)
    assert not grade_question(num, 3.16) and not grade_question(num, None)
    assert grade_question(num, "3.141")
    exact = {"id": "q_3", "type": "numeric", "answer_number": 42, "tolerance": None}
    assert grade_question(exact, 42) and not grade_question(exact, 42.001)
    short = {"id": "q_4", "type": "short", "accepted_answers": ["New  York", "NYC"]}
    assert grade_question(short, "  new york ") and grade_question(short, "nyc")
    assert grade_question(short, "NEW\tYORK") and not grade_question(short, "newyork")
    assert normalize_short("  A   b  C ") == "a b c"
    score, rows = grade([dict(mcq, points=2), dict(short, points=3)], {"q_1": 1, "q_4": "x"})
    assert score == 2 and [r["correct"] for r in rows] == [True, False]
    assert rows[1]["points_awarded"] == 0 and rows[0]["points_awarded"] == 2


def _submit(client, h, exam, answers_by_index):
    a = client.post(f"/api/student/exams/{exam['id']}/start",
                    json={"start_code": exam["start_code"]}, headers=h).json()
    qs = a["questions"]
    answers = {qs[i]["id"]: v for i, v in answers_by_index.items()}
    r = client.post(f"/api/student/attempts/{a['attempt_id']}/submit",
                    json={"answers": answers, "client_seq": 1}, headers=h)
    assert r.status_code == 200
    assert "score" not in r.json()  # scores are not returned to students
    return a


def test_results_end_to_end(client, teacher, alice, bob, live_exam):
    _submit(client, alice, live_exam, {0: 1, 1: 3.145, 2: "  PARIS   city"})  # all right: 10/10
    _submit(client, bob, live_exam, {0: 0, 1: 3.2})                            # all wrong: 0/10
    res = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    assert res["exam_id"] == live_exam["id"] and res["title"] == "Midterm" and res["total_points"] == 10
    assert res["average_percent"] == 50.0
    by = {x["username"]: x for x in res["attempts"]}
    al, bo = by["alice"], by["bob"]
    assert al["score"] == 10 and al["percent"] == 100.0 and al["total_points"] == 10
    assert bo["score"] == 0 and bo["percent"] == 0.0
    assert [r["correct"] for r in al["answers"]] == [True, True, True]
    assert [r["points_awarded"] for r in al["answers"]] == [2, 3, 5]
    assert bo["answers"][2] == {"question_id": live_exam["questions"][2]["id"], "given": None,
                                "correct": False, "points_awarded": 0, "max_points": 5,
                                "manual": False, "pending": False, "comment": "",
                                "rubric_scores": None}
    assert set(al) >= {"attempt_id", "username", "name", "status", "auto_submitted", "score",
                       "total_points", "percent", "submitted_at", "receipt", "answers", "events"}
    assert [q["percent_correct"] for q in res["questions"]] == [50.0, 50.0, 50.0]
    assert res["questions"][0]["prompt"] == SAMPLE_EXAM["questions"][0]["prompt"]


def test_results_include_events(client, teacher, alice, live_exam):
    a = _submit(client, alice, live_exam, {0: 1})
    client.post(f"/api/student/attempts/{a['attempt_id']}/events",
                json={"events": [{"type": "blocked_shortcut", "detail": "Cmd-key48",
                                  "client_time": "2026-01-01T00:00:00Z"}]}, headers=alice)
    res = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    ev = res["attempts"][0]["events"][0]
    assert ev["type"] == "blocked_shortcut" and ev["detail"] == "Cmd-key48"
    assert ev["client_time"] == "2026-01-01T00:00:00Z" and ev["server_time"].endswith("Z")
