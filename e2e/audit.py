"""Click-through audit of every page, link and button for teacher and student.

Seeds realistic data through the API, then for every route: loads it, records console
errors / page errors / failed API calls / broken text, and clicks every visible link and
button (each from a fresh load of the page). Destructive buttons are skipped (they are
covered by the flow tests). Prints a list of problems; exit code 1 if any.
Server on :8000 with a fresh database.
"""
import json
import re
import sys
import urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
SKIP = re.compile(r"^(sign out|log out|delete|close exam|yes, close|remove|move question|"
                  r"cancel|keep working|submit exam|yes, submit|finish|hide grades|reset)", re.I)
BAD_TEXT = re.compile(r"\bundefined\b|\bNaN\b|\[object Object\]|Infinity")
problems: list[str] = []


def api(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req) as r:
            t = r.read()
            return json.loads(t) if t else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code} {e.read()[:200]!r}")


def login(u, p):
    return api("POST", "/api/auth/login", {"username": u, "password": p})["token"]


def q(type_, **kw):
    base = {"type": type_, "prompt": kw.pop("prompt", f"A {type_} question?"), "points": kw.pop("points", 2),
            "options": [], "correct_index": None, "answer_number": None, "tolerance": None,
            "accepted_answers": [], "rubric": [], "min_words": None, "max_words": None,
            "topics": [kw.pop("topic", "audit topic")]}
    base.update(kw)
    return base


def seed():
    T = login("teacher", "teacher123")
    A, B, C = login("alice", "alice123"), login("bob", "bob123"), login("carol", "carol123")
    full = {"title": "Audit Full Exam", "course": "AUD 100", "room": "AB 1087", "duration_min": 30,
            "scheduled_at": "2026-12-01T06:00:00Z", "shuffle_questions": True, "questions": [
                q("mcq", prompt="Pick B", options=["A", "B", "C"], correct_index=1),
                q("mcq", prompt="True or false: 1+1=2", options=["True", "False"], correct_index=0, points=1),
                q("numeric", prompt="2+2?", answer_number=4, tolerance=0),
                q("short", prompt="Capital of France?", accepted_answers=["paris"]),
                q("essay", prompt="Explain integrals.", points=5,
                  rubric=[{"label": "Clarity", "points": 3}, {"label": "Correctness", "points": 2}],
                  min_words=5, max_words=200)]}
    e1 = api("POST", "/api/exams", full, T)
    api("POST", f"/api/exams/{e1['id']}/open", None, T)
    e1 = api("GET", f"/api/exams/{e1['id']}", None, T)
    ids = {x["prompt"]: x["id"] for x in e1["questions"]}
    code = e1["start_code"]
    a1 = api("POST", f"/api/student/exams/{e1['id']}/start", {"start_code": code}, A)
    api("POST", f"/api/student/attempts/{a1['attempt_id']}/submit", {"answers": {
        ids["Pick B"]: 1, ids["True or false: 1+1=2"]: 0, ids["2+2?"]: 4, ids["Capital of France?"]: "Paris",
        ids["Explain integrals."]: "Integrals add up tiny slices of area under a curve."}, "client_seq": 1}, A)
    b1 = api("POST", f"/api/student/exams/{e1['id']}/start", {"start_code": code}, B)
    api("POST", f"/api/student/attempts/{b1['attempt_id']}/submit", {"answers": {
        ids["Pick B"]: 0, ids["Capital of France?"]: "Lyon", ids["Explain integrals."]: "No idea really sorry."},
        "client_seq": 1}, B)
    api("POST", f"/api/student/attempts/{b1['attempt_id']}/events", {"events": [
        {"type": "blocked_shortcut", "detail": "Cmd-key49", "client_time": "2026-10-11T10:00:00Z"}]}, B)
    api("POST", f"/api/student/exams/{e1['id']}/start", {"start_code": code}, C)  # carol still writing

    # closed + graded + released exam
    e2 = api("POST", "/api/exams", {**full, "title": "Audit Released Exam", "shuffle_questions": False}, T)
    api("POST", f"/api/exams/{e2['id']}/open", None, T)
    e2 = api("GET", f"/api/exams/{e2['id']}", None, T)
    a2 = api("POST", f"/api/student/exams/{e2['id']}/start", {"start_code": e2["start_code"]}, A)
    ess = next(x["id"] for x in e2["questions"] if x["type"] == "essay")
    api("POST", f"/api/student/attempts/{a2['attempt_id']}/submit",
        {"answers": {ess: "A thoughtful essay about integrals and areas."}, "client_seq": 1}, A)
    api("POST", f"/api/exams/{e2['id']}/close", None, T)
    api("PUT", f"/api/exams/{e2['id']}/attempts/{a2['attempt_id']}/grades/{ess}",
        {"points": 4, "comment": "Good", "rubric_scores": [2, 2]}, T)
    api("PUT", f"/api/exams/{e2['id']}/attempts/{a2['attempt_id']}/comment", {"comment": "Nice work"}, T)
    api("POST", f"/api/exams/{e2['id']}/release", {"force": True}, T)

    # draft
    e3 = api("POST", "/api/exams", {**full, "title": "Audit Draft Exam"}, T)

    # website exam: alice submitted, scored, closed, released
    e4 = api("POST", "/api/exams", {"title": "Audit Website Exam", "course": "AUD 200", "duration_min": 20,
                                    "mode": "external", "external_url": "http://localhost:8000/demo-quiz.html",
                                    "allowed_domains": ["example.com"], "finish_url_contains": "submitted",
                                    "external_points": 20, "questions": []}, T)
    api("POST", f"/api/exams/{e4['id']}/open", None, T)
    e4 = api("GET", f"/api/exams/{e4['id']}", None, T)
    a4 = api("POST", f"/api/student/exams/{e4['id']}/start", {"start_code": e4["start_code"]}, A)
    api("POST", f"/api/student/attempts/{a4['attempt_id']}/submit", {"answers": {}, "client_seq": 0}, A)
    # live website exam bob is "in"
    e5 = api("POST", "/api/exams", {"title": "Audit Website Live", "duration_min": 20, "mode": "external",
                                    "external_url": "https://example.com/quiz", "questions": []}, T)
    api("POST", f"/api/exams/{e5['id']}/open", None, T)
    e5 = api("GET", f"/api/exams/{e5['id']}", None, T)
    b5 = api("POST", f"/api/student/exams/{e5['id']}/start", {"start_code": e5["start_code"]}, B)
    # paper exam (draft, printable)
    e6 = api("POST", "/api/exams", {**full, "title": "Audit Paper Exam", "delivery": "paper"}, T)
    return dict(e1=e1, e2=e2, e3=e3, e4=e4, e5=e5, e6=e6, a1=a1, b1=b1, a2=a2, a4=a4, b5=b5)


