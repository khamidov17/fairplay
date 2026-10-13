# DKU OJ code review: notes for FairPlay (2026-10-13)

**Source:** `~/Downloads/dkuoj-main` on Mister's Mac, a snapshot dated 2026-09-28. It has no git history. Upstream is gitlab.oit.duke.edu/dku-oj/dkuoj.

**Method:**
- Compared against upstream DMOJ (github.com/DMOJ/online-judge master) to find the code DKU wrote.
- Four reviewers each read one area. A fifth, independent reviewer re-checked every Critical and High claim against the code (15 confirmed, 5 partly true, none refuted).
- Nothing was run: no database, no judge, no browser. Every finding comes from reading the code, so each one needs a reproduction on a staging copy before it goes to the DKU OJ team as fact.
- The per-area reports are in `claude/dku-oj-code-review-details-2026-10-13.md`.

**Size:** about 86k lines of Python. About 70k of the new lines are DKU's own (20k of those are tests). 108 upstream files are changed.

---

## 1. What the code is

- **Stack:** Django 4.2, Jinja2 templates, MySQL, Redis and Celery, the DMOJ judge bridge, an event daemon over websocket.
- **Files:** S3-compatible primary storage (Garage) with a Box SFTP backup mirror.
- **AI:** Azure OpenAI or LiteLLM.
- **License:** AGPL-3.0.

**Data model:**
- `Organization` = **course**. `Class` = **section** inside a course.
- `Contest` = **assignment**. Quiz, exam and homework are tags on it.
- `Problem` = **question**. `Problem.submission_type` picks the question type.
- `Submission` = one answer. Inside an assignment it is linked through `ContestSubmission` and `ContestParticipation`.

**Question types** (`judge/question_types/registry.py`):
- `C` code, `M` choice (single, multiple, true/false), `B` fill in the blank, `S` short answer (AI-graded), `F` file upload (manual).
- Answers to non-code questions are stored as JSON in `SubmissionSource.source`.

**Code DKU added on top of DMOJ:**
- SEB lockdown (`judge/utils/seb.py`, `models/seb.py`, `ContestMiddleware`).
- New question types and the grade desk (`views/grade.py`, `templates/contest/grade.html`, Release All, Finalize).
- AI hints, AI grading and calibration (`judge/ai_hints.py`, `judge/llm/`, `utils/contest_ai.py`, `utils/calibration.py`).
- Analytics with an anomaly score (`utils/contest_analytics.py`).
- `/ide/` and the split-pane workspace (`resources/workspace.js`).
- Problem create v2 (`views/problem_create_v2.py`) and Excel import (`views/problem_import.py`).
- Course and class management, join requests, instructor verification, CSV roster import.
- PDF and Word export (pandoc + lualatex), and the resilient storage layer.

**Roles:**
- **Site teacher:** the `Instructors` group, approved by a superuser.
- **Course admin:** `org.admins`.
- **Class instructor:** `class.admins`.
- **Course teacher:** a broad read-only role, see the risk in section 5.
- **Student:** `class.members`.

**Login:** Duke NetID through an external SSO host (`/login_netid/?token=`), or a local password. Superusers must use the local login.

---

## 2. How the SEB gate works (the part FairPlay replaces)

**The gate:**
- `ContestMiddleware` (`judge/middleware.py:136-174`) runs on every request from a user whose `profile.current_contest` is an assignment with `seb_required`.
- `current_contest` is stored per user, not per browser. So once a student joins, every device logged in as that student is gated.
- A request passes if `X-SafeExamBrowser-RequestHash` or `X-SafeExamBrowser-ConfigKeyHash` equals `sha256(absolute URL + key)` for one of the pasted keys (`request_matches_seb`, `utils/seb.py:121-134`). Either header alone is enough.
- `ContestJoin` checks the same thing before joining.
- A few paths are exempt: logout, the quit URL, the `.seb` download, the launch-token route, STATIC and MEDIA (`_seb_block_exempt`).

**Launch:**
- `SEBLaunchToken` (`/contest/<key>/seb-launch-token`) creates a `sebs://` link.
- The link points to `SEBLaunchConfig` (`/seb/launch/<token>/`). The token is a Django `TimestampSigner` signature over `{contest id, profile id}`, valid for 30 minutes (`DMOJ_SEB_LAUNCH_TOKEN_EXPIRY_MINUTES`).
- `SEBLaunchConfig` serves the `.seb` file without a login. The token isn't tied to the user who opens the link.

