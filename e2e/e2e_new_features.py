"""E2E: the new features, clicked through in a real browser.
Student signs up -> teacher creates a class and adds a student -> another student joins with
the class code -> teacher creates an exam assigned to that class and opens it -> a student
outside the class cannot see or start it -> a class student takes and submits it -> teacher
opens the session timeline -> superadmin dashboard -> grading quality pages.
Server on FAIRPLAY_BASE (default :8000), development mode, fresh database."""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get("FAIRPLAY_BASE", "http://127.0.0.1:8000").rstrip("/")
SHOTS = os.environ.get("FAIRPLAY_SHOTS", "shots")
STAMP = str(int(time.time()))[-5:]
NEWBIE = f"dina{STAMP}"
CLASS = f"Calculus I {STAMP}"
TITLE = f"E2E Class Quiz {STAMP}"
results: list[tuple[str, bool]] = []
errors: list[str] = []


def check(name, ok):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name)


def api(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req) as r:
            t = r.read()
            return r.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def page(b, who):
    p = b.new_context(viewport={"width": 1366, "height": 880}).new_page()
    p.on("pageerror", lambda e: errors.append(f"{who}: {e}"))
    return p


def login(p, role, user, pw):
    p.goto(f"{BASE}/{role}/login")
    p.fill("#username", user)
    p.fill("#password", pw)
    p.get_by_role("button", name="Sign in").click()
    p.wait_for_url(re.compile(rf".*/{role}/?$"), timeout=10000)


