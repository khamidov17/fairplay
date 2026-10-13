"""End-to-end in REAL exam mode (FAIRPLAY_REQUIRE_APP=1, audit 2026-10-12).

The Mac app is simulated inside Chromium: an init script provides
window.webkit.messageHandlers.proctora and behaves like FairPlay.app (signs the start
challenge with the app key, answers lock/unlock, sends signed app beats, unlocks only
after the server confirms the hand-in). Server: FAIRPLAY_DEV=1 (dev app key) on :8000.

Checks: plain browser refused; app start works; teacher live room shows "Writing"
(locked from signed beats); helper on another "Mac" refused + flagged; same login in a
browser cannot read/write the attempt; invigilator code checked by the server;
submit -> unlock only after the server confirms; results + student grades.
"""
import json
import re
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
KEY = "fairplay-dev-app-key-not-secret"
ok_all = True


def check(name, cond):
    global ok_all
    ok_all &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)


def api(method, path, body=None, token=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, method=method, headers=h,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req) as r:
            t = r.read()
            return r.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


FAKE_APP = """
(() => {
  const KEY = %KEY%, DEVICE = %DEVICE%;
  const st = { locked: false, exam: null, timer: null, ts: 0 };
  window.__fp = st;
  async function hmac(msg) {
    const k = await crypto.subtle.importKey('raw', new TextEncoder().encode(KEY), {name:'HMAC', hash:'SHA-256'}, false, ['sign']);
    const sig = await crypto.subtle.sign('HMAC', k, new TextEncoder().encode(msg));
    return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2,'0')).join('');
  }
  const send = (d) => window.dispatchEvent(new CustomEvent('proctora-native', { detail: d }));
  async function beat() {
    const e = st.exam; if (!e) return;
    st.ts = Math.max(Date.now(), st.ts + 1);
    const checks = 'vm=0,disp=1,mir=0,tap=1,inp=1,proc=0';
    const sig = await hmac(`beat|${e.attempt_id}|${e.session}|${DEVICE}|${st.ts}|${st.locked?1:0}|${checks}`);
    await fetch(`/api/student/attempts/${e.attempt_id}/app-beat`, { method:'POST',
      headers: {'Content-Type':'application/json', Authorization:`Bearer ${e.token}`, 'X-FairPlay-Session': e.session},
      body: JSON.stringify({session:e.session, device_id:DEVICE, ts:st.ts, locked:st.locked, checks, signature:sig}) });
  }
  function setExam(m) {
    if (!m.session) return;
    st.exam = { attempt_id: m.attempt_id, token: m.token, session: m.session };
    if (!st.timer) st.timer = setInterval(beat, 2000);
    beat();
  }
  async function unlock() {
    const e = st.exam;
    if (e) {
      const r = await fetch(`/api/student/attempts/${e.attempt_id}`, { headers: {Authorization:`Bearer ${e.token}`, 'X-FairPlay-Session': e.session} });
      const j = await r.json().catch(() => ({}));
      if (!(r.status === 404 || r.status === 409 || j.status === 'submitted')) { send({type:'unlockPending', reason:'not_submitted'}); return; }
    }
    st.locked = false; st.unlockedAfterSubmit = true; beat(); send({type:'unlocked'});
  }
  window.webkit = { messageHandlers: { proctora: { postMessage: async (m) => {
    if (m.action === 'hello') send({type:'state', locked: st.locked, trusted: true, version: '0.5'});
    else if (m.action === 'attest') send({type:'attestation', nonce:m.nonce, device_id:DEVICE, signature: await hmac(`start|${m.nonce}|${DEVICE}`), version:'0.5', build:'dev'});
    else if (m.action === 'session') setExam(m);
    else if (m.action === 'lock') { if (m.session) setExam(m); st.locked = true; beat(); send({type:'locked'}); }
    else if (m.action === 'unlock') unlock();
  } } } };
})();
"""


def fake_app(device):
    return FAKE_APP.replace("%KEY%", json.dumps(KEY)).replace("%DEVICE%", json.dumps(device))


