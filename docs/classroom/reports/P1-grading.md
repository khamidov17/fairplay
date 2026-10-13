# P1 report: grading, results visibility, question types

Branch `p1-grading`, worktree `/home/claude/wt/grading`. Findings: G-1 to G-11 (G-4 = AI-1) and the expected-failure test `test_nan_score_does_not_crash`.

**Full suite:** `bash classroom/scripts/dev-test.sh judge --parallel 2` with `CLASSROOM_TEST_DB_NAME=test_classroom_grading`: **1677 tests, OK, 0 expected failures** (baseline: 1637 + 1 expected failure). flake8 is clean for every changed Python file (run from `classroom/`).

Every new test was run against the unfixed code (source files stashed, tests kept) and failed there. The exceptions are noted below.

## Per finding

### G-1: hidden exam results leak through filters, counts, AC rate and rankings (fixed)
- **New single helper:** `judge/utils/result_visibility.py`.
  - `results_hidden_q(prefix)` is a `Q` that applies the same rule as `Submission.contest_results_hidden`: exam format, not finalized, and the `ContestProblem.hide_results` override or, when unset, the type default.
  - `results_visible_q(user, prefix)` mirrors `results_visible_to`. Users with `view_all_submission` see everything. Problem editors, contest authors and curators, `view_contest_submissions` holders and testers with `tester_see_submissions` see through the mask.
  - `visible_results(qs, user)` applies it to a queryset.
- **Where it is applied:**
  - Submission lists: the `?status=` filter and `?results` counts (`SubmissionsListBase`), plus the cached global result counts (`AllSubmissions`, hidden rows are always left out).
  - API v2: the `?result=` filter (`APISubmissionList.filter_queryset`) and `APIUserDetail.solved_problems`.
  - Contest page AC rate: `assignment_problem_stats(contest, user)`, called from the contest page and the in-contest problem list. The user count stays, since it does not depend on verdicts.
  - `ContestStats` charts.
  - Ranked (best) lists, `RankedSubmissions` and `ContestRankedSubmission`. Hidden ids are excluded inside the ranking SQL, so a hidden best score can neither show up nor be inferred from a missing row.
  - Solved markers: `user_completed_ids`, `contest_completed_ids` and `Problem.is_solved_by`.
  - Public `Problem.update_stats` and `Profile.calculate_points`. Both are public and would reveal a hidden AC.
- Finalize and unfinalize now clear the completion caches and recompute problem stats and profile points for the contest (`_refresh_hidden_result_aggregates` in `judge/tasks/contest.py`).
- **Files:**
  - `judge/utils/result_visibility.py` (new)
  - `judge/views/submission.py`, `judge/views/ranked_submission.py`, `judge/views/api/api_v2.py`, `judge/views/contests.py`, `judge/views/problem.py`
  - `judge/utils/problems.py`, `judge/models/problem.py`, `judge/models/profile.py`, `judge/tasks/contest.py`
- **Tests:** `judge.views.tests.test_hidden_results.HiddenResultFiltersTestCase` (13 tests) and `ResultsHiddenQueryMatchesModelTestCase.test_q_matches_property`. The second checks, row by row, that the `Q` helpers agree with the model properties across hidden, forced-hidden, forced-shown, non-exam, practice and finalized cases. It is a consistency test for the new helper, so it has no "before" state.
- **Uncertain:**
  - The editor exemption in the `Q` uses `Problem.get_editable_problems(user)`, which can be narrower than `Problem.is_editable_by`. When they differ, an editor sees fewer rows in filters, which is the safe direction.
  - `Profile.calculate_points` was rewritten as an equivalent query over `Submission` so that hidden rows can be excluded. The rest of the suite still passes.

### G-2: Finalize and rejudge reset AI scores to 0 (fixed)
- `grade_internally` (`judge/submission_grading.py`) now keeps the stored score when all of the following hold:
  - the grader returned a no-LLM placeholder (`IR` or `IE` with 0 points);
  - the stored submission already has an AI score (`D` with `AC` or `WA`);
  - the type supports AI;
  - no explicit AI run is in progress (`should_run_ai_now` is False).
