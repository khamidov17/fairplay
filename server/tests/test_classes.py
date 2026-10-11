"""Classes: CRUD, join codes, adding students (list / CSV / LMS), exam assignment, the
eligibility rule (start, list, offline package), paper roster union, teacher scoping."""
import pytest

from app.classes.service import JOIN_ALPHABET, NOT_IN_CLASS, parse_csv, split_entries

from .conftest import SAMPLE_EXAM, login


def make_user(client, username, role, name=None):
    from app.db import get_database
    from app.models import User
    from app.security import hash_password

    async def go():
        async with get_database().sessionmaker() as s:
            u = User(username=username, name=name or username.title(), role=role,
                     password_hash=hash_password(f"{username}123", 1000))
            s.add(u)
            await s.commit()
            return u.id
    uid = client.portal.call(go)
    return login(client, username), uid


def user_id(client, username):
    from sqlalchemy import select

    from app.db import get_database
    from app.models import User

    async def go():
        async with get_database().sessionmaker() as s:
            return (await s.execute(select(User.id).where(User.username == username))).scalar_one()
    return client.portal.call(go)


@pytest.fixture
def carol(client):
    return login(client, "carol")


def new_class(client, teacher, name="Calculus I", **kw):
    r = client.post("/api/classes", json={"name": name, **kw}, headers=teacher)
    assert r.status_code == 201, r.text
    return r.json()


def add(client, teacher, cid, text):
    r = client.post(f"/api/classes/{cid}/members", json={"text": text}, headers=teacher)
    assert r.status_code == 200, r.text
    return r.json()


def live_exam(client, teacher, **kw):
    e = client.post("/api/exams", json={**SAMPLE_EXAM, **kw}, headers=teacher).json()
    return client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()


def assign(client, teacher, exam_id, class_ids):
    r = client.put(f"/api/exams/{exam_id}/classes", json={"class_ids": class_ids}, headers=teacher)
    assert r.status_code == 200, r.text
    return r.json()


def start(client, who, exam):
    return client.post(f"/api/student/exams/{exam['id']}/start",
                       json={"start_code": exam["start_code"]}, headers=who)


# ---------------------------------------------------------------- helpers
def test_split_entries_and_csv_parsing():
    assert split_entries("alice\n BOB ,carol;alice\n\n<dan@x.edu>") == ["alice", "BOB", "carol", "dan@x.edu"]
    rows = parse_csv("Name,Email,Username\nAlice Chen,alice@x.edu,alice\nZed,zed@x.edu,\n,,\n")
    assert rows == [(["alice@x.edu", "alice"], "Alice Chen (alice@x.edu)"), (["zed@x.edu"], "Zed (zed@x.edu)")]
    assert parse_csv("alice\nbob\n") == [(["alice"], "alice"), (["bob"], "bob")]
    assert parse_csv("") == []


# ---------------------------------------------------------------- classes CRUD
def test_create_rename_archive(client, teacher):
    c = new_class(client, teacher, section="MATH 101", term="Fall 2026")
    assert c["name"] == "Calculus I" and c["section"] == "MATH 101" and c["term"] == "Fall 2026"
    assert len(c["join_code"]) == 7 and set(c["join_code"]) <= set(JOIN_ALPHABET)
    assert c["join_enabled"] is True and c["archived"] is False and c["member_count"] == 0
    assert client.post("/api/classes", json={"name": "   "}, headers=teacher).status_code == 422

    r = client.patch(f"/api/classes/{c['id']}", json={"name": "Calculus 1A"}, headers=teacher)
    assert r.status_code == 200 and r.json()["name"] == "Calculus 1A" and r.json()["section"] == "MATH 101"
    assert [x["name"] for x in client.get("/api/classes", headers=teacher).json()] == ["Calculus 1A"]

    r = client.patch(f"/api/classes/{c['id']}", json={"archived": True}, headers=teacher)
    assert r.json()["archived"] is True
    assert client.get("/api/classes", headers=teacher).json() == []
    assert [x["id"] for x in client.get("/api/classes?archived=true", headers=teacher).json()] == [c["id"]]


