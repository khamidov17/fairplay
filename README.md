# FairPlay

Secure exams for universities: teacher web dashboard, student Mac app that locks the computer, and a scalable API.

```
server/   FastAPI API + serves the built web UI (teacher and student)
web/      React + TypeScript source of the web UI (npm run build -> server/static)
mac/      FairPlay.app: student exam app (WKWebView + lockdown)
e2e/      Playwright end-to-end test (teacher creates, 2 students take, results)
CONTRACT.md  API and native-bridge contract all parts follow
```

## Try it on this Mac
1. Double-click **Run Server.command** (first run installs Python packages, about a minute). Leave it open.
2. Teacher: open http://127.0.0.1:8000/teacher in a browser, sign in `teacher / teacher123`, create an exam, press Open, note the 4-digit start code.
3. Student: double-click **mac/Build and Run.command**. Sign in `alice / alice123`, enter the code, Start exam. The Mac locks until you submit.
   - First time only: turn on "FairPlay" in Privacy & Security > Accessibility (the app shows the steps).
   - Invigilator emergency exit while locked: **Ctrl-Opt-Cmd-E**, code **1234**. Dev builds also unlock by themselves after 180 minutes.
4. Watch the teacher's Live room, then Results.

Other students on the same Wi-Fi: start the server with `HOST=0.0.0.0` and point their app at it:
`defaults write ai.proctora.student ServerURL http://<this-mac-ip>:8000`