- When this applies, only `rejudged_date` is updated. Explicit AI runs (Grade-desk batch, rejudge widget, the AI button: `run_ai_for_submission`) still re-score.
- Released manual grades still win through `SubmissionManualGrade`, and Finalize still skips `reviewed_by` rows.
- **Tests:** `judge.models.tests.test_exam_contest.FinalizeKeepsAIScoresTest`. It covers Finalize for short answer and AI fill-blank, plain rejudge, an explicit AI run re-scoring with a fake LLM, and a released manual grade surviving a rejudge.
- **Uncertain:** a stored AI fill-blank score is no longer replaced by exact matching on a plain rejudge. So if a teacher changes the accepted answers of an AI-mode fill-blank, they have to re-run AI, which Finalize does not do. This matches the brief's "preserve unless explicitly asked to regrade with AI".

### G-3: editing an MCQ or true/false breaks earlier answers (fixed)
- New `sync_mcq_options()` and `selected_option_ids()` in `judge/question_types/mcq.py`.
  - The create-v2 editor (`problem_create_v2.py`) no longer deletes options when an MCQ stays an MCQ. Options are updated in place by id: the edit data now includes `id`, and `templates/problem/create_v2.html` stores it on each row and sends it back (2 small JS lines).
  - Payloads without ids are matched by position.
  - Only rows that are really new are created, and only rows really removed are deleted.
  - True/false is synced by position, so both ids stay.
- Deleting an option that a stored submission selected is refused with a 400 and a clear message, and nothing changes. The legacy formset editor (`views/mcq.py`) refuses this too.
- **Tests:**
  - `judge.views.tests.test_problem_create_v2.MCQEditKeepsOptionIdentityTestCase` (6 tests): typo fix, no-id payload, answer-key change regrades, reorder plus delete of an unused option, refused delete of a used option, true/false.
  - `judge.views.tests.test_mcq.MCQAuthoringViewTestCase.test_edit_refuses_deleting_selected_option`.
  - `test_answer_key_change_regrades` also passes before the fix, by design: it guards that a real key change still regrades.

### G-4 / AI-1: AI score denominator comes from the model's reply (fixed)
- `parse_calibrated_scores` (`judge/question_types/ai_grading.py`) now returns exactly one item per configured rubric row:
  - missing rows score 0;
  - unknown or extra rows are ignored;
  - duplicates are collapsed. When every returned name matches a configured row, items are matched by name; otherwise by position.
  - Non-dict items and NaN/inf scores count as 0.
- The fix is central, so short answer, AI fill-blank and code AI all take the configured total.
- **Tests:** in `judge.views.tests.test_ai_tag_and_grading.SharedAIGradingHelpersTestCase`:
  - `test_parse_uses_configured_table_as_denominator`
  - `test_parse_collapses_duplicate_rows`
  - `test_parse_extra_subscores_are_ignored` (replaces the old test that pinned "extras get max 1.0")
  - `test_parse_ignores_non_finite_and_non_dict_items`
  - `test_short_answer_fake_llm_one_of_three_rows`: a fake LLM returns 1 of 3 rows at full marks, which now gives 2/6 and WA.
- `judge/tests.py::IndexMatchingTests.test_extra_sub_scores_get_default_max` pinned the old behaviour. It was renamed to `test_extra_sub_scores_are_ignored` and updated.

### G-5: any instructor can re-run AI scoring on another course (fixed)
- `is_teacher_viewer` (`judge/ai_grading_quota.py`) is now scoped to the submission:
  - `edit_all_contest` (site contest admin) still counts;
  - otherwise `contest_object.is_editable_by(user)` for assignment submissions, or `problem.is_editable_by(user)` for practice;
  - `add_organization` and bare `rejudge_submission` no longer count.
- `SubmissionAIGrade` also requires `can_see_detail` (403 otherwise).
- **Tests:** in `judge.views.tests.test_ai_grading_quota`:
  - `test_is_teacher_viewer_false_with_create_course_permission`
  - `test_is_teacher_viewer_false_with_bare_rejudge_permission`
  - `test_is_teacher_viewer_true_for_practice_problem_editor`
  - `test_ai_grade_view_refuses_instructor_of_another_course`
- `test_ai_grading_dual_role.CodeAIJudgeIRTestCase` had a "teacher" who held only `view_all_submission` and `rejudge_submission`. That teacher is now an author of the problem.

