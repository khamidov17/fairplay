# P1 report: AI hints, IDE / Run, analytics, exam surface, SEB

Branch `p1-aiide`. Findings: AI-2, AI-3, AI-4, AI-5, AI-6, AI-8, the exam surface allowlist, SEB-1.

**Full suite:** `bash classroom/scripts/dev-test.sh judge` ran 1695 tests: OK, 1 expected failure (the known `test_nan_score_does_not_crash`). The baseline was 1637, so 58 tests are new. `flake8` is clean on every changed file. `makemigrations --check` reports no changes, and `check_migration_graph judge` finds a single leaf.

**Fails before the fix:** I stashed every non-test change (including the migration) and ran the new test classes against the old code. The non-IDE run had 55 tests, with 51 failures and 17 errors, counting subtests. For the IDE classes, 8 of the 10 failed or errored; to run them I temporarily stubbed the new import. Every finding has at least one failing test. The new tests that pass on both versions do so on purpose. They guard behaviour that must not change: output stays visible outside a contest and after the contest ends, homework is not locked, editors and spectators are not locked, and five Runs per minute still work.

**Migration:** `judge/migrations/0220_p1_aiide_exam_surface.py`. It is self-contained (4 `AddField`, depends on `0219`) and needs renumbering on merge. It adds a nullable FK column to `judge_submission`. MariaDB 10.3+ adds a nullable column instantly, but check this on production before deploying.

## Per finding

### AI-2: "Hide IDE / Run Test output" on every path. Fixed.
- **Recorded at creation.** A new `Submission.sandbox_hidden_contest` FK is set by `IDESubmitView` (both `/ide/submit` and the workspace Run). It is set when the user's current assignment hides output. It is also set when the user has any other LIVE participation that hides output, is still running and is not staff. This closes the "Leave, Run, rejoin" route.
- **One check for every view.** `judge/utils/sandbox_output.py:submission_output_hidden_for(submission, user)` is used by `/ide/status/<id>`, `/submission/<id>` and `/widgets/submission_testcases` (`SubmissionStatus` / `SubmissionTestCaseQuery` set `sandbox_output_hidden`, and `status-testcases.html` stops printing `case.output`). The output stays hidden until that assignment ends. Assignment staff are exempt.
- **Other paths, checked:**
  - Websocket events carry only type and case position, never output (`bridge/judge_handler.py`).
  - API v2 submission list and detail exclude IDE runs or have no output field.
  - Workspace JSON (`problem_workspace_submission_status`) has no output field.
  - The test-data diff view is editor-only.
- **Not changed, on purpose:** `feedback` / `extended_feedback` (runtime exception, traceback) are still returned. The field's help text promises this, and an existing test asserts it (`test_runtime_error_keeps_traceback`). If the judge fork ever writes the program's stderr into `extended_feedback`, that becomes a channel again. This needs a runtime check with the real judge.
- **Files:** `models/submission.py`, `utils/sandbox_output.py`, `views/ide.py`, `views/submission.py`, `templates/submission/status-testcases.html`.
- **Tests** (`judge/views/tests/test_ide.py`, class `IDESandboxOutputEveryPathTestCase`): `test_run_records_the_hiding_contest`, `test_output_hidden_on_submission_page_and_widget`, `test_leaving_the_contest_does_not_reveal_output`, `test_run_made_after_leaving_a_running_contest_is_hidden`, `test_output_shown_after_the_contest_ends`, `test_run_outside_any_contest_records_nothing`, `test_assignment_staff_are_not_hidden_from`.

### AI-3: a contest can switch AI hints off. Fixed.
- **Rule.** `ai_hints.hints_enabled_for(problem, contest, user)`: inside a contest, `contest.allow_ai_hints` alone decides. Outside a contest, the problem flag decides. If the viewer is currently in an assignment, that assignment's switch also applies to work outside it, such as an old practice submission opened during the exam.
- **Callers.** The status page, the hint POST and the workspace pass `user`.
- **Defaults off.** The create_v2 checkbox is no longer pre-ticked. The Excel-import editor prefill now defaults to off too.
- **Files:** `ai_hints.py`, `views/submission.py`, `views/problem.py`, `templates/problem/create_v2.html`, `views/problem_import.py`.
- **Tests** (`test_ai_hints.py`):
  - `ContestControlsAIHintsTestCase`: `test_contest_off_refuses_hint_on_contest_submission`, `test_contest_off_refuses_hint_on_practice_submission_while_in_contest`, `test_practice_submission_outside_contest_uses_problem_flag`, `test_workspace_hides_hints_in_contest_with_hints_off`, `test_status_page_hides_hints_for_contest_submission`, `test_create_v2_ai_hints_default_off`.
  - The old `test_hints_enabled_for_or_logic` is now `test_hints_enabled_for_contest_decides`. It asserted the old OR rule, which was the bug.

