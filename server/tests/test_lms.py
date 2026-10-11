"""Slice 5 — LMS sync, against a FAKE Canvas (httpx.MockTransport) and a fake Moodle.
Nothing here talks to a real LMS."""
import asyncio
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from app import lms
from app.lms import GradeRow, LmsAuthError, LmsError, make_provider

from .conftest import SAMPLE_EXAM, login

CANVAS = "https://canvas.test"
GOOD = "good-canvas-token-SECRET123"


class FakeCanvas:
    PAGE = 2

    def __init__(self):
        self.token = GOOD
        self.courses = [{"id": 100 + i, "name": f"Course {i}", "course_code": f"C{i}"}
                        for i in range(5)]
        self.students = {101: [
            {"id": 7, "name": "Alice Chen (Canvas)", "login_id": "alice", "email": "alice@x.edu"},
            {"id": 8, "name": "Dan Park", "login_id": "", "email": "dan.park@x.edu"},
            {"id": 9, "name": "Eve Ito", "login_id": "eve", "email": ""},
        ]}
        self.assignments: dict[int, dict] = {}
        self.grades: dict[int, dict] = {}       # assignment id -> {user id: {grade, comment}}
        self.created = 0
        self.calls: list[str] = []
        self.fail_next: list[int] = []          # status codes to return before succeeding

    def handle(self, req: httpx.Request) -> httpx.Response:
        u = urlsplit(str(req.url))
        self.calls.append(f"{req.method} {u.path}")
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), json={"errors": [{"message": "busy"}]})
        if req.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"errors": [{"message": "Invalid access token."}]})
        q = parse_qs(u.query)
        path = u.path
        if path == "/api/v1/users/self":
            return httpx.Response(200, json={"id": 1, "name": "Dr Teacher", "login_id": "teach"})
        if path == "/api/v1/courses":
            assert q.get("enrollment_type") == ["teacher"]
            return self._page(self.courses, q, path)
        m = re.fullmatch(r"/api/v1/courses/(\d+)/users", path)
        if m:
            assert q.get("enrollment_type[]") == ["student"]
            return self._page(self.students.get(int(m[1]), []), q, path)
        m = re.fullmatch(r"/api/v1/courses/(\d+)/assignments", path)
        if m and req.method == "POST":
            body = json.loads(req.content)["assignment"]
            self.created += 1
            aid = 500 + self.created
            self.assignments[aid] = {"id": aid, "course": int(m[1]), **body}
            return httpx.Response(201, json=self.assignments[aid])
        m = re.fullmatch(r"/api/v1/courses/(\d+)/assignments/(\d+)", path)
        if m and req.method == "GET":
            a = self.assignments.get(int(m[2]))
            return httpx.Response(200, json=a) if a else httpx.Response(
                404, json={"errors": [{"message": "The specified resource does not exist."}]})
        m = re.fullmatch(r"/api/v1/courses/(\d+)/assignments/(\d+)/submissions/update_grades", path)
        if m and req.method == "POST":
            aid = int(m[2])
            if aid not in self.assignments:
                return httpx.Response(404, json={"errors": [{"message": "not found"}]})
            form = parse_qs(req.content.decode())
            g = self.grades.setdefault(aid, {})
            for k, v in form.items():
                km = re.fullmatch(r"grade_data\[(\d+)\]\[(posted_grade|text_comment)\]", k)
                assert km, k
                g.setdefault(int(km[1]), {})[km[2]] = v[0]
            return httpx.Response(200, json={"id": 77, "workflow_state": "queued",
                                             "url": f"{CANVAS}/api/v1/progress/77"})
        if path == "/api/v1/progress/77":
            return httpx.Response(200, json={"id": 77, "workflow_state": "completed"})
        return httpx.Response(404, json={"errors": [{"message": "no route"}]})

    def _page(self, items, q, path):
        page = int(q.get("page", ["1"])[0])
        chunk = items[(page - 1) * self.PAGE: page * self.PAGE]
        headers = {}
        if page * self.PAGE < len(items):
            # like Canvas: the next link keeps the original filters
            keep = [(k, v) for k, vs in q.items() if k not in ("page", "per_page") for v in vs]
            nxt = urlencode(keep + [("page", page + 1), ("per_page", self.PAGE)])
            headers["Link"] = (f'<{CANVAS}{path}?{nxt}>; rel="next", '
                               f'<{CANVAS}{path}?page=1&per_page={self.PAGE}>; rel="first"')
        return httpx.Response(200, json=chunk, headers=headers)