def audit_page(ctx, url, role, label, click=True):
    page = ctx.new_page()
    errs = []
    page.on("console", lambda m: errs.append(f"console {m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
    page.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url}")
            if "/api/" in r.url and r.status >= 500 else None)
    page.goto(url)
    page.wait_for_timeout(1200)
    final = page.url
    body = page.locator("body").inner_text()
    if BAD_TEXT.search(body):
        problems.append(f"[{role}] {label}: bad text {BAD_TEXT.search(body).group(0)!r} at {url}")
    if re.search(r"/login$", final) and "/login" not in url:
        problems.append(f"[{role}] {label}: bounced to login from {url}")
    for e in errs:
        problems.append(f"[{role}] {label}: {e}")
    targets = []
    if click:
        loc = page.locator("a:visible, button:visible, [role=tab]:visible, [role=checkbox]:visible, [role=radio]:visible")
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                name = (el.inner_text() or el.get_attribute("aria-label") or "").strip().split("\n")[0][:60]
                href = el.get_attribute("href")
                disabled = el.is_disabled()
            except Exception:
                continue
            if disabled or SKIP.search(name or ""):
                continue
            targets.append((i, name, href))
    page.close()
    clicked = 0
    for i, name, href in targets:
        if href and href.startswith("http") and "127.0.0.1:8000" not in href:
            continue  # external links (exam website) are not followed
        p = ctx.new_page()
        perrs = []
        p.on("pageerror", lambda e: perrs.append(f"pageerror: {e}"))
        p.on("console", lambda m: perrs.append(f"console error: {m.text}") if m.type == "error" else None)
        p.on("response", lambda r: perrs.append(f"HTTP {r.status} {r.url}")
             if "/api/" in r.url and r.status >= 500 else None)
        p.goto(url)
        p.wait_for_timeout(900)
        loc = p.locator("a:visible, button:visible, [role=tab]:visible, [role=checkbox]:visible, [role=radio]:visible")
        if i >= loc.count():
            p.close()
            continue
        try:
            loc.nth(i).click(timeout=3000)
            p.wait_for_timeout(900)
            clicked += 1
        except Exception as ex:
            problems.append(f"[{role}] {label}: could not click {name!r}: {str(ex).splitlines()[0][:120]}")
        txt = p.locator("body").inner_text()
        if BAD_TEXT.search(txt):
            problems.append(f"[{role}] {label} → {name!r}: bad text {BAD_TEXT.search(txt).group(0)!r}")
        if "Something went wrong" in txt or "Unexpected Application Error" in txt:
            problems.append(f"[{role}] {label} → {name!r}: app crashed")
        for e in perrs:
            if "403" in e or "404" in e or "409" in e:
                continue
            problems.append(f"[{role}] {label} → {name!r}: {e}")
        p.close()
    print(f"  {role:7} {label:38} links/buttons clicked: {clicked}")


