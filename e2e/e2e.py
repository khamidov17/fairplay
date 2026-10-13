"""End-to-end: teacher builds + opens an exam, two students take it, teacher sees live room and results."""
import re, sys
from playwright.sync_api import sync_playwright, expect

BASE = "http://127.0.0.1:8000"

def login(pg, role, user, pw):
    pg.goto(f"{BASE}/{role}/login")
    pg.fill("#username", user); pg.fill("#password", pw)
    pg.get_by_role("button", name="Sign in").click()
    pg.wait_for_url(f"**/{role}")

with sync_playwright() as p:
    b = p.chromium.launch()
    T = b.new_context(viewport={"width": 1280, "height": 860}).new_page()
    login(T, "teacher", "teacher", "teacher123")
    T.goto(f"{BASE}/teacher/exams/new")
    T.fill("#exam-title", "E2E Calculus Quiz")
    T.fill("#exam-duration", "20")
    T.get_by_role("button", name="+ Multiple choice").click()
    T.get_by_role("button", name="+ Numeric").click()
    T.get_by_role("button", name="+ Short answer").click()
    prompts = T.locator("[id$='-prompt']")
    prompts.nth(0).fill("Derivative of x^2 at x = 3?")
    T.locator("[id$='-opt-0']").first.fill("3")
    T.locator("[id$='-opt-1']").first.fill("6")
    T.get_by_label("Option B is correct").check()
    prompts.nth(1).fill("Integral of 2x from 0 to 3?")
    T.locator("[id$='-answer']").fill("9")
    T.locator("[id$='-tolerance']").fill("0.01")
    prompts.nth(2).fill("Capital of France?")
    T.locator("[id$='-accepted']").fill("Paris")
    for i, topic in enumerate(["derivatives", "integrals", "geography"]):
        box = T.locator("[id$='-topics']").nth(i)
        box.fill(topic)
        box.press("Enter")
    T.screenshot(path="shots/10-editor-filled.png", full_page=True)
    # audit S-03: every exam needs a class (dev servers have "Demo class")
    T.wait_for_function("!document.querySelector('#exam-class').disabled")
    T.locator("#exam-class").select_option(index=1)
    T.get_by_role("button", name="Save exam").click()
    T.wait_for_url(re.compile(r".*/teacher/exams/e_[A-Za-z0-9_-]+$"))
    exam_url = T.url
    print("exam saved:", exam_url)
    T.get_by_role("button", name=re.compile("Open exam", re.I)).click()
    # some UIs confirm
    confirm = T.get_by_role("button", name=re.compile("^(Open|Yes, open|Open now)", re.I))
    T.wait_for_timeout(600)
    if T.get_by_text(re.compile(r"\b\d{6}\b")).count() == 0 and confirm.count():
        confirm.last.click()
    T.wait_for_timeout(1200)
    body = T.locator("body").inner_text()
    m = re.search(r"(?m)^\s*(\d{6})\s*$", body) or re.search(r"\b(\d{6})\b", body)
    code = m.group(1)
    print("start code:", code)
    T.screenshot(path="shots/11-live-room-empty.png", full_page=True)

    def take(user, pw, answers_right, shot):
        S = b.new_context(viewport={"width": 1280, "height": 860}).new_page()
        login(S, "student", user, pw)
        S.wait_for_timeout(800)
        S.screenshot(path=f"shots/{shot}-a-home.png", full_page=True)
        S.get_by_text("E2E Calculus Quiz").first.wait_for()
        # open code entry
        S.get_by_role("button", name="Enter start code").first.click()
        S.wait_for_timeout(400)
        inputs = S.locator("input")
        if inputs.count() == 6:
            for i, d in enumerate(code): inputs.nth(i).fill(d)
        else:
            inputs.first.fill(code)
        S.screenshot(path=f"shots/{shot}-b-code.png", full_page=True)
        S.get_by_role("button", name="Start exam", exact=True).click()
        S.wait_for_url(re.compile(r".*/student/exam/a_[A-Za-z0-9_-]+"))
        S.wait_for_timeout(800)
        S.screenshot(path=f"shots/{shot}-c-q1.png", full_page=True)
        # Q1 mcq
        S.get_by_text("6" if answers_right else "3", exact=True).first.click()
        S.get_by_role("button", name=re.compile("^Next", re.I)).click()
        S.wait_for_timeout(300)
        S.locator("input").first.fill("9.005" if answers_right else "8")
        S.get_by_role("button", name=re.compile("^Next", re.I)).click()
        S.wait_for_timeout(300)
        S.locator("input").first.fill("  paris " if answers_right else "London")
        S.wait_for_timeout(1600)  # debounce + sync
        S.screenshot(path=f"shots/{shot}-d-q3.png", full_page=True)
        return S

    A = take("alice", "alice123", True, "20-alice")
    B = take("bob", "bob123", False, "30-bob")

    T.wait_for_timeout(5500)  # live room polls every 3 s, heartbeats every 5 s
    live = T.locator("body").inner_text()
    T.screenshot(path="shots/12-live-room-writing.png", full_page=True)
    print("live room shows alice:", "Alice" in live, "bob:", "Bob" in live)

    for S, who in ((A, "alice"), (B, "bob")):
        S.get_by_role("button", name=re.compile("Review", re.I)).first.click()
        S.wait_for_timeout(400)
        S.screenshot(path=f"shots/40-{who}-review.png", full_page=True)
        S.get_by_role("button", name=re.compile("^Submit", re.I)).last.click()
        S.wait_for_timeout(400)
        c = S.get_by_role("button", name=re.compile("^(Submit|Yes)", re.I))
        if "/student/done/" not in S.url and c.count():
            c.last.click()
        S.wait_for_url(re.compile(r".*/student/done/.+"), timeout=10000)
        txt = S.locator("body").inner_text()
        rc = re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", txt)
        print(who, "submitted, receipt:", rc.group(0) if rc else None)
        S.screenshot(path=f"shots/41-{who}-done.png", full_page=True)

    T.reload(); T.wait_for_timeout(1500)
    res_tab = T.get_by_role("tab", name=re.compile("Results", re.I)).or_(T.get_by_role("button", name=re.compile("Results", re.I)))
    if res_tab.count(): res_tab.first.click()
    T.wait_for_timeout(1200)
    out = T.locator("body").inner_text()
    T.screenshot(path="shots/13-results.png", full_page=True)
    print("results snippet:\n", out[:1500])
    b.close()