### G-6: "solved" visibility lets students read classmates' answers (fixed)
- `Submission.can_see_detail` grants SOLVED access only when `solved_source_visibility_applies(problem)` is true:
  - the problem is code (answers to MCQ, blanks, essays and uploads are never shared with peers);
  - the problem is not used in a non-finalized exam with hidden results.
- `is_solved_by` also ignores hidden verdicts (see G-1).
- The denial page shows the "Solve X to view it" hint only where solving would actually grant access. The response is therefore identical whether one's own hidden answer is right or wrong.
- **Tests:** `judge.views.tests.test_hidden_results.HiddenResultSourceVisibilityTestCase`:
  - classmate denied during the exam;
  - byte-identical response, with CSRF stripped, for a right and a wrong own answer;
  - non-code practice is own-only;
  - code practice "solved" access still works. This regression guard passes before and after.
- **Product decision:** treating SOLVED as own-only for every non-code type, outside exams too, follows the review's recommended fix. It is the safest default.

### G-7: exam problems can be practised outside the exam (fixed)
- `_submission_blocked_response` (`judge/views/problem.py`) blocks submits outside an assignment with a 403 (HTML or workspace JSON). This applies to non-editors, on any problem, public or not, that is used with hidden results in an exam that is not finalized and has not ended (running or upcoming).
- Editors can still test-submit. After Finalize or the end of the exam, practice works again.
- **Tests:** `judge.views.tests.test_hidden_results.PracticeSubmitDuringExamTestCase`: running exam (enrolled and not enrolled), upcoming exam, after finalize, editor.

### G-8: viewing a file upload's status marks it Aborted (fixed)
- `_finalize_queued_timeout` only applies to bridge-judged (code) types. File uploads stay `QU` until a teacher grades them.
- **Test:** `judge.views.tests.test_file_upload.FileUploadGradeDownloadTestCase.test_status_page_view_does_not_abort_old_upload`.

### G-9: legacy grade-file endpoint (fixed by removal)
- `submission/<id>/grade-file` and `file_upload.submission_file_grade` were removed. Nothing in the UI used them (checked templates and JS), and grading goes through the Grade desk, which checks assignment editing rights and Release All.
- **Test:** `test_legacy_grade_file_endpoint_removed`. It replaces the three old tests of the endpoint.
- **Product decision:** removal (safest), as the brief allows.

### G-10: student score differs from the ranking for code with an AI rubric (fixed)
- `machine_assignment_points` (`judge/utils/contest_grade.py`) scales the combined `submission.points` when `ai_points` is set, exactly as `_refresh_contest_score` does for the ranking.
- **Test:** `judge.views.tests.test_contest_grade_fixes.CodeAIRubricStudentScoreTestCase` (6/10 test cases with a 9/10 AI rubric gives 7.2 in both places).

### G-11: ECOO counts IE/CE and in-flight submissions as the latest (fixed)
- The ECOO `update_participation` again excludes `IE`, `CE` and submissions with no result yet (in flight), as upstream does.
- A new format hook, `counts_as_latest()`, makes the Grade desk's "final version" (`scoring_submissions_for_contest`) apply the same rule.
- **Test:** `judge.models.tests.test_exam_contest.EcooLatestSubmissionTest`.

### NaN/inf manual scores (fixed; `@expectedFailure` removed)
- `ContestGradeSave` and `PracticeGradeSave` return 400 for any non-finite score.
- **Tests:** `test_contest_grade_variants.test_nan_score_does_not_crash` (nan, inf, -inf, 1e309) and `test_grade_entry.test_practice_grade_save_rejects_non_finite_score`.
- **Contract change:** the old tests `test_infinity_clamps_to_total`, `test_negative_infinity_clamps_to_zero` and `test_huge_exponent_clamps_to_total` expected inf to be clamped. Per the brief ("NaN/inf scores must return 400") they were replaced by `test_infinity_rejected`.

## Touched outside my area
- `templates/problem/create_v2.html`: 2 JS lines that round-trip option ids (G-3). There is no visual change.
- `judge/views/mcq.py` (legacy MCQ editor): refuses to delete an option that was used (G-3).
- `judge/models/profile.py` (`calculate_points`) and `judge/views/api/api_v2.py` (`APIUserDetail.solved_problems`): hidden exam verdicts excluded (G-1).
- `dmoj/urls.py`: one route removed (G-9).

No migrations.