**Who skips the gate:**
- `user_bypasses_seb` (`contest.py:801`): `edit_all_contest`, the assignment's editors, testers and spectators, and admins of the contest's courses (for course-private contests only).

**Presets:**
- An admin uploads one blessed `.seb` file with its key hashes (`SEBConfigPreset`). Instructors tick "use default".
- Config Key hashes are always required, because the macOS SEB build sends only that header.

---

## 3. Where FairPlay plugs in (integration plan)

1. **Gate.**
   - Add `Contest.fairplay_required` and a FairPlay branch in `ContestMiddleware` and `ContestJoin._check_seb`, in the same places as `seb_required`.
   - Reuse `_seb_block_exempt` and add a FairPlay launch route. Read the `_seb_launch_prefix` comment first: a root-ish exempt prefix switches the lock off for the whole site.
2. **Launch token.**
   - Copy the `SEBLaunchToken` / `SEBLaunchConfig` pattern. OJ issues a short-lived signed token for "this student, this assignment, until this time".
   - FairPlay calls an OJ verify endpoint once, then sends a per-request proof. Use a header with an HMAC over a session key, not a static hash like SEB's.
   - Bind the token to the user, unlike the SEB token.
   - Fix CA-9 first if the token is signed with `SECRET_KEY`.
3. **Integrity events.**
   - Short term: put a "FairPlay report" link on the analytics page (`templates/contest/analytics.html`).
   - Later: an endpoint that receives FairPlay events, with the flags shown in the anomaly table.
   - Don't use OJ's `progress-trace` beacon as evidence. It is client-reported and anyone can fake it.
4. **Read API** (nothing exists for classes or final grades today):
   - `courses?mine=1`
   - `course/<id>/classes`
   - `class/<id>/roster`
   - `class/<id>/assignments` (with `seb_required` and `fairplay_required`)
   - `contest/<key>/grades` (release-aware: `student_display_points()` and `results_visible_to()`, no result filter)

   Use scoped, read-only tokens. Today a Bearer token (`APIMiddleware`) acts as the full account on every non-admin page.
5. **URL allowlist during an exam.**
   - OJ has none. While a student is in an assignment, `/ide/`, other problems, comments, tickets and user pages all still open.
   - Either FairPlay's window enforces an allowlist, or OJ adds one for `request.in_contest`: the contest, its problems, the workspace JSON, the contest's own submissions, and static files.
6. **Holes no lock can close.** A locked client does nothing against leaks a student can reach from a second device or a friend's account:
   - AUTH-01: statements exported before the start.
   - G-1: hidden results readable through filters.
   - AUTH-02: test-case download.
   - G-7: public exam problems solvable as practice.
   - AUTH-03: public import ZIPs.
   - AI-2: Run output that should be hidden.

   These must be fixed on the OJ side before a "Require FairPlay" exam means anything.

---

## 4. Findings (verified list, most severe first)

Severity is the verifier's corrected value. "Confirmed" means the code path was read end to end by two reviewers. Nothing was reproduced on a running site.

