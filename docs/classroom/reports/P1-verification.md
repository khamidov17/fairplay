# P1 verification (independent review of the P1 fixes)

Branch `p1-verify-snapshot`. This reviewer did not write the fixes. Scope: every finding in
`REVIEW.md` that the four P1 reports (`P1-accounts`, `P1-grading`, `P1-aiide`, `P1-authoring`)
claim as fixed, plus the exam-surface allowlist and the course-teacher visibility change.

**Method**
- Read each fix (`git log -p 77ae5c5..HEAD -- classroom/`) next to the original finding and the per-area review notes.
- Looked for other paths to the same data: other views, API v2, JSON widgets, templates, websocket payloads, the data export and the legacy editors. Checked them for different roles (anonymous, student in the class, student elsewhere, a friend's account, course admin, site teacher) and at different times (before the start, running, after the end, after Finalize).
- Wrote one test per remaining path. They are in `classroom/judge/views/tests/test_p1_verification.py` and marked `@expectedFailure`. Run them with `P1_SHOW_GAPS=1` to see each one fail. All 14 fail on their assertion, not on an error.
- Checked the most important paths on a local dev server (port 8123, DB `classroom_verify`) with curl:
  - Anonymous `GET /user/<student>/solved` during a running exam showed `vmcq … 10 / 10` for the student's hidden exam answer.
  - A student with no shared course got `Real Person (ab123)` from `/widgets/martor/search-user?username=a`.
  - The workspace pages of all five question types were crawled as a locked student (see the allowlist section).
- Full suite: `bash classroom/scripts/dev-test.sh judge` (serial) ran **1841 tests: OK, 14 expected failures**. That is the 1826 from before plus the 15 new tests.

## Verdicts

| Finding | Verdict | Remaining path / note |
|---|---|---|
| CA-1 | **INCOMPLETE** | A course admin uploads the same roster twice. The first upload links the account to the course, and the second rewrites its e-mail and re-activates it. |
| CA-2 | FIXED | |
| CA-3 | FIXED | |
| CA-4 | **INCOMPLETE** | `/widgets/martor/search-user` (rich-editor @mention) is not scoped. |
| CA-5 | FIXED | |
| CA-6 | FIXED | |
| CA-7 | **INCOMPLETE** (conditional) | A social-auth sign-up (Google/GitHub/Facebook) can take a NetID-shaped username. SSO then binds to it, because the account has no usable password. |
| CA-8 | FIXED | |
| CA-9 | FIXED | The production `local_settings.py` still has to be checked. |
| CA-10 | FIXED in the repo | The GitLab protected-variable and runner settings, and the image tags, still need the OJ team. |
| CA-11 | FIXED | |
| G-1 | **INCOMPLETE** | Four paths remain: `/user/<u>/solved` (best scores and the PP breakdown), `/problems/?hide_solved=1`, the user data export, and `/stats/language/data/ac/`. |
| G-2 | FIXED | |
| G-3 | FIXED | |
| G-4 / AI-1 | FIXED | |
| G-5 | FIXED | |
| G-6 | FIXED | |
| G-7 | **INCOMPLETE** | The same problem in another contest (a virtual join of an old contest, or a running homework) gives the verdict at once. |
| G-8, G-9, G-10, G-11, NaN scores | FIXED | |
| AUTH-01 | FIXED | |
| AUTH-02 | FIXED | |
| AUTH-03 | FIXED | Production nginx should deny `/media/problem_import_test_data/` until the migration has run. |
| AUTH-04 | FIXED | |
| AUTH-05 | **INCOMPLETE** | The legacy MCQ and fill-in-the-blank editors still rewrite a full-markup description. |
| AUTH-06 | FIXED | The residual risk is the one in the authoring report (a blocklist). |
| AUTH-07 | FIXED | |
| AUTH-08 / AI-7 | FIXED | |
| AUTH-09 | FIXED | |
| AUTH-10 | **INCOMPLETE** | The legacy MCQ and fill-in-the-blank editors attach any course. |
| AUTH-11, AUTH-12 | FIXED | |
| AI-2 | FIXED | Whether the judge writes stderr into `extended_feedback` still needs a runtime check. |
| AI-3, AI-4, AI-5, AI-6, AI-8 | FIXED | |
| SEB-1 | **INCOMPLETE** | The `.seb` download gate is fixed. Any instructor still gets the site-default preset by ticking "use default" on a draft of their own. The Config Key limitation remains, and the Browser Exam Key option that would close it is off by default. |
| SEB-2 | FIXED | |
| Exam-surface allowlist | **INCOMPLETE** | Leave and then rejoin turns the lock off for a non-SEB exam. |
| Course-teacher visibility | FIXED | Behaviour change: class instructors no longer see their class's unpublished assignments. |

No legitimate flow I tried broke (REGRESSION: none). The behaviour changes worth a product decision are listed at the end.

## Remaining paths

### CA-1: two uploads rewrite any non-staff account
`import_may_update_existing_user` only allows an update for a plain member of the target course. But `process_user_csv` adds every row's existing account to the course (`_add_user_to_org`) even when it skips the update. So a course admin uploads `victim,A,B,attacker@example.com` twice with "Update existing users":
- The first upload links the victim to the course.
- The second one rewrites the victim's e-mail and sets `is_active=True`.

The victim can be any non-staff account on the site: a student of another course, or a teacher who manages nothing in this course. A password reset then takes any such account that has a usable password and no 2FA.
- **Test:** `CA1TwoStepImportTestCase.test_second_upload_rewrites_email_of_an_outside_account`.
- **Fix idea:** only update accounts that were members *before* this upload, or never change the e-mail of an account that has a usable password.

### CA-4: rich-editor mention search
`martor.views.markdown_search_user` (`/widgets/martor/search-user?username=`) needs only a login. It does not use `scoped_user_queryset`:
- a one-character term works;
- `is_unlisted` is ignored;
- it searches by real name and returns `name` and `username` for every active user on the site.

On the dev server, a student who shares no course with anyone got `Real Person (ab123)`.
- **Test:** `CA4MentionSearchTestCase.test_martor_mention_search_is_scoped`.
- **Fix:** route it through `scoped_user_queryset`.

### CA-7: social-auth accounts look like SSO accounts (only if a social backend has keys)
`netid_may_bind_existing_user` accepts any account without a usable password. Social-auth sign-ups also have none, and `judge.social_auth.UsernameForm` does not check `SSO_RESERVED_USERNAME_REGEX`. For example, the Google username suggestion for `ab123@gmail.com` is `ab123`.

So whoever signs up through Google as `ab123` before the student's first NetID login receives that student's SSO logins. They also keep access to the account through Google.
- **Tests:**
  - `CA7SocialSignupTestCase.test_social_signup_cannot_pick_a_netid_shaped_username`
  - `CA7SocialSignupTestCase.test_sso_does_not_bind_to_a_social_auth_account`
- **Fix:** apply the reserved regex in `UsernameForm`, and refuse to bind when a `UserSocialAuth` row exists.
- This is moot if production has no social-auth keys. Ask the OJ team.

### G-1: four more places read the raw verdict or points
The fixture is an exam MCQ on a public problem, results hidden and not finalized.

1. **`/user/<u>/solved`** (`UserProblemsPage`, `points__gt=0`) and its PP breakdown (`performance_points.get_pp_breakdown`, raw SQL) list the problem with the hidden score.
   - The page is public. A student can check their own answer from a logged-out second device during the exam, because the lock is per account and logging out removes it.
   - Tests: `G1RemainingPathsTestCase.test_user_solved_page_skips_hidden_exam_scores` and `test_user_pp_breakdown_skips_hidden_exam_scores`.
   - Confirmed on the dev server: `10 / 10` was shown to an anonymous visitor.
2. **`/problems/?hide_solved=1`** (`ProblemList.get_normal_queryset`, raw `result='AC'`) removes the problem when the hidden answer is correct.
   - Test: `test_problem_list_hide_solved_ignores_hidden_verdict`. It runs after the end and before Finalize, when the lock is off.
3. **User data export** (`tasks/user.prepare_user_data`, only when `DMOJ_USER_DATA_DOWNLOAD` is on): the `submission_results` filter and `info.json` use the real `result` and `case_points`.
   - Test: `G1UserDataExportTestCase.test_data_export_masks_hidden_exam_results`.
4. **`/stats/language/data/ac/`** (and `ac_rate`): this is a site-wide, anonymous AC count per language, and hidden answers count under "Answer".
   - It is low severity because it is noisy on a busy site, but in a quiet window it answers "was my last answer right?".
   - Test: `test_language_ac_stats_skip_hidden`.

**Fix:** use `results_hidden_q()` / `visible_results()` in each of these, as was done for `Profile.calculate_points`.

### G-7: the same problem in another contest
`_submission_blocked_response` only blocks submits *outside* any contest (`contest_problem is None`). If the problem is also in another contest whose results are shown, a submit there is graded and shown at once. Two cases:
- the exam reuses a library problem from last term's (finalized) quiz, and virtual participation is on by default in the editor;
- a homework that is running at the same time uses the problem.

A friend's account, or the student after Leave, gets an answer checker that way.
- **Test:** `G7RemainingPathsTestCase.test_virtual_participation_in_old_contest_with_same_problem`.
- **Fix:** apply `problem_in_active_hidden_exam` to every submit that is not inside that exam, not only out-of-contest ones. Or hide results for that problem in the other contest while the exam is open.

Related, by design in the fix: practice opens again at the exam's end, before Finalize. A student can then learn their own hidden verdict before it is released. They cannot change the answer by then, but it defeats "results hidden until release".

### AUTH-05 and AUTH-10: the legacy MCQ and fill-in-the-blank editors
`/problems/new/mcq`, `/problem/<code>/mcq` and the `fill-blank` twins use a `ModelForm` with `description`, `organizations` and `classes`:
- **AUTH-10:** the `organizations` queryset is every course, so any user with `create_private_problem` can attach any course.
- **AUTH-05:** an editor without `problem_full_markup` can rewrite the description of a full-markup problem. It is rendered unsanitized, so this is stored XSS.

The fixes only covered create_v2 and the Excel import. The existing test `test_mcq.MCQAuthoringViewTestCase.test_create_with_organization_is_org_private` even attaches a course the creator does not teach.
- **Tests:**
  - `AuthoringLegacyEditorTestCase.test_legacy_mcq_editor_cannot_attach_a_course_not_taught`
  - `AuthoringLegacyEditorTestCase.test_legacy_mcq_editor_keeps_full_markup_description`
- The fill-in-the-blank editor (`views/fill_blank.py`) has the same form and no check. I read the code but did not write a separate test for it.
- **Fix:** reuse `assignable_organizations()` and `_check_full_markup_text()`, or retire the legacy editors.

### SEB-1: the preset is one checkbox away
`seb_preset_downloadable_by` needs `contest_seb_requirement` and editing a contest that uses the preset. Every Instructor has that permission, and any instructor can tick "use the default configuration" on an unpublished draft of their own. They can then download the site-default `.seb`, which is shared by every exam that uses the default.

The same is true for a student who sat one exam using the default: they keep the file, and with it the Config Key for every later exam that uses the default. The fix documents this; the Browser Exam Key option stays off by default for macOS.
- **Test:** `SEB1PresetAdoptionTestCase.test_instructor_cannot_fetch_site_default_by_adopting_it`.
- The real fix is operational: encrypt the default `.seb` with a settings password, and use one preset per exam or the Browser Exam Key where possible.

### Exam-surface allowlist: Leave turns it off
`ContestLeave` is allowed during the lock (any route on this contest is), and it clears `current_contest`. The lock and, for non-SEB exams, everything else are then off. Rejoining the same LIVE participation works, so a student can leave, use `/ide/`, other problems, tickets or comments, and come back.
- **Test:** `ExamLockLeaveTestCase.test_leaving_a_running_locked_exam_keeps_the_site_locked`.
- The P1-aiide report calls this a product question. For FairPlay it means OJ's lock is advisory unless Leave is disabled while a locked exam is running, or the lock is keyed on any not-ended LIVE participation rather than on `current_contest`.

**Runtime crawl.** I crawled the contest page and the workspace of MCQ, short-answer, file-upload, code and fill-blank problems as a locked student. Every endpoint the pages reference worked (submit, draft, workspace run, submissions, status, upload). The only same-origin URLs that were redirected and are harmless are:
- `/favicon-*.png`, `/apple-touch-icon-*.png` and `/manifest.json`, which get a 302 to the contest page, so the browser fetches HTML as an icon on every page;
- comment and vote endpoints and the profile link.

Consider adding the icons and manifest to `_EXAM_LOCK_ALWAYS`, or serving them from static.

## Checked and found fixed (beyond the engineers' own tests)
- **Websocket:**
  - `grading-end` carries no verdict when hidden.
  - The `submissions` channel carries only status.
  - The bridge test-case events carry only the position.
- **API v2:**
  - `/api/v2/contest/<key>` uses `can_see_full_scoreboard`.
  - `/api/v2/users?organization=abc` returns 400, not 500.
  - The user list has no real names.
- **Course-teacher visibility:** `get_visible_contests` and `access_check` agree. The class page, calendar, iCal, select2 and API use them. Problems of an unpublished assignment stay private (`Problem.is_accessible_by`).
- **SEB-2:** a spectator gets only a SPECTATE participation. Their submissions don't count toward the ranking.
- **AI-2:** the submission page and the testcases widget use `submission_output_hidden_for`, and the API has no output field.

## Behaviour changes to confirm (not regressions in the code, but they change legitimate use)
1. **Practice and test downloads close site-wide.**
   - G-7 closes practice submits on a problem while *any* exam that hides its results and uses it is upcoming or running.
   - AUTH-02 and AUTH-07 close test-case and tester downloads while *any* assignment using the problem is open.

   Both apply site-wide. One course scheduling an exam a month ahead freezes practice on a shared public problem for every student on the site.
2. **The lock is on by default.** New assignments default to the Exam format, so the lock is on for most of them, homework included, unless the teacher picks Off.
3. **Class instructors lose the preview.** Class instructors who are not authors no longer see their class's unpublished assignments. Co-teachers must be added as curators.
4. **The SEB file window.** The `.seb` can be downloaded only from 30 minutes before the start.

## Tests
- New file: `classroom/judge/views/tests/test_p1_verification.py`, with 15 tests: 14 `@expectedFailure` gaps and 1 precondition.
- `P1_SHOW_GAPS=1` runs the gaps as plain tests.
- When a gap is fixed, its test passes, unittest reports an "unexpected success", and the marker must be removed.
- Full suite on this branch: **1841 tests, OK (expected failures=14)**, in 227 s. `flake8` is clean on the new file.