@pytest.fixture
def canvas():
    fake = FakeCanvas()

    async def no_sleep(_s):
        return None

    lms.set_transport_factory(lambda pid, url: httpx.MockTransport(fake.handle))
    lms.set_sleep(no_sleep)
    yield fake
    lms.set_transport_factory(None)
    lms.set_sleep(None)


def connect(client, teacher, token=GOOD, **kw):
    body = {"provider": "canvas", "base_url": CANVAS + "/", "token": token, "label": "", **kw}
    return client.post("/api/lms/connections", json=body, headers=teacher)


def assert_no_token(resp):
    assert GOOD not in resp.text


# ---------------------------------------------------------------- connect
def test_providers(client, teacher):
    r = client.get("/api/lms/providers", headers=teacher)
    assert r.status_code == 200
    by = {p["id"]: p for p in r.json()}
    assert by["canvas"]["status"] == "ready" and by["canvas"]["auth"] == "token"
    assert {by[k]["status"] for k in ("moodle", "blackboard", "brightspace")} == {"beta"}


def test_lms_requires_teacher(client, alice):
    assert client.get("/api/lms/connections", headers=alice).status_code == 403
    assert client.get("/api/lms/connections").status_code == 401


def test_connect_bad_token(client, teacher, canvas):
    r = connect(client, teacher, token="wrong")
    assert r.status_code == 422
    assert "access token" in r.json()["detail"]
    assert client.get("/api/lms/connections", headers=teacher).json() == []


def test_connect_bad_urls(client, teacher, canvas):
    for url in ("http://canvas.test", "ftp://x", "https://127.0.0.1", "https://10.0.0.5",
                "https://localhost", ""):
        r = client.post("/api/lms/connections", headers=teacher,
                        json={"provider": "canvas", "base_url": url or " ", "token": GOOD})
        assert r.status_code == 422, url
    r = client.post("/api/lms/connections", headers=teacher,
                    json={"provider": "nope", "base_url": CANVAS, "token": GOOD})
    assert r.status_code == 422


def test_connect_good_token_never_returned(client, teacher, canvas):
    r = connect(client, teacher)
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["provider"] == "canvas" and c["base_url"] == CANVAS
    assert c["lms_user_name"] == "Dr Teacher"
    assert "token" not in c and "credential" not in c
    assert_no_token(r)
    lst = client.get("/api/lms/connections", headers=teacher)
    assert_no_token(lst)
    assert len(lst.json()) == 1
    # reconnecting the same LMS replaces the token instead of adding a second connection
    assert connect(client, teacher, base_url=CANVAS + "/api/v1").json()["id"] == c["id"]
    assert len(client.get("/api/lms/connections", headers=teacher).json()) == 1

    # stored sealed, not in plain text
    import sqlite3
    db = client.app.state.settings.database_url.split("///", 1)[1]
    row = sqlite3.connect(db).execute("select credential from lms_connections").fetchone()
    assert GOOD not in row[0] and row[0].startswith("v1:")


def test_secretbox_roundtrip():
    from app.lms.secretbox import open_, seal
    s = seal("k1", "tokén")
    assert open_("k1", s) == "tokén"
    with pytest.raises(ValueError):
        open_("k2", s)


# ---------------------------------------------------------------- courses + roster
def test_list_courses_paginates(client, teacher, canvas):
    cid = connect(client, teacher).json()["id"]
    canvas.calls.clear()
    r = client.get(f"/api/lms/connections/{cid}/courses", headers=teacher)
    assert r.status_code == 200, r.text
    assert [c["lms_course_id"] for c in r.json()] == ["100", "101", "102", "103", "104"]
    assert r.json()[1] == {"lms_course_id": "101", "name": "Course 1", "code": "C1", "link_id": None}
    assert canvas.calls.count("GET /api/v1/courses") == 3  # 2 + 2 + 1 per page
    assert_no_token(r)


def test_retries_on_503(client, teacher, canvas):
    cid = connect(client, teacher).json()["id"]
    canvas.fail_next = [503, 429]
    r = client.get(f"/api/lms/connections/{cid}/courses", headers=teacher)
    assert r.status_code == 200 and len(r.json()) == 5


