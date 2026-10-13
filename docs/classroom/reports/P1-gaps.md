# P1 gaps: closing the paths left open after P1

Follows `P1-verification.md`. That review found 9 findings still incomplete, with 14 failing tests. All 9 are closed here. An independent reviewer (who did not write these fixes) then found 5 more paths; those are closed too, and the reviewer re-checked them.

**Result:** full suite `bash classroom/scripts/dev-test.sh judge --parallel 2` ran **1932 tests, OK** (0 expected failures). `flake8` is clean on every changed file. Every new test was run against the code before its fix and failed there.

## What changed, per finding

| Finding | Fix | Where |
|---|---|---|
| G-1 `/user/<u>/solved` | Excludes `results_hidden_q()`, like `Profile.calculate_points`. | `judge/views/user.py` |
| G-1 PP breakdown | The raw SQL skips the user's hidden submissions (passed as an id list). | `judge/performance_points.py` |
| G-1 `/problems/?hide_solved=1` | A hidden AC no longer removes the problem. | `judge/views/problem.py` |
| G-1 language AC count and AC rate | Hidden verdicts are not counted. | `judge/views/stats.py` |
| G-1 status stats (found in review) | Same, for `/stats/language/data/status/`. | `judge/views/stats.py` |
| G-1 `has_any_solves` (found in review) | A hidden AC no longer unlocks commenting. | `judge/models/profile.py` |
| G-1 data export | The result filter skips hidden rows. The archive keeps the answer but blanks its verdict, points, time and memory until release. | `judge/tasks/user.py` |
| G-7 | A problem in an open hidden-results exam can only be submitted where the verdict is masked too. This closes virtual joins of old contests and concurrent homework. Editors can still test. New helper `contest_problem_hides_results`. | `judge/views/problem.py`, `judge/utils/result_visibility.py` |
| G-7 harvest (found in review) | Auto-submit applies the same rule; such drafts stay drafts. | `judge/utils/assignment_harvest.py` |
| CA-1 | A roster import only edits SSO-only accounts (no usable password, no social login): names, e-mail and the active flag. Any other account is only linked. "Members before this upload" could not work, because the first upload is what makes them members. | `judge/utils/import_export.py` |
| CA-1 chain (found in review) | `associate_by_email` in the social pipeline never links a Google/GitHub/Facebook login to an SSO-only account. A roster e-mail is unverified. | `judge/social_auth.py`, `dmoj/settings.py` |
| CA-4 | The @mention search uses `scoped_user_queryset`, and real names only for those who may see them. Capped at 20 rows. | `martor/views.py` |
| CA-7 | The social sign-up form rejects NetID-shaped usernames. SSO never binds to an account that has a social login. The rule is shared with local registration in `judge/utils/sso_usernames.py`. | `judge/social_auth.py`, `judge/views/user.py`, `judge/views/register.py` |
| AUTH-05 / AUTH-10 | `ScopedProblemFormMixin` is used by both the legacy MCQ and fill-in-the-blank editors. Courses are limited to `assignable_organizations` and classes to visible ones (plus whatever the problem already has). Without `problem_full_markup`, a full-markup statement is read-only. | `judge/utils/problem_create.py`, `judge/views/mcq.py`, `judge/views/fill_blank.py` |
| SEB-1 | A *shared* preset (the site default, or one also used by exams this user does not edit) goes only to preset managers (`change_sebconfigpreset`) and to students under the roster-and-window rule. Editing a contest that uses it is not enough. This applies on both routes: the preset preview and the contest's own `.seb`/launch token. The editor hides the "Preview .seb" link when it would be refused. | `judge/utils/seb.py`, `judge/views/contests.py`, `templates/contest/create.html` |
| Exam lock, Leave | Leave still hands in (harvest) and still sends SEB students to the quit URL. While a running assignment locks the site, it no longer takes the student out of it, so the lock holds until the window ends; `update_contest` then clears it. The student sees a short message. Site icons and `manifest.json` now pass the lock. | `judge/views/contests.py`, `judge/middleware.py` |

For the lock, a first attempt keyed it on any open LIVE participation. It was dropped: it locked students who held a participation without being in the exam, and it broke 15 existing tests.

## Tests
- `judge/views/tests/test_p1_verification.py`: the 14 gap tests are now ordinary tests (markers removed).
- New `judge/views/tests/test_p1_gap_fixes.py` (32 tests). It pins the legitimate flows around each fix:
  - results come back after Finalize, and practice AC still counts;
  - homework opens again after the exam, and editors can still test;
  - SSO-only accounts still get roster updates;
  - teachers still find students, with real names;
  - teachers can attach their own course and keep existing ones;
  - full-markup editors can still edit;
  - the lock lifts at the end and does not apply to homework.
  It also has tests for the review findings.
- Existing tests changed because they encoded the old behaviour:
  - `test_mcq` and `test_fill_blank`: the creator now teaches the course it attaches.
  - `test_import_export`: the updated account is SSO-only.
  - `test_martor_search_user`: the searcher shares a course.
  - `test_seb`: the preview test uses a non-default preset, plus a new default-denied test; after Leave from a running SEB exam the student stays in.

## Behaviour changes to confirm (product decisions)
1. **Finishing early keeps the student locked until the window ends.** This covers most new assignments, which default to the Exam format with the lock on. With a multi-day window and no time limit, that is days. It also blocks joining an overlapping assignment. Under SEB, a normal browser shows "SEB required" until the end. If that is too strict, use per-student time limits for long windows, or decide that Leave may unlock once all answers are handed in.
2. **Instructors cannot download or launch the site-default `.seb`**, not even for their own exam, unless they have `change_sebconfigpreset`. To test in SEB, an exam needs its own preset or its own uploaded file. The default `.seb` should still be encrypted with a settings password (operational, from the P1 review).
3. **Roster uploads change nothing on accounts with a password or a social login:** no names, e-mail or re-activation. Teachers who fixed such e-mails through uploads will see those rows skipped.
4. **A NetID-only student who signs in with Google gets a separate account.** It is not linked by e-mail. Rare: a third provider with the same e-mail then fails with the library's "multiple accounts" error. This is moot if production has no social-auth keys.

## Not done here
- **P3 performance** (including the 300-student load test) is deferred to the end, as agreed on 2026-10-13.
- Operational items from the P1 review are unchanged:
  - production `local_settings.py`;
  - GitLab protected variables;
  - the nginx deny for `/media/problem_import_test_data/`;
  - the AI-2 runtime stderr check;
  - encrypting the default `.seb`;
  - whether production has social-auth keys.
- Minor, pre-existing and not in the P1 list: the ticket and contest user pickers are not scoped like the others.