def login(pg, role, user, pw):
    pg.goto(f"{BASE}/{role}/login")
    pg.fill("#username", user)
    pg.fill("#password", pw)
    pg.get_by_role("button", name="Sign in").click()
    pg.wait_for_url(f"**/{role}")


def enter_code(S, title, code):
    S.get_by_text(title).first.wait_for()
    card = S.locator("article, li, section").filter(has_text=title).filter(
        has=S.get_by_role("button", name="Enter start code")).last
    card.get_by_role("button", name="Enter start code").click()
    S.wait_for_timeout(300)
    boxes = S.get_by_role("textbox", name=re.compile(r"Digit \d of 6"))
    for i, d in enumerate(code):
        boxes.nth(i).fill(d)
    S.get_by_role("button", name="Start exam", exact=True).click()


# ---- seed through the API (teacher) ----
_, t = api("POST", "/api/auth/login", {"username": "teacher", "password": "teacher123"})
TT = t["token"]
exam_body = {"title": "App Mode Quiz", "duration_min": 20, "questions": [
    {"type": "mcq", "prompt": "2+2?", "points": 1, "options": ["3", "4"], "correct_index": 1, "topics": ["arith"]},
    {"type": "short", "prompt": "Capital of France?", "points": 1, "accepted_answers": ["paris"], "topics": ["geo"]}]}
_, e = api("POST", "/api/exams", exam_body, TT)
_, e = api("POST", f"/api/exams/{e['id']}/open", None, TT)
code, exit_code = e["start_code"], e["invigilator_code"]
check("codes are 6 digits", len(code) == 6 and len(exit_code) == 6)