def test_revoked_token_reports_error(client, teacher, canvas):
    cid = connect(client, teacher).json()["id"]
    canvas.token = "rotated"
    r = client.get(f"/api/lms/connections/{cid}/courses", headers=teacher)
    assert r.status_code == 502
    assert "connect again" in r.json()["detail"]
    c = client.get("/api/lms/connections", headers=teacher).json()[0]
    assert c["last_error"] and "401" in c["last_error"]


def _link(client, teacher, canvas, course="101"):
    cid = connect(client, teacher).json()["id"]
    r = client.post(f"/api/lms/connections/{cid}/courses/{course}/link", headers=teacher)
    assert r.status_code == 200, r.text
    return cid, r.json()


def test_link_and_roster_sync_idempotent(client, teacher, canvas):
    cid, link = _link(client, teacher, canvas)
    assert link["name"] == "Course 1" and link["code"] == "C1" and link["member_count"] == 3
    assert link["sync"] == {"added": 3, "updated": 0, "removed": 0, "total": 3}
    assert link["last_sync_at"] and link["last_error"] is None
    assert_no_token(client.get("/api/lms/courses", headers=teacher))

    # existing FairPlay student "alice" was reused (name not overwritten); others created
    import sqlite3
    db = client.app.state.settings.database_url.split("///", 1)[1]
    con = sqlite3.connect(db)
    users = dict(con.execute("select username, name from users where role='student'").fetchall())
    assert users["alice"] == "Alice Chen"
    assert users["dan.park"] == "Dan Park" and users["eve"] == "Eve Ito"
    n_users = len(users)

    # link again (idempotent) and re-sync: nothing new
    r = client.post(f"/api/lms/connections/{cid}/courses/101/link", headers=teacher)
    assert r.json()["id"] == link["id"] and r.json()["sync"]["added"] == 0
    r = client.post(f"/api/lms/courses/{link['id']}/sync", headers=teacher)
    assert r.status_code == 200 and r.json()["sync"] == {"added": 0, "updated": 0, "removed": 0,
                                                          "total": 3}
    assert con.execute("select count(*) from users where role='student'").fetchone()[0] == n_users

    # imported students cannot log in with a password
    r = client.post("/api/auth/login", json={"username": "eve", "password": ""})
    assert r.status_code in (401, 422)
    r = client.post("/api/auth/login", json={"username": "eve", "password": "!lms-import"})
    assert r.status_code == 401

    # roster changes: Dan leaves, Eve renamed, Frank joins (username clash with "bob" -> bob-2?)
    canvas.students[101] = [canvas.students[101][0],
                            {"id": 9, "name": "Eve Ito-Sato", "login_id": "eve"},
                            {"id": 10, "name": "Bob Other", "login_id": "bob"}]
    r = client.post(f"/api/lms/courses/{link['id']}/sync", headers=teacher).json()
    assert r["sync"] == {"added": 1, "updated": 1, "removed": 1, "total": 3}
    assert r["member_count"] == 3
    # Canvas "bob" reused the existing local student bob (same login id)
    assert con.execute("select count(*) from users where username like 'bob%'").fetchone()[0] == 1

    courses = client.get(f"/api/lms/connections/{cid}/courses", headers=teacher).json()
    assert next(c for c in courses if c["lms_course_id"] == "101")["link_id"] == link["id"]
    linked = client.get("/api/lms/courses", headers=teacher).json()
    assert [(l["id"], l["member_count"]) for l in linked] == [(link["id"], 3)]


def test_same_person_in_two_courses_is_one_user(client, teacher, canvas):
    canvas.students[102] = [{"id": 8, "name": "Dan Park", "email": "dan.park@x.edu"}]
    cid, _ = _link(client, teacher, canvas)
    r = client.post(f"/api/lms/connections/{cid}/courses/102/link", headers=teacher)
    assert r.json()["sync"]["added"] == 1
    import sqlite3
    db = client.app.state.settings.database_url.split("///", 1)[1]
    assert sqlite3.connect(db).execute(
        "select count(*) from users where username like 'dan.park%'").fetchone()[0] == 1