def test_classes_need_teacher(client, alice):
    assert client.get("/api/classes", headers=alice).status_code == 403
    assert client.post("/api/classes", json={"name": "x"}, headers=alice).status_code == 403


# ---------------------------------------------------------------- join by code
def test_join_by_code(client, teacher, alice, bob):
    c = new_class(client, teacher)
    code = c["join_code"]
    messy = f" {code[:3].lower()}-{code[3:].lower()} "
    r = client.post("/api/student/classes/join", json={"code": messy}, headers=alice)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == c["id"] and r.json()["already_member"] is False
    assert r.json()["teacher_name"] == "Demo Teacher"
    r = client.post("/api/student/classes/join", json={"code": code}, headers=alice)
    assert r.status_code == 200 and r.json()["already_member"] is True

    r = client.post("/api/student/classes/join", json={"code": "ZZZZZZZ"}, headers=bob)
    assert r.status_code == 404 and "No class has this code" in r.json()["detail"]

    # joining turned off: refused, but existing members are fine
    client.patch(f"/api/classes/{c['id']}", json={"join_enabled": False}, headers=teacher)
    r = client.post("/api/student/classes/join", json={"code": code}, headers=bob)
    assert r.status_code == 403 and r.json()["detail"] == "Joining this class is turned off. Ask your teacher."
    client.patch(f"/api/classes/{c['id']}", json={"join_enabled": True}, headers=teacher)

    # regenerate: old code dead, new code works
    new = client.post(f"/api/classes/{c['id']}/join-code", headers=teacher).json()
    assert new["join_code"] != code
    assert client.post("/api/student/classes/join", json={"code": code}, headers=bob).status_code == 404
    assert client.post("/api/student/classes/join", json={"code": new["join_code"]}, headers=bob).status_code == 200

    detail = client.get(f"/api/classes/{c['id']}", headers=teacher).json()
    assert {(s["username"], s["added_by"]) for s in detail["students"]} == {("alice", "code"), ("bob", "code")}

    # archived classes cannot be joined; teachers cannot join
    client.patch(f"/api/classes/{c['id']}", json={"archived": True}, headers=teacher)
    carol = login(client, "carol")
    assert client.post("/api/student/classes/join", json={"code": new["join_code"]}, headers=carol).status_code == 404
    assert client.post("/api/student/classes/join", json={"code": new["join_code"]}, headers=teacher).status_code == 403


def test_join_wrong_code_rate_limited(client, teacher, alice):
    c = new_class(client, teacher)
    for _ in range(10):
        assert client.post("/api/student/classes/join", json={"code": "WRONG22"}, headers=alice).status_code == 404
    r = client.post("/api/student/classes/join", json={"code": c["join_code"]}, headers=alice)
    assert r.status_code == 429


# ---------------------------------------------------------------- adding students
def test_add_by_username_list_and_remove(client, teacher):
    c = new_class(client, teacher)
    r = add(client, teacher, c["id"], "alice\nBOB\nnobody\nteacher\n")
    assert [s["username"] for s in r["added"]] == ["alice", "bob"]
    assert r["not_found"] == ["nobody", "teacher"]  # teachers are never class members
    assert r["class"]["member_count"] == 2
    r = add(client, teacher, c["id"], "alice, carol")
    assert [s["username"] for s in r["added"]] == ["carol"] and [s["username"] for s in r["already"]] == ["alice"]
    r = client.post(f"/api/classes/{c['id']}/members", json={"entries": ["  "]}, headers=teacher)
    assert r.status_code == 422

    bob_id = user_id(client, "bob")
    assert client.delete(f"/api/classes/{c['id']}/members/{bob_id}", headers=teacher).status_code == 204
    assert client.delete(f"/api/classes/{c['id']}/members/{bob_id}", headers=teacher).status_code == 404
    detail = client.get(f"/api/classes/{c['id']}", headers=teacher).json()
    assert [s["username"] for s in detail["students"]] == ["alice", "carol"]  # by name
    assert detail["member_count"] == 2