### AI-4: no hint while the verdict is hidden. Fixed.
- **Status page and POST.** The status page sets `ai_hints_possible` only when `submission.results_visible_to(user)`, so the section is not rendered. `SubmissionAIHint.post` returns 403 before it reserves quota or calls the LLM.
- **Workspace.** The workspace uses the new `ai_hints.results_hidden_in_contest_problem(contest_problem, user)` (the problem-level twin of `results_visible_to`), so the AI tab and its data attributes are not rendered in a hidden-results exam.
- **Tests** (`test_ai_hints.py`, class `HiddenVerdictAIHintTestCase`): `test_hint_post_refused`, `test_status_page_does_not_offer_hint`, `test_workspace_does_not_offer_hint`, and `test_hint_offered_once_results_are_finalized` (the safe direction).

### AI-5: anomaly percentile. Fixed.
- **Percentile.** It is now the share of ranked students with a *strictly lower* composite score, so tied students share the lowest value.
- **Level.** A new `anomaly_level` is `high` / `medium` only when the score is above 0 and the percentile is at least 90 / 70. The template uses it.
- **Result.** With 20 students and 2 outliers, the 18 ordinary ones show 0th / low, and the outliers show 90th and 95th / high.
- **Files:** `utils/contest_analytics.py`, `templates/contest/analytics.html`.
- **Tests** (`judge/utils/tests/test_contest_analytics.py`, new): `test_twenty_students_two_outliers`, `test_all_tied_at_zero_is_not_high`.

### AI-6: students with little data are not ranked. Fixed.
- **No data is None.** `avg_attempts_before_ac` and `spacing_variance` now return `None` instead of 0 when there is no data. `spacing_variance` needs at least 3 submissions.
- **Not enough data.** Students with fewer than `DMOJ_ANALYTICS_MIN_SUBMISSIONS` (new setting, default 3) completed submissions get `insufficient_data=True` and `anomaly_score=None`. They are left out of the class mean and standard deviation, shown as "Not enough data", and sorted last.
- **Files:** `utils/contest_analytics.py`, `views/contest_analytics.py`, `templates/contest/analytics.html`, `dmoj/settings.py`.
- **Tests:** `test_few_submissions_marked_not_enough_data`, `test_minimum_is_a_setting`, `AnalyticsNoSubmissionStudentsTestCase.test_absent_and_single_submission_students_are_not_ranked` (real DB rows), and `test_analytics_page_lists_absent_students_last`.

### AI-8: atomic limits, honest hint quota wording. Fixed, with one point deferred.
- **IDE limit.** The slot is reserved with `cache.add(key, 0, ttl)` and then `cache.incr`, after validation and before the run is created. A fast-path `get` remains only as an early exit. If the key expires between `add` and `incr`, it is retried, so there is no 500 any more.
- **AI-grading limit.** The per-problem rate list is now read, checked and written under a cache mutex. The global quota uses `add` + `incr`, so the first-use race is gone. The mutex now lives in the shared `judge/utils/cache_mutex.py`, and `ai_hints` uses it too.
- **Hint quota wording.** It was "used all your AI hints for today. Try again tomorrow." It now says "You have used all 5 AI hints allowed in a 5-minute window. Try again in a few minutes." (`ai_hints.quota_exhausted_message()`). I kept the behaviour (a sliding window) and fixed the wording. A real daily cap would be a product decision.
- **Deferred:** the reviewer's last bullet, "hints stay on after the assignment ends". With AI-3 the contest flag now governs that assignment's submissions even after the end. Whether it should switch off at the end is a product decision.
- **Tests:**
  - `test_ide.py`, class `IDERateLimitAtomicTestCase`: `test_stale_counter_read_cannot_pass_the_limit`, `test_counter_expiring_mid_request_does_not_500`, `test_five_runs_then_limited`.
  - `test_ai_grading_quota.py`, class `AIGradingRateLimitConcurrencyTestCase`: `test_parallel_burst_respects_the_window` (10 real threads with a widened read-to-write gap; the old code let all 10 through).
  - `test_ai_hints.py`: `test_quota_message_is_not_daily`.