def test_link_unknown_course(client, teacher, canvas):
    cid = connect(client, teacher).json()["id"]
    r = client.post(f"/api/lms/connections/{cid}/courses/999/link", headers=teacher)
    assert r.status_code == 404
    assert client.post(f"/api/lms/connections/{cid + 50}/courses/101/link",
                       headers=teacher).status_code == 404
    assert client.post("/api/lms/courses/12345/sync", headers=teacher).status_code == 404


# ---------------------------------------------------------------- exams
def _exam(client, teacher, live=True):
    e = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    if live:
        e = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    return e


def test_link_exam_restricts_start(client, teacher, canvas):
    _, link = _link(client, teacher, canvas)
    e = _exam(client, teacher)
    r = client.put(f"/api/exams/{e['id']}/lms", json={"course_link_id": link["id"]}, headers=teacher)
    assert r.status_code == 200 and r.json()["course_link_id"] == link["id"]
    assert client.get(f"/api/exams/{e['id']}", headers=teacher).json()["course_link_id"] == link["id"]
    assert client.put(f"/api/exams/{e['id']}/lms", json={"course_link_id": 9999},
                      headers=teacher).status_code == 404

    alice, carol = login(client, "alice"), login(client, "carol")
    r = client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": e["start_code"]},
                    headers=carol)
    assert r.status_code == 403 and r.json()["detail"] == "This exam is for Course 1 students"
    r = client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": e["start_code"]},
                    headers=alice)
    assert r.status_code == 200, r.text

    # unlink -> everyone may start again (cache invalidated)
    r = client.put(f"/api/exams/{e['id']}/lms", json={"course_link_id": None}, headers=teacher)
    assert r.json()["course_link_id"] is None
    r = client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": e["start_code"]},
                    headers=carol)
    assert r.status_code == 200


def _finish(client, teacher, e, students):
    for name, answers, comment in students:
        h = login(client, name)
        st = client.post(f"/api/student/exams/{e['id']}/start",
                         json={"start_code": e["start_code"]}, headers=h).json()
        qid = {q["prompt"]: q["id"] for q in st["questions"]}
        ans = {qid[k]: v for k, v in answers.items()}
        client.post(f"/api/student/attempts/{st['attempt_id']}/submit",
                    json={"answers": ans, "client_seq": 1}, headers=h)
        if comment:
            client.put(f"/api/exams/{e['id']}/attempts/{st['attempt_id']}/comment",
                       json={"comment": comment}, headers=teacher)


def test_push_grades(client, teacher, canvas):
    _, link = _link(client, teacher, canvas)
    e = _exam(client, teacher)
    _finish(client, teacher, e, [
        ("alice", {"2+2?": 1, "pi?": 3.14, "Capital of France?": "paris"}, "Great work"),
        ("bob", {"2+2?": 0}, ""),   # bob not in the Canvas course (until linked) -> skipped
    ])
    # 409: no course link
    r = client.post(f"/api/exams/{e['id']}/lms/push", headers=teacher)
    assert r.status_code == 409 and "Link" in r.json()["detail"]
    client.put(f"/api/exams/{e['id']}/lms", json={"course_link_id": link["id"]}, headers=teacher)
    # bob started before the link; he is still not a member
    # 409: not released
    r = client.post(f"/api/exams/{e['id']}/lms/push", headers=teacher)
    assert r.status_code == 409 and "Release" in r.json()["detail"]
    client.post(f"/api/exams/{e['id']}/close", headers=teacher)
    assert client.post(f"/api/exams/{e['id']}/release", json={"force": False},
                       headers=teacher).status_code == 200

    r = client.post(f"/api/exams/{e['id']}/lms/push", headers=teacher)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pushed"] == 1 and body["skipped"] == 1 and body["errors"] == []
    assert_no_token(r)
    assert canvas.created == 1
    (aid, a), = canvas.assignments.items()
    assert a["name"] == "Midterm" and a["points_possible"] == 10 and a["course"] == 101
    assert a["published"] is True
    assert canvas.grades[aid] == {7: {"posted_grade": "10", "text_comment": "Great work"}}
    assert client.get(f"/api/exams/{e['id']}", headers=teacher).json()["lms_assignment_id"] == str(aid)

    # second push: same assignment, grades re-posted
    r = client.post(f"/api/exams/{e['id']}/lms/push", headers=teacher)
    assert r.json()["pushed"] == 1 and canvas.created == 1

    # assignment deleted in Canvas -> a new one is created
    canvas.assignments.clear()
    r = client.post(f"/api/exams/{e['id']}/lms/push", headers=teacher)
    assert r.json()["pushed"] == 1 and canvas.created == 2

    # Canvas fails the background job -> per-student error
    orig = canvas.handle

    def failing(req):
        if req.url.path == "/api/v1/progress/77":
            return httpx.Response(200, json={"id": 77, "workflow_state": "failed",
                                             "message": "Grades are locked"})
        return orig(req)
    canvas.handle = failing
    lms.set_transport_factory(lambda pid, url: httpx.MockTransport(canvas.handle))
    r = client.post(f"/api/exams/{e['id']}/lms/push", headers=teacher).json()
    assert r["pushed"] == 0 and r["errors"] == [{"student": "Alice Chen", "detail": "Grades are locked"}]