def test_add_from_csv(client, teacher):
    c = new_class(client, teacher)
    csv_text = "Name,Email,Username\nAlice Chen,,alice\nBob K,,Bob\nZed Moon,zed@x.edu,\n"
    r = client.post(f"/api/classes/{c['id']}/members/csv", json={"csv": csv_text}, headers=teacher)
    assert r.status_code == 200, r.text
    assert [s["username"] for s in r.json()["added"]] == ["alice", "bob"]
    assert r.json()["not_found"] == ["Zed Moon (zed@x.edu)"]
    r = client.post(f"/api/classes/{c['id']}/members/csv", json={"csv": "carol\nalice\n"}, headers=teacher)
    assert [s["username"] for s in r.json()["added"]] == ["carol"]
    assert client.post(f"/api/classes/{c['id']}/members/csv", json={"csv": "\n\n"},
                       headers=teacher).status_code == 422


def test_import_from_lms_course(client, teacher):
    from app.db import get_database
    from app.models import CourseLink, CourseMember, LmsConnection

    bob_id, carol_id = user_id(client, "bob"), user_id(client, "carol")
    t_id = user_id(client, "teacher")

    async def go():
        async with get_database().sessionmaker() as s:
            conn = LmsConnection(teacher_id=t_id, provider="canvas", base_url="https://canvas.test",
                                 credential="sealed")
            s.add(conn)
            await s.flush()
            link = CourseLink(connection_id=conn.id, lms_course_id="101", name="Course 1")
            s.add(link)
            await s.flush()
            s.add_all([CourseMember(course_link_id=link.id, user_id=bob_id, lms_user_id="8"),
                       CourseMember(course_link_id=link.id, user_id=carol_id, lms_user_id="9")])
            await s.commit()
            return link.id
    link_id = client.portal.call(go)
    c = new_class(client, teacher)
    r = client.post(f"/api/classes/{c['id']}/members/lms", json={"course_link_id": link_id}, headers=teacher)
    assert r.status_code == 200, r.text
    assert sorted(s["username"] for s in r.json()["added"]) == ["bob", "carol"]
    detail = client.get(f"/api/classes/{c['id']}", headers=teacher).json()
    assert {s["added_by"] for s in detail["students"]} == {"lms"}
    assert client.post(f"/api/classes/{c['id']}/members/lms", json={"course_link_id": 999},
                       headers=teacher).status_code == 404

    t2, _ = make_user(client, "teacher2", "teacher")
    c2 = new_class(client, t2)
    assert client.post(f"/api/classes/{c2['id']}/members/lms", json={"course_link_id": link_id},
                       headers=t2).status_code == 404


# ---------------------------------------------------------------- assignment + eligibility
def test_assignment_and_eligibility(client, teacher, alice, bob, carol):
    c = new_class(client, teacher, name="Group A")
    add(client, teacher, c["id"], "alice\nbob")
    e = live_exam(client, teacher)

    info = client.get(f"/api/exams/{e['id']}/classes", headers=teacher).json()
    assert info["classes"] == [] and info["audience"] == "open" and info["student_count"] == 0

    # unassigned: open to anyone with the code (behaviour before classes)
    assert any(x["id"] == e["id"] for x in client.get("/api/student/exams", headers=carol).json())

    info = assign(client, teacher, e["id"], [c["id"]])
    assert info["audience"] == "classes" and info["student_count"] == 2
    assert info["classes"][0]["name"] == "Group A" and info["classes"][0]["member_count"] == 2

    # carol is not in the class: hidden from her list, start + package refused
    assert all(x["id"] != e["id"] for x in client.get("/api/student/exams", headers=carol).json())
    r = start(client, carol, e)
    assert r.status_code == 403 and r.json()["detail"] == NOT_IN_CLASS
    r = client.get(f"/api/student/exams/{e['id']}/package", headers=carol)
    assert r.status_code == 403 and r.json()["detail"] == NOT_IN_CLASS

    # alice is: sees it with the class name, may start and download the package
    item = next(x for x in client.get("/api/student/exams", headers=alice).json() if x["id"] == e["id"])
    assert item["class_names"] == ["Group A"]
    assert start(client, alice, e).status_code == 200
    assert client.get(f"/api/student/exams/{e['id']}/package", headers=alice).status_code == 200

    # the class list shows the exam to members
    mine = client.get("/api/student/classes", headers=alice).json()
    assert mine[0]["name"] == "Group A" and mine[0]["teacher_name"] == "Demo Teacher"
    assert mine[0]["exams"][0]["id"] == e["id"] and mine[0]["exams"][0]["attempt"]["status"] == "in_progress"
    assert client.get("/api/student/classes", headers=carol).json() == []

    # joining the class lets carol in
    client.post("/api/student/classes/join", json={"code": c["join_code"]}, headers=carol)
    assert start(client, carol, e).status_code == 200

    # removing a class from the exam opens it again
    assert assign(client, teacher, e["id"], [])["audience"] == "open"
    detail = client.get(f"/api/classes/{c['id']}", headers=teacher).json()
    assert detail["exams"] == [] and detail["exam_count"] == 0