def shot(p, name):
    os.makedirs(SHOTS, exist_ok=True)
    p.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch()

        # 1. a new student signs up
        D = page(b, "dina")
        D.goto(f"{BASE}/register")
        D.get_by_text("Student", exact=True).first.click()
        D.fill("#reg-name", "Dina Rustamova")
        D.fill("#reg-email", f"{NEWBIE}@example.edu")
        D.fill("#reg-username", NEWBIE)
        D.fill("#reg-password", "dina-pass-1")
        D.fill("#reg-confirm", "dina-pass-1")
        shot(D, "n01-register")
        D.get_by_role("button", name="Create account").click()
        D.wait_for_url(re.compile(r".*/student/?$"), timeout=10000)
        check("student signs up and lands on the student home", "/student" in D.url)
        st, res = api("POST", "/api/auth/login", {"username": f"{NEWBIE}@example.edu", "password": "dina-pass-1"})
        check("new student can sign in with email", st == 200 and res["role"] == "student")
        st, _ = api("POST", "/api/auth/register", {"name": "X", "email": f"{NEWBIE}@example.edu",
                                                   "username": NEWBIE, "password": "dina-pass-1", "role": "student"})
        check("duplicate sign-up refused", st in (400, 409, 422))

        # 2. teacher creates a class and adds alice
        T = page(b, "teacher")
        login(T, "teacher", "teacher", "teacher123")
        T.goto(f"{BASE}/teacher/classes")
        T.get_by_role("button", name="New class").first.click()
        T.fill("#cl-name", CLASS)
        T.fill("#cl-section", "MATH 101-02")
        T.get_by_role("button", name="Create class").click()
        T.wait_for_url(re.compile(r".*/teacher/classes/\d+"), timeout=10000)
        code = T.locator(".cl-code").first.inner_text().strip()
        check("class created with a join code", bool(re.fullmatch(r"[A-Z0-9]{6,8}", code)))
        T.fill("#cl-add-text", "alice\nnobody_here")
        T.get_by_role("button", name="Add students").click()
        T.wait_for_timeout(1200)
        body = T.locator("body").inner_text()
        check("alice added to the class", "Alice Chen" in body)
        check("unknown username reported as not found", "nobody_here" in body)
        shot(T, "n02-class")

        # 3. dina joins with the code
        D.goto(f"{BASE}/student/classes")
        D.fill("#join-code", code.lower())
        D.get_by_role("button", name="Join", exact=True).click()
        D.wait_for_timeout(1200)
        check("student joins the class with the code", CLASS in D.locator("body").inner_text())
        shot(D, "n03-joined")
        T.reload()
        T.wait_for_timeout(1000)
        check("teacher sees the joined student", "Dina Rustamova" in T.locator("body").inner_text())

        # 4. exam assigned to the class (editor UI), then opened
        T.goto(f"{BASE}/teacher/exams/new")
        T.fill("#exam-title", TITLE)
        T.fill("#exam-course", "MATH 101")
        T.fill("#exam-duration", "20")
        T.get_by_role("button", name="+ Multiple choice").click()
        T.locator("[id$='-prompt']").nth(0).fill("d/dx of x^2 at x=1?")
        T.locator("[id$='-opt-0']").first.fill("2")
        T.locator("[id$='-opt-1']").first.fill("1")
        T.get_by_label("Option A is correct").first.click()
        box = T.locator("[id$='-topics']").first
        box.fill("derivatives")
        box.press("Enter")
        T.get_by_label(re.compile(re.escape(CLASS))).check()
        shot(T, "n04-editor")
        T.get_by_role("button", name="Save exam").click()
        T.wait_for_url(re.compile(r".*/teacher/exams/e_[A-Za-z0-9_-]+"), timeout=10000)
        exam_url = T.url.split("?")[0]
        eid = exam_url.rsplit("/", 1)[-1]
        T.get_by_role("button", name=re.compile("Open exam", re.I)).first.click()
        T.wait_for_timeout(1500)
        exam_code = re.search(r"(?m)^\s*(\d{4})\s*$", T.locator("body").inner_text()).group(1)
        check("exam page shows the assigned class", CLASS in T.locator("body").inner_text())
        shot(T, "n05-exam-open")

        # 5. bob is not in the class
        _, bt = api("POST", "/api/auth/login", {"username": "bob", "password": "bob123"})
        st, lst = api("GET", "/api/student/exams", None, bt["token"])
        check("exam hidden from a student outside the class", all(e["id"] != eid for e in lst))
        st, res = api("POST", f"/api/student/exams/{eid}/start", {"start_code": exam_code}, bt["token"])
        check("start refused for a student outside the class (403)", st == 403)

        # 6. dina takes it in the browser
        D.goto(f"{BASE}/student")
        D.wait_for_timeout(800)
        card = D.locator("section, article, li, div").filter(has_text=TITLE).filter(
            has=D.get_by_role("button", name="Enter start code")).last
        card.get_by_role("button", name="Enter start code").click()
        boxes = D.locator("input")
        for i, d in enumerate(exam_code):
            boxes.nth(i).fill(d)
        D.get_by_role("button", name="Start exam", exact=True).click()
        D.wait_for_url(re.compile(r".*/student/exam/a_.+"), timeout=10000)
        aid = D.url.rsplit("/", 1)[-1]
        D.wait_for_timeout(600)
        D.get_by_text("2", exact=True).first.click()
        D.wait_for_timeout(1200)
        D.get_by_role("button", name=re.compile("Review and submit", re.I)).first.click()
        D.get_by_role("button", name=re.compile("^Submit", re.I)).last.click()
        D.wait_for_timeout(400)
        c = D.get_by_role("button", name=re.compile("^(Submit|Yes)", re.I))
        if "/student/done/" not in D.url and c.count():
            c.last.click()
        D.wait_for_url(re.compile(r".*/student/done/.+"), timeout=10000)
        receipt = re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", D.locator("body").inner_text())
        check("class student submits and gets a receipt", bool(receipt))
        shot(D, "n06-done")

        # 7. session timeline
        T.goto(f"{BASE}/teacher/exams/{eid}/submissions/{aid}/timeline")
        T.wait_for_timeout(1500)
        tl = T.locator("body").inner_text()
        shot(T, "n07-timeline")
        check("timeline shows start and submit", bool(re.search(r"start", tl, re.I)) and bool(re.search(r"submit", tl, re.I)))
        check("timeline shows the receipt", receipt is not None and receipt.group(0) in tl)

        # 8. superadmin dashboard
        A = page(b, "admin")
        A.goto(f"{BASE}/admin/login")
        A.fill("#username", "admin")
        A.fill("#password", "admin123")
        A.get_by_role("button", name="Sign in").click()
        A.wait_for_url(re.compile(r".*/admin/?$"), timeout=10000)
        A.wait_for_timeout(1500)
        ad = A.locator("body").inner_text()
        shot(A, "n08-admin")
        check("admin dashboard lists the demo teacher", "Demo Teacher" in ad)
        _, at = api("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
        st, stats = api("GET", "/api/admin/stats", None, at["token"])
        check("admin stats reachable", st == 200)
        print("   admin stats:", stats)
        _, tt = api("POST", "/api/auth/login", {"username": "teacher", "password": "teacher123"})
        st, _ = api("GET", "/api/admin/stats", None, tt["token"])
        check("teacher refused on admin API (403)", st == 403)

        # 9. grading quality pages render
        for path, name in (("/teacher/quality", "n09-quality"), ("/teacher/quality/benchmarks", "n10-benchmarks"),
                           (f"/teacher/quality/exams/{eid}", "n11-exam-quality"),
                           ("/teacher/quality/second-marking", "n12-second-marking")):
            T.goto(BASE + path)
            T.wait_for_timeout(1300)
            shot(T, name)
            txt = T.locator("body").inner_text()
            check(f"{path} renders", len(txt) > 50 and "Something went wrong" not in txt)

        check("no JavaScript page errors", not errors)
        for e in errors:
            print("   ", e)
        b.close()
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