### Exam surface allowlist. Fixed.
- **When it applies.** `ContestMiddleware` runs the check right after the SEB check, for a LIVE, not-ended participation in a contest where `Contest.locks_site_during_participation` is true, and only for users who do not bypass SEB (editors, testers, spectators, course admins, `edit_all_contest`).
- **New field.** `Contest.lock_site_during_participation` is a nullable boolean. Empty means automatic: on when `seb_required` or `format_name == 'exam'`. On / Off overrides that. It is editable in the assignment editor (a select: Automatic / On / Off, written only when sent) and in the admin.
- **Allowed:**
  - any route whose `contest` kwarg is this contest (pages, join/leave, drafts, own submissions list, SEB config / launch token);
  - this contest's problems: `problem_detail` (GET only, because comments POST there), `problem_submit`, `problem_pdf` and the workspace run / submissions / submission JSON;
  - the language template for those problems;
  - own submissions *in this contest* (status, testcases widget, row widget, source, abort, file, test-case download, AI grade, and AI hint only when `allow_ai_hints` is on);
  - `/ide/status/<id>` (the workspace Run polls it; the view checks the owner);
  - logout, SEB quit, the SEB launch prefix, static/media, set_language, the progress beacon, and rich-editor upload/download (needed for short-answer images and statement images).
- **Everything else.** A GET redirects to the contest page. Other methods, and `Accept: application/json`, get a 403 JSON.
- **Blocked in the tests:** `/ide/`, `/ide/submit`, other problems, the problem list, tickets (list and new), editorial, problem submissions, user pages, home/blog, all submissions, other contests, practice submissions, the API, comment POSTs, and AI hint when off.
- **Files:** `judge/middleware.py`, `models/contest.py`, `views/contests.py`, `templates/contest/create.html`, `admin/contest.py`.
- **Tests:**
  - `judge/views/tests/test_exam_surface.py` (new): `test_blocked_pages`, `test_blocked_posts`, `test_allowed_pages`, `test_workspace_run_and_its_status_allowed`, `test_ai_hint_route_allowed_when_contest_allows_hints`, `test_not_applied_to_ordinary_homework`, `test_flag_turns_lock_on_for_homework_and_off_for_exam`, `test_seb_required_contest_locks_by_default`, `test_editor_not_locked`, `test_spectator_not_locked`, `test_json_clients_get_403_not_redirect`.
  - `test_contest_editor.py`: `test_post_roundtrips_lock_site_during_participation`.
- **Please check (product):**
  - The assignment editor defaults new assignments to the Exam format, so the lock is on for most new assignments unless the teacher picks Off.
  - `ContestLeave` still lets a student leave a non-SEB exam and use the site. The lock applies only while they are in the assignment. Their Runs stay hidden (AI-2), but nothing else changes.
  - Editor-attachment download stays open, as before, because exam statements embed those images. A UUID shared by a friend is therefore still reachable.

### SEB-1: .seb download gate and the Config Key limitation. Fixed for the gate; the Config Key part is a limitation of SEB.
- **Who may fetch a contest's `.seb`.** `utils/seb.py:seb_config_downloadable_by(contest, user)` decides for the download button, the launch-token mint and the token-authorized launch fetch. The launch fetch re-checks the user the token was minted for.
  - Editors always may.
  - Anyone else must be on the roster (`Contest.seb_config_participant_to_be`): class member for class-restricted contests, organization member for org-private contests without classes, `private_contestants` for private ones, and `join_organizations` when that is limited. A public contest has no roster, so anyone who can see it counts.
  - The time is restricted too: from `DMOJ_SEB_CONFIG_DOWNLOAD_WINDOW_MINUTES` (new setting, default 30) before the start until the end.
  - Spectators, scoreboard viewers, course teachers and the like who are not on the roster are refused.