def test_eligibility_union_of_classes_and_lms_precedence(client, teacher, alice, bob, carol):
    c1, c2 = new_class(client, teacher, name="A"), new_class(client, teacher, name="B")
    add(client, teacher, c1["id"], "alice")
    add(client, teacher, c2["id"], "bob")
    e = live_exam(client, teacher)
    assert assign(client, teacher, e["id"], [c1["id"], c2["id"]])["student_count"] == 2
    assert start(client, alice, e).status_code == 200
    assert start(client, bob, e).status_code == 200
    assert start(client, carol, e).status_code == 403
    # archived classes keep their assignment (exam history stays valid)
    client.patch(f"/api/classes/{c2['id']}", json={"archived": True}, headers=teacher)
    info = client.get(f"/api/exams/{e['id']}/classes", headers=teacher).json()
    assert [x["archived"] for x in info["classes"]] == [False, True]


def test_unassigned_exam_list_unchanged(client, teacher, alice):
    e = live_exam(client, teacher)
    item = next(x for x in client.get("/api/student/exams", headers=alice).json() if x["id"] == e["id"])
    assert item["class_names"] == []
    assert start(client, alice, e).status_code == 200


def test_explicit_roster_wins_over_classes(client, teacher, alice, bob, carol):
    c = new_class(client, teacher)
    add(client, teacher, c["id"], "alice")
    e = live_exam(client, teacher)
    assign(client, teacher, e["id"], [c["id"]])
    r = client.put(f"/api/exams/{e['id']}/roster", json={"usernames": ["bob"]}, headers=teacher)
    assert r.status_code == 200 and r.json()["source"] == "picked"
    assert client.get(f"/api/exams/{e['id']}/classes", headers=teacher).json()["audience"] == "roster"
    assert start(client, alice, e).status_code == 403
    assert start(client, bob, e).status_code == 200
    names = [x["id"] for x in client.get("/api/student/exams", headers=carol).json()]
    assert e["id"] not in names


def test_paper_roster_is_union_of_classes(client, teacher):
    c1, c2 = new_class(client, teacher, name="A"), new_class(client, teacher, name="B")
    add(client, teacher, c1["id"], "alice")
    add(client, teacher, c2["id"], "alice\nbob")
    paper = client.post("/api/exams", json={**SAMPLE_EXAM, "delivery": "paper"}, headers=teacher).json()
    r = client.get(f"/api/exams/{paper['id']}/roster", headers=teacher).json()
    assert r["source"] == "all" and len(r["students"]) == 3
    assign(client, teacher, paper["id"], [c1["id"], c2["id"]])
    r = client.get(f"/api/exams/{paper['id']}/roster", headers=teacher).json()
    assert r["source"] == "classes" and [s["username"] for s in r["students"]] == ["alice", "bob"]
    # explicit pick overrides; [] goes back to the classes
    r = client.put(f"/api/exams/{paper['id']}/roster", json={"usernames": ["carol"]}, headers=teacher).json()
    assert r["source"] == "picked" and [s["username"] for s in r["students"]] == ["carol"]
    r = client.put(f"/api/exams/{paper['id']}/roster", json={"usernames": []}, headers=teacher).json()
    assert r["source"] == "classes" and len(r["students"]) == 2