def test_push_token_revoked(client, teacher, canvas):
    _, link = _link(client, teacher, canvas)
    e = _exam(client, teacher)
    _finish(client, teacher, e, [("alice", {"2+2?": 1}, "")])
    client.put(f"/api/exams/{e['id']}/lms", json={"course_link_id": link["id"]}, headers=teacher)
    client.post(f"/api/exams/{e['id']}/close", headers=teacher)
    client.post(f"/api/exams/{e['id']}/release", json={"force": True}, headers=teacher)
    canvas.token = "revoked"
    r = client.post(f"/api/exams/{e['id']}/lms/push", headers=teacher)
    assert r.status_code == 502 and "token" in r.json()["detail"]


def test_delete_connection(client, teacher, canvas):
    cid, link = _link(client, teacher, canvas)
    e = _exam(client, teacher)
    client.put(f"/api/exams/{e['id']}/lms", json={"course_link_id": link["id"]}, headers=teacher)
    carol = login(client, "carol")
    assert client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": e["start_code"]},
                       headers=carol).status_code == 403
    assert client.delete(f"/api/lms/connections/{cid}", headers=teacher).status_code == 204
    assert client.get("/api/lms/connections", headers=teacher).json() == []
    assert client.get("/api/lms/courses", headers=teacher).json() == []
    ex = client.get(f"/api/exams/{e['id']}", headers=teacher).json()
    assert ex["course_link_id"] is None and ex["lms_assignment_id"] is None
    assert client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": e["start_code"]},
                       headers=carol).status_code == 200
    assert client.delete(f"/api/lms/connections/{cid}", headers=teacher).status_code == 404


# ---------------------------------------------------------------- Moodle (fake)
MOODLE = "https://moodle.test"


class FakeMoodle:
    def __init__(self):
        self.saved: list[dict] = []
        self.comments_enabled = True

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/webservice/rest/server.php" and req.method == "POST"
        f = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        assert f["moodlewsrestformat"] == "json"
        if f["wstoken"] != "mtok":
            return httpx.Response(200, json={"exception": "moodle_exception", "errorcode": "invalidtoken",
                                             "message": "Invalid token - token not found"})
        fn = f["wsfunction"]
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json={"userid": 3, "username": "prof", "fullname": "Prof X"})
        if fn == "core_enrol_get_users_courses":
            assert f["userid"] == "3"
            return httpx.Response(200, json=[{"id": 11, "shortname": "M101", "fullname": "Maths"}])
        if fn == "core_enrol_get_enrolled_users":
            assert f["courseid"] == "11"
            return httpx.Response(200, json=[
                {"id": 3, "fullname": "Prof X", "username": "prof", "roles": [{"shortname": "editingteacher"}]},
                {"id": 21, "fullname": "Ann Lee", "username": "ann", "roles": [{"shortname": "student"}]},
                {"id": 22, "fullname": "Ben Ng", "email": "ben@x.edu", "roles": [{"shortname": "student"}]}])
        if fn == "mod_assign_get_assignments":
            assert f["courseids[0]"] == "11"
            return httpx.Response(200, json={"courses": [{"id": 11, "assignments": [
                {"id": 55, "name": "Midterm"}]}]})
        if fn == "mod_assign_save_grades":
            if "grades[0][plugindata][assignfeedbackcomments_editor][text]" in f and not self.comments_enabled:
                return httpx.Response(200, json={"exception": "invalid_parameter_exception",
                                                 "errorcode": "invalidparameter", "message": "bad"})
            self.saved.append(f)
            return httpx.Response(200, content=b"null")
        return httpx.Response(200, json={"exception": "x", "errorcode": "nofunction", "message": fn})