with sync_playwright() as p:
    b = p.chromium.launch()

    # 1. plain browser is refused
    P = b.new_context().new_page()
    login(P, "student", "bob", "bob123")
    enter_code(P, "App Mode Quiz", code)
    P.wait_for_timeout(1200)
    check("plain browser refused with app message", "FairPlay app" in P.locator("body").inner_text())
    P.screenshot(path="shots/app-01-browser-refused.png", full_page=True)

    # 2. alice in the (simulated) app
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    ctx.add_init_script(fake_app("mac-alice-0001"))
    S = ctx.new_page()
    login(S, "student", "alice", "alice123")
    enter_code(S, "App Mode Quiz", code)
    S.wait_for_url(re.compile(r".*/student/exam/a_[A-Za-z0-9_-]+"), timeout=10000)
    aid = S.url.rsplit("/", 1)[-1]
    S.wait_for_timeout(1500)
    check("app locked", S.evaluate("window.__fp.locked") is True)
    S.screenshot(path="shots/app-02-exam.png", full_page=True)
    S.get_by_text("4", exact=True).first.click()
    S.get_by_role("button", name=re.compile("^Next", re.I)).click()
    S.wait_for_timeout(300)
    S.locator("input").first.fill("Paris")
    S.wait_for_timeout(2500)

    # 3. teacher live room
    T = b.new_context(viewport={"width": 1280, "height": 900}).new_page()
    login(T, "teacher", "teacher", "teacher123")
    T.goto(f"{BASE}/teacher/exams/{e['id']}")
    T.wait_for_timeout(4000)
    _, live = api("GET", f"/api/exams/{e['id']}/live", None, TT)
    st = next(s for s in live["students"] if s["username"] == "alice")
    check("live: locked from signed beats", st["locked"] and st["app_online"] and st["client"] == "app")
    check("live: answered 2", st["answered"] == 2)
    seat = T.locator(".t-seat").filter(has_text="Alice").first
    check("live seat shows Writing", "writing" in (seat.get_attribute("class") or ""))
    check("live shows invigilator code", exit_code in T.locator("body").inner_text())
    T.screenshot(path="shots/app-03-live-room.png", full_page=True)

    # 4. helper: same account on another Mac running FairPlay
    hctx = b.new_context()
    hctx.add_init_script(fake_app("mac-helper-9999"))
    H = hctx.new_page()
    login(H, "student", "alice", "alice123")
    H.wait_for_timeout(1200)
    H.screenshot(path="shots/app-04a-helper-home.png", full_page=True)
    print("helper home buttons:", [x.inner_text() for x in H.get_by_role("button").all()][:12])
    btn = H.get_by_role("button", name=re.compile("Enter start code|Continue|Resume|Back to", re.I)).first
    btn.click()
    H.wait_for_timeout(500)
    if H.get_by_role("textbox", name=re.compile(r"Digit 1 of 6")).count():
        boxes = H.get_by_role("textbox", name=re.compile(r"Digit \d of 6"))
        for i, d in enumerate(code):
            boxes.nth(i).fill(d)
        H.get_by_role("button", name="Start exam", exact=True).click()
    H.wait_for_timeout(2000)
    htxt = H.locator("body").inner_text()
    print("helper sees:", htxt[:400].replace("\n", " | "))
    check("helper on another Mac refused", "another computer" in htxt or "somewhere else" in htxt)
    H.screenshot(path="shots/app-04-helper-refused.png", full_page=True)

    # 5. same login from a script: no session -> 409
    _, s2 = api("POST", "/api/auth/login", {"username": "alice", "password": "alice123"})
    st_get, _ = api("GET", f"/api/student/attempts/{aid}", None, s2["token"])
    st_put, _ = api("PUT", f"/api/student/attempts/{aid}/answers", {"answers": {}, "client_seq": 99}, s2["token"])
    check("other login cannot read/write the attempt", st_get == 409 and st_put == 409)

    # 6. invigilator code is checked by the server
    sess = S.evaluate("window.__fp.exam.session")
    hdr = {"X-FairPlay-Session": sess}
    st_bad, _ = api("POST", f"/api/student/attempts/{aid}/exit-code", {"code": "1234"}, s2["token"], hdr)
    check("old fixed code 1234 rejected", st_bad == 403)

    # 7. unlock is refused before hand-in
    S.evaluate("window.webkit.messageHandlers.proctora.postMessage({action:'unlock'})")
    S.wait_for_timeout(800)
    check("page-requested unlock refused before submit", S.evaluate("window.__fp.locked") is True)

    T.wait_for_timeout(3500)
    T.screenshot(path="shots/app-05-live-flags.png", full_page=True)
    flags = T.locator("body").inner_text()
    check("teacher sees the helper flagged", "Another login tried to use this exam" in flags or "another computer" in flags)

    # 8. submit -> unlocked after server confirms
    S.get_by_role("button", name=re.compile("Review", re.I)).first.click()
    S.wait_for_timeout(300)
    S.get_by_role("button", name=re.compile("^Submit", re.I)).last.click()
    S.wait_for_timeout(400)
    c = S.get_by_role("button", name=re.compile("^(Submit|Yes)", re.I))
    if "/student/done/" not in S.url and c.count():
        c.last.click()
    S.wait_for_url(re.compile(r".*/student/done/.+"), timeout=10000)
    S.wait_for_timeout(1500)
    check("unlocked after server-confirmed submit", S.evaluate("window.__fp.unlockedAfterSubmit") is True)
    S.screenshot(path="shots/app-06-done.png", full_page=True)

    # 9. results + release + student grades
    _, res = api("GET", f"/api/exams/{e['id']}/results", None, TT)
    a = next(x for x in res["attempts"] if x["username"] == "alice")
    check("results: alice 2/2", a["score"] == 2)
    api("POST", f"/api/exams/{e['id']}/close", None, TT)
    api("POST", f"/api/exams/{e['id']}/release", {"released": True}, TT)
    S.goto(f"{BASE}/student")
    S.wait_for_timeout(1500)
    S.screenshot(path="shots/app-07-student-home.png", full_page=True)
    T.goto(f"{BASE}/teacher/exams/{e['id']}")
    T.wait_for_timeout(1500)
    T.screenshot(path="shots/app-08-teacher-results.png", full_page=True)
    b.close()

print("ALL PASS" if ok_all else "SOME CHECKS FAILED")
sys.exit(0 if ok_all else 1)