| ID | Sev | What is wrong | Where |
|---|---|---|---|
| CA-1 | **Critical** | A class instructor's CSV roster import overwrites the email of any existing account matched by username, superusers included, and re-activates disabled accounts. Changing an admin's email and then resetting the password takes the account, unless that admin has 2FA. | `utils/import_export.py:509-536`, called with `update_existing=True` from `views/organization.py:2027`, `:2176` |
| G-1 | High | During an exam, a student can learn whether a hidden answer is correct. The submission list `?status=AC` filter, the `?results` counts, the API `?result=` filter (which also works on other students) and the per-problem AC rate all use the real verdict. Exams score the latest answer, so this works as an answer checker. | `views/submission.py:610`, `:730`; `views/api/api_v2.py:581`; `utils/problems.py:87` |
| G-2 | High | "Finalize results" (and any rejudge) re-grades the latest answers without calling the AI. AI-graded short answers become 0, and AI fill-blank falls back to exact matching. Every score the teacher didn't edit by hand is lost. | `tasks/contest.py:65`; `question_types/short_answer.py`, `fill_blank.py` |
| G-3 | High | Saving an MCQ or true/false question in create v2, even for a typo, deletes and recreates its options with new IDs. Stored answers point to the old IDs, so the next rejudge, Finalize or auto-grade marks everyone wrong. | `views/problem_create_v2.py:426`, `:1509` |
| AUTH-01 | High | Any user who can see an assignment, including enrolled students before it starts (or anyone, for public ones), can export every problem statement as PDF or Word. | `views/contests.py:3046`, `:3656` (only `is_accessible_by`) |
| AUTH-04 | High | Stored XSS. Any instructor can create a problem type whose name contains `</script>`. The editor pages put it into a `<script>` block with `|safe`, so it runs for every instructor or admin who opens `/problems/new/`. | `views/problem_create_v2.py:174`, `:203`; `templates/problem/create_v2.html` |
| CA-2 | High | Any approved site teacher can delete any class in any course: the Instructors group has `delete_class`, and that permission alone is accepted. | `views/organization.py:2189`; `models/notification.py:24-40` |
| CA-3 | High | "Add from library" accepts any contest on the site. An instructor can link another course's exam to their class, which gives them and their students access. If the exam had no classes before, it becomes class-only and its own students lose access. | `forms.py:596`; `models/contest.py:603-606`, `721-738` |
| CA-7 | High / Med | NetID SSO matches users by username only. With local registration open (the default), someone can register a student's NetID as a local account before the student's first login. The SSO token check also uses `verify=False`. | `views/user.py:187`; `settings.py:642` |
| SEB-1 | High | The gate accepts the Config Key header alone, and the `.seb` file is downloadable by any user who can see the assignment, before it starts. Any instructor can also download every preset, including the site default. If the Config Key can be computed from the `.seb` file (a property of SEB itself, not checked here), an ordinary browser that adds the header passes. | `utils/seb.py:121-134`; `views/contests.py:4319` |
| AUTH-05 | Med-High | Create v2 lets an editor without the full-markup permission rewrite the description of a full-markup problem, which is rendered without sanitizing. The admin blocks this; create v2 doesn't. | `views/problem_create_v2.py` |
| AUTH-03 | High (likely) | Test-data ZIPs from the Excel import are kept in public MEDIA (`media/problem_import_test_data/`) under guessable names and never deleted. With DEBUG on, Django serves them without login, and nginx usually does too. | `views/problem_import.py` |
| CA-4 | Medium | Anyone, logged in or not, can list every user's real name next to their NetID through the select2 endpoints. An empty search term returns everyone, and `/judge-select2/profile/` ignores `is_unlisted`. | `views/select2.py` |
| CA-5 | Medium | Impersonation leaves no record: `DISABLE_LOGGING` is True and access logging is skipped. Teachers with the impersonate permissions can impersonate students. | `dmoj/settings.py:348-352`; `judge/impersonation.py` |
| CA-6 | Medium | `/api/v2/participations` and `/api/v2/user/<u>` show final scores of ended contests even when the scoreboard is "Hidden permanently". This is limited to people who can already see the contest. | `views/api/api_v2.py` |
| CA-9 | Medium | `DEBUG = True` is the shipped default, and it turns off the refusal to run with the well-known default `SECRET_KEY` and event-daemon key. No deploy file sets DEBUG, and the live site runs with DEBUG on. If `local_settings.py` doesn't set a real key, sessions and CSRF tokens can be forged. Ask the OJ team. | `dmoj/settings.py:29-34`, `:750` |
| AI-1 / G-4 | Medium | The AI grade's total is built from the rubric rows the model returned, not the configured table. One row returned at full marks gives 100%. A student can try to cause this with prompt injection in their answer. | `question_types/ai_grading.py:60-104`; `short_answer.py:127`; `fill_blank.py:223`; `utils/contest_ai.py:280` |
| AI-2 | Medium | "Hide IDE / Run-Test output" is enforced only in `/ide/status`. `/submission/<id>` and the testcases widget still print the stdout of Run. | `templates/submission/status-testcases.html:173` |
| AUTH-02 | Medium | Where the teacher enabled `view_test_cases` (off by default), a student can download the first failing hidden test, input and expected output, during an exam. | `views/problem_data.py:364` |
| AI-4 | Medium | AI Hint is hidden only after AC and never checks `results_visible_to`. Whether it appears reveals a hidden verdict. | `views/submission.py:283`; `templates/problem/workspace.html:529` |
| G-5 | Medium | Anyone with `add_organization` (the whole Instructors group) counts as a teacher for AI grading, and can re-run AI scoring on another course's submissions. | `views/submission.py` (AI grade view) |
| G-6 / G-7 | Medium | When source visibility is "solved", a student with AC can read classmates' answers and files. A public problem used in an exam can be practised outside the exam, with results shown at once. | `views/submission.py`, `views/problem.py` |
| AI-7 / AUTH-08 | Medium | A student's short answer is rendered with the problem's markdown style, which has no sanitizer on full-markup problems. This is possible stored XSS against the teacher's grade desk; it needs a runtime check. | `views/problem.py:310` |
| AUTH-06 | Medium (needs runtime) | Possible LaTeX injection and file reads in the pandoc/lualatex export, for example `\input` of `local_settings.py`. | contest export |
| CA-8 | Medium | A class instructor rejecting an already-approved request removes that user from the whole course and every class in it. | `views/organization.py` |
| CA-10 | Medium | CI gives every branch pipeline a GitLab personal access token and an LLM key, inside an unpinned `pr-agent:latest` image. | `.gitlab-ci.yml` |
| AI-3 | Low-Med | A contest can't turn AI hints off: the problem flag or the contest flag turns them on, and create v2 ticks the problem flag by default. | `judge/ai_hints.py:139` |
| AI-5 / AI-6 | Low-Med | The analytics percentile counts ties as "≤", so with 20 students and 2 outliers the 18 clean ones show "90th" in red. Students with no submissions top the anomaly list. | `utils/contest_analytics.py:560` |
| SEB-2 | Low-Med | Students can't make themselves tester or spectator. A student wrongly added as spectator can still join the exam live and skip SEB. | `models/contest.py:638-643`, `:801` |
| G-8 | Low | Opening a file upload's status page marks it Aborted with 0 points after 40 seconds, which drops it from the grading queue. | file upload status view |
| AUTH-07 | Low | Where `view_tester` is on, the tester file downloads without a problem-access check (other courses, before the exam). | `views/problem_data.py` |
| CA-11 | Low | Any logged-in user can apply as class instructor. Approval silently grants `is_staff`. | `views/organization.py` |
| others | Low | `is_public` bypass with a non-existent organization id (AUTH-09); instructors can attach problems to any course (AUTH-10); a zip bomb in a DOCX embedded in the Excel import (AUTH-11); the legacy `import_users_csv_old` defaults to password `123456` (AUTH-12); the old `grade-file` endpoint skips Release All (G-9); code with an AI rubric shows students a different score from the ranking (G-10); ECOO counts IE/CE as the latest submission (G-11); non-atomic rate limits in the IDE and AI grading, and a hint "daily quota" that is really 5 per 5 minutes (AI-8). | see details doc |