@pytest.fixture
def moodle():
    fake = FakeMoodle()
    lms.set_transport_factory(lambda pid, url: httpx.MockTransport(fake.handle))
    yield fake
    lms.set_transport_factory(None)


def test_moodle_adapter(moodle):
    async def go():
        async with make_provider("moodle", MOODLE, "mtok") as p:
            me = await p.whoami()
            assert (me.id, me.name, me.login) == ("3", "Prof X", "prof")
            courses = await p.list_courses()
            assert [(c.id, c.name, c.code) for c in courses] == [("11", "Maths", "M101")]
            st = await p.list_students("11")
            assert [(s.lms_user_id, s.login_id, s.email) for s in st] == [
                ("21", "ann", ""), ("22", "", "ben@x.edu")]
            assert await p.ensure_assignment("11", "midterm ", 10) == "55"
            with pytest.raises(LmsError, match="Create an Assignment"):
                await p.ensure_assignment("11", "Final", 10)
            errs = await p.post_grades("11", "55", [GradeRow("21", 8.5, "Nice"), GradeRow("22", 3)])
            assert errs == []
            assert moodle.saved[0]["assignmentid"] == "55"
            assert moodle.saved[0]["grades[0][userid]"] == "21"
            assert moodle.saved[0]["grades[0][grade]"] == "8.5"
            assert moodle.saved[0]["grades[0][plugindata][assignfeedbackcomments_editor][text]"] == "Nice"
            moodle.comments_enabled = False
            errs = await p.post_grades("11", "55", [GradeRow("21", 8.5, "Nice")])
            assert len(errs) == 1 and errs[0].sent is True
        async with make_provider("moodle", MOODLE, "bad") as p:
            with pytest.raises(LmsAuthError):
                await p.whoami()
    asyncio.run(go())


def test_moodle_connect_and_link(client, teacher, moodle):
    r = client.post("/api/lms/connections", headers=teacher,
                    json={"provider": "moodle", "base_url": MOODLE, "token": "bad"})
    assert r.status_code == 422 and "token" in r.json()["detail"]
    r = client.post("/api/lms/connections", headers=teacher,
                    json={"provider": "moodle", "base_url": MOODLE, "token": "mtok"})
    assert r.status_code == 201 and "mtok" not in r.text
    cid = r.json()["id"]
    r = client.post(f"/api/lms/connections/{cid}/courses/11/link", headers=teacher)
    assert r.status_code == 200 and r.json()["sync"]["added"] == 2


def test_beta_adapters_whoami():
    """Blackboard / Brightspace: only shape checks against tiny fakes (beta, unverified)."""
    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/learn/api/public/v1/users/me":
            return httpx.Response(200, json={"id": "_5_1", "userName": "t",
                                             "name": {"given": "Tia", "family": "Ko"}})
        if req.url.path == "/learn/api/public/v1/users/_5_1/courses":
            return httpx.Response(200, json={"results": [
                {"courseId": "_9_1", "courseRoleId": "Instructor",
                 "course": {"id": "_9_1", "courseId": "BIO1", "name": "Bio"}},
                {"courseId": "_8_1", "courseRoleId": "Student", "course": {"id": "_8_1"}}],
                "paging": {}})
        if req.url.path.startswith("/d2l/api/lp/") and req.url.path.endswith("/users/whoami"):
            return httpx.Response(200, json={"Identifier": "42", "FirstName": "Di", "LastName": "Ra",
                                             "UniqueName": "dira"})
        return httpx.Response(401)
    lms.set_transport_factory(lambda pid, url: httpx.MockTransport(handle))
    try:
        async def go():
            async with make_provider("blackboard", "https://bb.test", "t") as p:
                assert (await p.whoami()).name == "Tia Ko"
                assert [(c.id, c.code) for c in await p.list_courses()] == [("_9_1", "BIO1")]
            async with make_provider("brightspace", "https://d2l.test", "t") as p:
                assert (await p.whoami()).login == "dira"
        asyncio.run(go())
    finally:
        lms.set_transport_factory(None)