def test_delete_exam_with_classes(client, teacher):
    c = new_class(client, teacher)
    e = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    assign(client, teacher, e["id"], [c["id"]])
    assert client.delete(f"/api/exams/{e['id']}", headers=teacher).status_code == 204
    assert client.get(f"/api/classes/{c['id']}", headers=teacher).json()["exam_count"] == 0


def test_student_class_list_hides_drafts_and_paper(client, teacher, alice):
    c = new_class(client, teacher)
    add(client, teacher, c["id"], "alice")
    draft = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    planned = client.post("/api/exams", json={**SAMPLE_EXAM, "title": "Planned",
                                              "scheduled_at": "2030-01-01T09:00:00Z"}, headers=teacher).json()
    paper = client.post("/api/exams", json={**SAMPLE_EXAM, "delivery": "paper"}, headers=teacher).json()
    for x in (draft, planned, paper):
        assign(client, teacher, x["id"], [c["id"]])
    exams = client.get("/api/student/classes", headers=alice).json()[0]["exams"]
    assert [x["id"] for x in exams] == [planned["id"]] and exams[0]["attempt"] is None


def test_gradebook_class_filter(client, teacher, alice, bob):
    c = new_class(client, teacher)
    add(client, teacher, c["id"], "alice")
    e1, e2 = live_exam(client, teacher), live_exam(client, teacher, title="Other")
    assign(client, teacher, e1["id"], [c["id"]])
    gb = client.get(f"/api/gradebook?class_id={c['id']}", headers=teacher).json()
    assert [s["username"] for s in gb["students"]] == ["alice"]
    assert [x["exam_id"] for x in gb["exams"]] == [e1["id"]]
    assert len(client.get("/api/gradebook", headers=teacher).json()["students"]) == 3
    r = client.get(f"/api/gradebook.csv?class_id={c['id']}", headers=teacher)
    assert r.status_code == 200 and "Alice Chen" in r.text and "Bob" not in r.text


# ---------------------------------------------------------------- teacher scoping
def test_other_teacher_cannot_touch_classes(client, teacher, alice):
    c = new_class(client, teacher)
    add(client, teacher, c["id"], "alice")
    t2, _ = make_user(client, "teacher2", "teacher")
    cid = c["id"]
    alice_id = user_id(client, "alice")
    assert client.get("/api/classes", headers=t2).json() == []
    assert client.get(f"/api/classes/{cid}", headers=t2).status_code == 404
    assert client.patch(f"/api/classes/{cid}", json={"name": "x"}, headers=t2).status_code == 404
    assert client.post(f"/api/classes/{cid}/join-code", headers=t2).status_code == 404
    assert client.post(f"/api/classes/{cid}/members", json={"text": "bob"}, headers=t2).status_code == 404
    assert client.post(f"/api/classes/{cid}/members/csv", json={"csv": "bob"}, headers=t2).status_code == 404
    assert client.delete(f"/api/classes/{cid}/members/{alice_id}", headers=t2).status_code == 404
    assert client.get(f"/api/gradebook?class_id={cid}", headers=t2).status_code == 404
    # cannot assign A's class to B's exam, nor read/assign A's exam
    e2 = client.post("/api/exams", json=SAMPLE_EXAM, headers=t2).json()
    r = client.put(f"/api/exams/{e2['id']}/classes", json={"class_ids": [cid]}, headers=t2)
    assert r.status_code == 404 and r.json()["detail"] == "Class not found"
    e1 = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    assert client.get(f"/api/exams/{e1['id']}/classes", headers=t2).status_code == 404
    assert client.put(f"/api/exams/{e1['id']}/classes", json={"class_ids": []}, headers=t2).status_code == 404
    # nothing changed
    assert client.get(f"/api/classes/{cid}", headers=teacher).json()["member_count"] == 1