**Checked and found fine:**
- Zip-slip in test-data ZIPs, the XLSX size and compression limits, and XXE (stdlib ElementTree doesn't resolve external entities).
- Storage credentials and the private bucket.
- Draft ownership checks.
- File-upload storage paths and type checks.
- Answer keys are never sent to students.
- The grade desk is limited to that assignment's editors.
- No submission after the student's window ends.
- No open redirects; the only CSRF exemption is the upstream Mailgun webhook.
- The IDE requires login, and students can't open analytics.

---

## 5. Design risks (not bugs, but they matter for exams)

- **"Course teacher" is very broad.** It covers course admins, any site teacher who joins the course, and admins of any class in it. All of them can open every course-private exam in the course before it is published (`contest.py:586`), and they skip the assignment rules.
- **Class admins can add anyone** as a class admin or member, with no verification.
- **Course member lists are public.** `/organization/<id>/users` is anonymous and lists members, and DKU uses organizations as courses.
- **Analytics is slow at scale.** It is computed live on every page view, with an O(n²) similarity check per problem.

---

## 6. Suggested order for the DKU OJ team

1. **Now.**
   - Fix CA-1.
   - Confirm the production `SECRET_KEY` and set `DEBUG = False` (CA-9).
   - Rotate the CI tokens (CA-10).
   - Fix AUTH-04.
2. **Before the next exam.**
   - Fix G-1, G-2, G-3 and AUTH-01.
   - Gate the select2 endpoints (CA-4).
   - Fix CA-2 and CA-3.
3. **Before "Require FairPlay".**
   - Fix AI-2, AI-3, AI-4, AUTH-02, AUTH-03 and G-7.
   - Add the URL allowlist and scoped API tokens.
4. **Then build the FairPlay gate** (section 3).

**Open questions for the OJ team:**
- What is in `local_settings.py` on production?
- Is local registration open on production?
- Is the default `.seb` password-encrypted?
- Do teachers turn on `view_test_cases` and `view_tester`?
- How do problem codes get assigned?
