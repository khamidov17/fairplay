"""E2E slice 2: essay exam -> students write -> teacher closes, grades essays with
the rubric, releases -> students see feedback -> gradebook. Server on :8000."""
import re
import sys
from playwright.sync_api import sync_playwright, expect

BASE = "http://127.0.0.1:8000"
TITLE = "E2E History Essay"
ESSAY = ("Overland trade declined because the Ottoman expansion raised tolls and risks on the "
         "caravan routes. At the same time Portuguese sailors opened sea routes to India, which "
         "were cheaper per tonne. The sea routes mattered more because they changed costs for everyone.")


def login(p, role, user, pw):
    p.goto(f"{BASE}/{role}/login")
    p.fill("#username", user)
    p.fill("#password", pw)
    p.get_by_role("button", name="Sign in").click()
    p.wait_for_url(re.compile(rf".*/{role}(/)?$"))


def shot(p, name):
    p.screenshot(path=f"shots/{name}.png", full_page=True)


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        T = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
        T.on("pageerror", lambda e: print("TEACHER PAGE ERROR:", e))
        login(T, "teacher", "teacher", "teacher123")

        # ---- create essay exam ----
        T.goto(f"{BASE}/teacher/exams/new")
        T.fill("#exam-title", TITLE)
        T.fill("#exam-course", "HIST 110")
        T.fill("#exam-duration", "30")
        T.get_by_role("button", name="+ Multiple choice").click()
        T.locator("[id$='-prompt']").nth(0).fill("Which sea route opened in 1498?")
        T.locator("[id$='-opt-0']").first.fill("Cape route to India")
        T.locator("[id$='-opt-1']").first.fill("Northwest passage")
        T.get_by_label("Option A is correct").first.click()
        T.get_by_role("button", name="+ Essay").click()
        T.locator("[id$='-prompt']").nth(1).fill("Explain two reasons the Silk Road declined. Which mattered more?")
        T.locator("[id$='-points']").nth(1).fill("5")
        crit = T.get_by_label(re.compile(r"Criterion \d+ description"))
        if crit.count() == 0:
            T.get_by_role("button", name="+ Add criterion").click()
        crit.nth(0).fill("Two clear reasons")
        T.get_by_label("Criterion 1 points").fill("3")
        if T.get_by_label("Criterion 2 description").count() == 0:
            T.get_by_role("button", name="+ Add criterion").click()
        T.get_by_label("Criterion 2 description").fill("Clear writing")
        T.get_by_label("Criterion 2 points").fill("2")
        for i, topic in enumerate(["sea routes", "silk road"]):
            box = T.locator("[id$='-topics']").nth(i)
            box.fill(topic)
            box.press("Enter")
        shot(T, "50-essay-editor")
        # audit S-03: every exam needs a class (dev servers have "Demo class")
        T.wait_for_function("!document.querySelector('#exam-class').disabled")
        T.locator("#exam-class").select_option(index=1)
        T.get_by_role("button", name="Save exam").click()
        T.wait_for_url(re.compile(r".*/teacher/exams/e_[A-Za-z0-9_-]+"))
        exam_url = T.url.split("?")[0]
        T.get_by_role("button", name=re.compile("Open exam", re.I)).first.click()
        T.wait_for_timeout(1500)
        code = re.search(r"(?m)^\s*(\d{6})\s*$", T.locator("body").inner_text()).group(1)
        print("exam", exam_url.rsplit("/", 1)[-1], "code", code)

        # ---- students ----
        def take(user, pw_, right, essay_text, tag):
            S = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
            S.on("pageerror", lambda e: print(user, "PAGE ERROR:", e))
            login(S, "student", user, pw_)
            card = S.locator("section, article, li, div").filter(has_text=TITLE).filter(
                has=S.get_by_role("button", name="Enter start code")).last
            card.get_by_role("button", name="Enter start code").click()
            boxes = S.locator("input")
            for i, d in enumerate(code):
                boxes.nth(i).fill(d)
            S.get_by_role("button", name="Start exam", exact=True).click()
            S.wait_for_url(re.compile(r".*/student/exam/a_.+"))
            S.wait_for_timeout(600)
            S.get_by_text("Cape route to India" if right else "Northwest passage", exact=True).first.click()
            S.get_by_role("button", name=re.compile("^Next", re.I)).click()
            S.wait_for_timeout(300)
            S.locator("textarea").first.fill(essay_text)
            S.wait_for_timeout(1500)
            shot(S, f"{tag}-essay")
            S.get_by_role("button", name=re.compile("Review and submit", re.I)).first.click()
            S.get_by_role("button", name=re.compile("^Submit", re.I)).last.click()
            S.wait_for_timeout(300)
            c = S.get_by_role("button", name=re.compile("^(Submit|Yes)", re.I))
            if "/student/done/" not in S.url and c.count():
                c.last.click()
            S.wait_for_url(re.compile(r".*/student/done/.+"), timeout=10000)
            print(user, "submitted:", re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", S.locator("body").inner_text()).group(0))
            return S

        A = take("alice", "alice123", True, ESSAY, "51-alice")
        B = take("bob", "bob123", False, "Trade got worse because of reasons.", "52-bob")

        # ---- close exam ----
        T.goto(exam_url + "?tab=live")
        T.get_by_role("button", name="Close exam", exact=True).click()
        T.get_by_role("button", name="Yes, close exam").click()
        expect(T.get_by_text("Closed").first).to_be_visible(timeout=10000)

        # ---- results -> grade ----
        T.goto(exam_url + "?tab=results")
        T.wait_for_timeout(1200)
        shot(T, "53-results-before-grading")
        T.get_by_role("link", name=re.compile(r"^Grade \d+ answer", re.I)).or_(
            T.get_by_role("button", name=re.compile(r"^Grade \d+ answer", re.I))).first.click()
        T.wait_for_url(re.compile(r".*/grading$"))
        T.wait_for_timeout(1000)
        shot(T, "54-grading-first")
        graded = 0
        for _ in range(4):
            body = T.locator("body").inner_text()
            if "Overland trade declined" in body:
                levels = (3, 2, "Strong answer with a clear judgement.")
            elif "Trade got worse" in body:
                levels = (1, 0, "Name the two reasons explicitly.")
            else:
                break
            T.get_by_role("button", name=f"Two clear reasons: {levels[0]} of 3").click()
            T.get_by_role("button", name=f"Clear writing: {levels[1]} of 2").click()
            T.get_by_label("Comment to student").fill(levels[2])
            T.get_by_role("button", name=re.compile("Save and next", re.I)).click()
            T.wait_for_timeout(900)
            graded += 1
            if graded == 2:
                break
        shot(T, "55-grading-done")
        print("graded essays:", graded)

        # ---- release ----
        T.goto(exam_url + "?tab=results")
        T.wait_for_timeout(1200)
        T.get_by_role("button", name="Release grades").first.click()
        T.wait_for_timeout(400)
        rel = T.get_by_role("button", name=re.compile("^(Release|Yes)", re.I))
        rel.last.click()
        T.wait_for_timeout(1200)
        out = T.locator("body").inner_text()
        shot(T, "56-results-released")
        print("released shown:", bool(re.search(r"released", out, re.I)))
        print("alice row 6/6:", "6/6" in out.replace(" ", ""), "| bob row 1/6:", "1/6" in out.replace(" ", ""))

        # ---- student feedback ----
        for S, who in ((A, "alice"), (B, "bob")):
            S.goto(f"{BASE}/student/grades")
            S.wait_for_timeout(1200)
            shot(S, f"57-{who}-grades")
            S.get_by_role("link", name=re.compile("Feedback", re.I)).first.click()
            S.wait_for_url(re.compile(r".*/student/grades/e_.+"))
            S.wait_for_timeout(1200)
            txt = S.locator("body").inner_text()
            shot(S, f"58-{who}-feedback")
            m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*6", txt)
            print(who, "feedback score:", m.group(0) if m else None,
                  "| has teacher comment:", ("Strong answer" in txt) or ("Name the two reasons" in txt))

        # ---- gradebook ----
        T.goto(f"{BASE}/teacher/gradebook")
        T.wait_for_timeout(1500)
        gb = T.locator("body").inner_text()
        shot(T, "59-gradebook")
        print("gradebook has exam:", TITLE in gb, "| alice 100%:", "100" in gb)
        b.close()


if __name__ == "__main__":
    sys.exit(main())