- **Launch links.** `seb_launch_context` shows the download / launch links only to users who may fetch the file.
- **Preset `.seb`.** It is served only to users with `judge.contest_seb_requirement` who edit a contest that uses that preset (the one named by `?contest=` when given), or to users with `judge.change_sebconfigpreset` (the admin, which links to it). The editor's "Preview .seb" link now appears only for the preset this contest is linked to. As a result, the site default can no longer be previewed before an assignment adopts it, as the brief asked.
- **New option.** `seb_require_browser_exam_key` on Contest and `require_browser_exam_key` on SEBConfigPreset, both default off. When either is on, `request_matches_seb(..., require_bek=True)` ignores the Config Key header and logs a clear message. It is wired into the middleware, `ContestJoin` and the contest page through `Contest.request_matches_seb(request)`. It is admin-editable and permission-gated like the other SEB fields. The preset field is in `_LOCKOUT_FIELDS`, so it cannot be flipped during a live exam.
- **The SEB limitation.** The gate accepts the Config Key hash header on its own. The Config Key is computed from the `.seb` settings, so anyone holding an unencrypted `.seb` can compute it, and an ordinary browser that adds the header then passes. No server-side check can tell such a browser from SEB. The Browser Exam Key also covers the SEB application itself, so it is the stronger proof. My recommendations:
  - turn on "require Browser Exam Key header" wherever no student uses macOS;
  - encrypt the `.seb` with a settings password, so the file does not disclose its configuration;
  - keep the download window short, which is what the new gate does;
  - keep the default off, because the macOS SEB build sends only the Config Key.
- **Files:** `utils/seb.py`, `views/contests.py`, `models/contest.py`, `models/seb.py`, `admin/contest.py`, `admin/seb.py`, `middleware.py`, `templates/contest/create.html`, `dmoj/settings.py`.
- **Tests:**
  - `judge/views/tests/test_seb.py`, class `SEBConfigDownloadGateTestCase`: `test_student_cannot_download_hours_before_start`, `test_student_can_download_within_window_before_start`, `test_student_can_download_while_running`, `test_nobody_but_editors_after_the_end`, `test_viewer_not_on_roster_denied_even_during_exam`, `test_editor_always_allowed`, `test_window_is_a_setting`, `test_contest_page_hides_links_before_window`.
  - `test_seb.py`, class `SEBRequireBrowserExamKeyViewTestCase`: `test_config_key_alone_blocked`, `test_browser_exam_key_allowed`.
  - `test_seb.py`, `ContestSEBPresetTestCase`: `test_preset_preview_denied_with_permission_but_not_editing_a_contest_using_it`, `test_preset_preview_denied_for_editor_without_permission`, `test_preset_preview_denied_for_other_contest_key`. The existing `test_preset_preview_allowed_with_permission` now makes the user an author first.
  - `judge/utils/tests/test_seb.py`, class `RequireBrowserExamKeyTestCase` (3 tests), and `test_no_links_for_a_viewer_who_may_not_fetch_the_file`.
- **Existing tests adjusted:** five SEB header tests requested `home`, which the exam lock now blocks during an SEB exam. They now request the contest page; what they check (header acceptance) is unchanged. The `SEBLaunchContextTestCase` unit tests now mock the new permission check.

## Touched outside my area
- `judge/views/problem_import.py`: the editor prefill for imported items now has `ai_hints_enabled: False` (AI-3 default off).
- Assignment editor (`judge/views/contests.py` `ContestCreate`, `templates/contest/create.html`): a new "Lock the site during participation" select, and the preset preview link change.
- `judge/admin/contest.py`, `judge/admin/seb.py`: the new fields.
- Existing tests edited: `test_seb.py` (`home` changed to the contest page, preset preview), `test_ai_hints.py` (the OR-logic test now asserts the new rule), `judge/utils/tests/test_seb.py` (mock in setUp).

## Uncertain / runtime checks needed
- Whether the judge fork puts program stderr in `extended_feedback` for IDE runs (see AI-2).
- The exam lock was tested against the URL names in `dmoj/urls.py`. A page that loads an extra endpoint I did not exercise would be blocked during an exam and need adding to the allowlist in `judge/middleware.py`. Examples are a JS widget on the contest page or workspace, or i18n served from a URL rather than the compiled static file. The rich editor, Monaco templates, drafts, Run and polling are covered.