def main():
    d = seed()
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        # ---------- teacher ----------
        T = b.new_context(viewport={"width": 1366, "height": 900})
        tok = login("teacher", "teacher123")
        T.add_init_script(f"localStorage.setItem('proctora:token:teacher', {json.dumps(tok)})")
        e1, e2, e3, e4, e5 = d["e1"]["id"], d["e2"]["id"], d["e3"]["id"], d["e4"]["id"], d["e5"]["id"]
        teacher_pages = [
            ("/teacher", "home"), ("/teacher/exams", "exams list"), ("/teacher/exams/new", "new exam"),
            (f"/teacher/exams/{e1}?tab=live", "live room (live exam)"),
            (f"/teacher/exams/{e1}?tab=results", "results (live exam)"),
            (f"/teacher/exams/{e1}?tab=questions", "questions tab"),
            (f"/teacher/exams/{e1}/grading", "grading queue"),
            (f"/teacher/exams/{e1}/submissions/{d['a1']['attempt_id']}", "review alice"),
            (f"/teacher/exams/{e1}/submissions/{d['b1']['attempt_id']}", "review bob"),
            (f"/teacher/exams/{e2}?tab=results", "results (released)"),
            (f"/teacher/exams/{e2}/submissions/{d['a2']['attempt_id']}", "review released"),
            (f"/teacher/exams/{e3}", "draft exam page"), (f"/teacher/exams/{e3}/edit", "edit draft"),
            (f"/teacher/exams/{e4}?tab=results", "website exam results"),
            (f"/teacher/exams/{e4}/submissions/{d['a4']['attempt_id']}", "website exam review"),
            (f"/teacher/exams/{e4}/grading", "website exam grading"),
            (f"/teacher/exams/{e5}?tab=live", "website exam live"),
            (f"/teacher/exams/{e4}?tab=questions", "website exam setup view"),
            ("/teacher/grading", "grading list"), ("/teacher/gradebook", "gradebook"),
            ("/teacher/insights", "insights"), ("/teacher/students", "students"),
            ("/teacher/students/alice", "student profile"), ("/teacher/practice", "practice"),
            ("/teacher/practice?student=bob", "practice for bob"),
            ("/teacher/scan", "scan & grade overview"), (f"/teacher/exams/{d['e6']['id']}/scan", "scan paper exam"),
            (f"/teacher/exams/{d['e6']['id']}", "paper exam page"), ("/teacher/lms", "lms sync"),
        ]
        print("Teacher")
        for path, label in teacher_pages:
            audit_page(T, BASE + path, "teacher", label)
        # ---------- student (alice) ----------
        S = b.new_context(viewport={"width": 1280, "height": 860})
        stok = login("alice", "alice123")
        S.add_init_script(f"localStorage.setItem('proctora:token:student', {json.dumps(stok)})")
        student_pages = [
            ("/student", "home"), ("/student/exams", "exams"),
            (f"/student/exams/{e5}/checkin", "check-in (website exam)"),
            (f"/student/exams/{e1}/checkin", "check-in (submitted exam)"),
            (f"/student/done/{d['a1']['attempt_id']}", "done page"),
            ("/student/grades", "grades"), (f"/student/grades/{e2}", "feedback (released)"),
            (f"/student/grades/{e1}", "feedback (not released)"),
            (f"/student/external/{d['a4']['attempt_id']}", "website exam page (submitted)"),
            ("/student/practice", "practice"),
        ]
        print("Student (alice)")
        for path, label in student_pages:
            audit_page(S, BASE + path, "student", label)
        # carol: in-progress exam page with every question type
        C = b.new_context(viewport={"width": 1280, "height": 860})
        ctok = login("carol", "carol123")
        C.add_init_script(f"localStorage.setItem('proctora:token:student', {json.dumps(ctok)})")
        att = next(x for x in api("GET", "/api/student/exams", None, ctok) if x["id"] == e1)["attempt"]
        print("Student (carol, writing)")
        audit_page(C, f"{BASE}/student/exam/{att['attempt_id']}", "student", "exam page (writing)")
        audit_page(C, f"{BASE}/student", "student", "home while writing")
        b.close()
    print()
    if problems:
        print(f"{len(problems)} PROBLEMS")
        for p in dict.fromkeys(problems):
            print(" -", p)
        sys.exit(1)
    print("No problems found")


if __name__ == "__main__":
    main()
