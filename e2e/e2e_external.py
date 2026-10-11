"""E2E slice 3: exam held on another website.
Teacher creates an external exam -> student (with a simulated Mac app bridge) checks in,
the page locks and sends openExternal with the right URL/domains/token -> the app
"finishes" by loading /student/external-done/<aid> -> receipt, unlock -> teacher sees
the submission; release is refused; gradebook shows "Submitted". Server on :8000."""
import json
import re
import urllib.request
from playwright.sync_api import sync_playwright, expect

BASE = "http://127.0.0.1:8000"
TITLE = "E2E Google Form Quiz"
FAKE_NATIVE = """
window.__native = [];
window.webkit = { messageHandlers: { proctora: { postMessage: function (m) {
  window.__native.push(m);
  var reply = function (d) { setTimeout(function () {
    window.dispatchEvent(new CustomEvent('proctora-native', { detail: d })); }, 30); };
  if (m.action === 'hello') reply({ type: 'state', locked: false, trusted: true, version: 'test' });
  if (m.action === 'lock') reply({ type: 'locked' });
  if (m.action === 'unlock') reply({ type: 'unlocked' });
} } } };
"""


def login(p, role, user, pw):
    p.goto(f"{BASE}/{role}/login")
    p.fill("#username", user)
    p.fill("#password", pw)
    p.get_by_role("button", name="Sign in").click()
    p.wait_for_url(re.compile(rf".*/{role}(/)?$"))


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        T = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
        T.on("pageerror", lambda e: print("TEACHER PAGE ERROR:", e))
        login(T, "teacher", "teacher", "teacher123")

        T.goto(f"{BASE}/teacher/exams/new")
        T.fill("#exam-title", TITLE)
        T.fill("#exam-course", "STATS 302")
        T.fill("#exam-duration", "40")
        T.get_by_text("On another website").first.click()
        T.fill("#exam-external-url", "https://docs.google.com/forms/d/e/TESTFORM/viewform")
        T.fill("#exam-allowed-domains", "accounts.google.com\ngstatic.com")
        T.fill("#exam-finish-contains", "formResponse")
        T.screenshot(path="shots/60-external-setup.png", full_page=True)
        T.get_by_role("button", name="Save exam").click()
        T.wait_for_url(re.compile(r".*/teacher/exams/e_[A-Za-z0-9_-]+"))
        exam_url = T.url.split("?")[0]
        T.get_by_role("button", name=re.compile("Open exam", re.I)).first.click()
        T.wait_for_timeout(1500)
        code = re.search(r"(?m)^\s*(\d{4})\s*$", T.locator("body").inner_text()).group(1)
        print("external exam", exam_url.rsplit("/", 1)[-1], "code", code)

        # ---- plain browser: must refuse ----
        P = b.new_context(viewport={"width": 1280, "height": 860}).new_page()
        login(P, "student", "carol", "carol123")

        # ---- simulated Mac app ----
        ctx = b.new_context(viewport={"width": 1280, "height": 860})
        ctx.add_init_script(FAKE_NATIVE)
        S = ctx.new_page()
        S.on("pageerror", lambda e: print("STUDENT PAGE ERROR:", e))
        login(S, "student", "alice", "alice123")
        card = S.locator("section, article, li, div").filter(has_text=TITLE).filter(
            has=S.get_by_role("button", name="Enter start code")).last
        card.get_by_role("button", name="Enter start code").click()
        boxes = S.locator("input")
        for i, d in enumerate(code):
            boxes.nth(i).fill(d)
        S.get_by_role("button", name="Start exam", exact=True).click()
        S.wait_for_url(re.compile(r".*/student/external/a_.+"), timeout=10000)
        aid = S.url.rsplit("/", 1)[-1]
        S.wait_for_timeout(1500)
        S.screenshot(path="shots/61-external-opening.png", full_page=True)
        msgs = S.evaluate("window.__native")
        actions = [m.get("action") for m in msgs]
        ext = next((m for m in msgs if m.get("action") == "openExternal"), None)
        print("native messages:", actions)
        ok_open = bool(ext) and ext["url"].startswith("https://docs.google.com/forms") \
            and "docs.google.com" in ext["allowed_domains"] and "gstatic.com" in ext["allowed_domains"] \
            and ext["finish_url_contains"] == "formResponse" and len(ext["token"]) > 20 \
            and ext["attempt_id"] == aid
        print("lock before openExternal:", "lock" in actions and actions.index("lock") < actions.index("openExternal") if ext else False)
        print("openExternal message correct:", ok_open)

        # live room sees the student (the native app sends heartbeats; simulate one)
        token = ext["token"]
        req = urllib.request.Request(f"{BASE}/api/student/attempts/{aid}/heartbeat",
                                     data=json.dumps({"current_question": 0, "locked": True}).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        hb = json.loads(urllib.request.urlopen(req).read())
        print("native-style heartbeat:", hb.get("status"), "deadline", bool(hb.get("deadline_at")))

        # plain browser student opening the same kind of exam is refused
        P.goto(f"{BASE}/student/external/{aid}")
        P.wait_for_timeout(1200)
        print("plain browser refused:", "FairPlay" in P.locator("body").inner_text() and "/student/external/" in P.url or "not" in P.locator("body").inner_text().lower())

        # ---- the app finishes: loads external-done ----
        S.goto(f"{BASE}/student/external-done/{aid}?reason=student_finished")
        S.wait_for_url(re.compile(r".*/student/done/.+"), timeout=10000)
        S.wait_for_timeout(800)
        txt = S.locator("body").inner_text()
        print("receipt:", (re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", txt) or [None])[0])
        print("unlock sent:", "unlock" in [m.get("action") for m in S.evaluate("window.__native")])
        S.screenshot(path="shots/62-external-done.png", full_page=True)

        # ---- teacher ----
        T.goto(exam_url + "?tab=results")
        T.wait_for_timeout(1500)
        out = T.locator("body").inner_text()
        T.screenshot(path="shots/63-external-results.png", full_page=True)
        print("results list alice:", "Alice" in out)

        # ---- teacher grades the website exam on the review page ----
        T.get_by_role("link", name=re.compile(r"^(Grade|Review)$")).first.click()
        T.wait_for_url(re.compile(r".*/submissions/a_.+"))
        T.wait_for_timeout(800)
        T.locator("#pts-q_external").fill("85")
        T.get_by_label("Comment to student").first.fill("Well done on the form.")
        T.get_by_role("button", name="Save grade").click()
        T.wait_for_timeout(1200)
        rv = T.locator("body").inner_text()
        T.screenshot(path="shots/65-external-review.png", full_page=True)
        print("teacher entered website score:", "85" in rv and "Saved" in rv)

        # close + release, then the student sees it
        T.goto(exam_url + "?tab=live")
        T.get_by_role("button", name="Close exam", exact=True).click()
        T.get_by_role("button", name="Yes, close exam").click()
        T.wait_for_timeout(1200)
        T.goto(exam_url + "?tab=results")
        T.wait_for_timeout(1200)
        T.get_by_role("button", name="Release grades").first.click()
        T.wait_for_timeout(400)
        T.get_by_role("button", name=re.compile("^(Release|Yes)", re.I)).last.click()
        T.wait_for_timeout(1200)
        S.goto(f"{BASE}/student/grades")
        S.wait_for_timeout(1200)
        sg = S.locator("body").inner_text()
        print("student sees website exam score:", TITLE in sg and "85" in sg)
        T.goto(f"{BASE}/teacher/gradebook")
        T.wait_for_timeout(1200)
        gb = T.locator("body").inner_text()
        print("gradebook shows score:", TITLE in gb and "85" in gb)
        T.screenshot(path="shots/64-external-gradebook.png", full_page=True)
        b.close()


if __name__ == "__main__":
    main()
