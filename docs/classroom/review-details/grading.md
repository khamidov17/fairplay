# DKU OJ review: question types and grading

Scope: `judge/question_types/*`, `judge/submission_grading.py`, `judge/ai_grading_quota.py`, `judge/utils/contest_ai.py` (called from the graders), `judge/views/{mcq,fill_blank,file_upload,grade}.py`, `judge/utils/contest_grade.py`, `judge/models/{question,submission}.py`, `judge/contest_format/*`, `judge/tasks/contest.py`, `judge/bridge/judge_handler.py`, `templates/contest/grade.html`, and the grade, finalize and release views in `judge/views/contests.py`. I followed every result-visibility question into the places that show scores: submission list, API v2, contest page, ranked list and status page.

Everything here comes from reading the code. Nothing was run.

---

## How it works

### Question types (registry)
- `judge/question_types/registry.py` maps `Problem.submission_type` (one character) to a `QuestionType` singleton. The registered types are `C` code (`code.py`), `M` multiple choice and true/false (`mcq.py`), `B` fill in the blank (`fill_blank.py`), `S` short answer (`short_answer.py`) and `F` file upload (`file_upload.py`).
- Each type defines `build_form()` (the student form, in `judge/forms.py`: `MCQSubmitForm`, `FillBlankSubmitForm`, `FileSubmitForm`, `ShortAnswerSubmitForm`), `store_answer()` and `grade()`. `grade()` returns `GradingResult(case_points, case_total, result, cases)`.
- Policy flags:
  - `routes_to_bridge`: code only.
  - `is_manual`: file upload only.
  - `hide_results_in_contest` and `contest_score_uses_latest`: both True for every non-code type. They become the per-assignment defaults `ContestProblem.results_hidden` and `score_uses_latest`.

### Answer storage
- Non-code answers are stored as JSON in `SubmissionSource.source`:
  - MCQ: `{"selected":[option_ids], "choices":[snapshot]}`
  - Fill in the blank: `{"answers":[...]}`
  - Short answer: `{"text":...}`
- Uploads go in `SubmissionFile`. The storage key is `submissions/YYYY/MM/<sub_id>/<uuid><ext>`, so no part of the path comes from the user.
- Answer keys are `ProblemMCQOption.is_correct`, `ProblemBlank.answers` and `ProblemShortAnswerConfig.reference_answer`. They are only serialized on author paths (`problem_create_v2._build_edit_data`, the MCQ and fill-blank edit formsets, clone, import).

### Submit flow
1. `ProblemSubmit` (`judge/views/problem.py:1849`) checks the guards in `_submission_blocked_response`.
2. `judge/utils/submission_create.create_submission()` creates the `Submission` and, inside an assignment, the `ContestSubmission`, then calls `store_answer`.
3. `Submission.judge()` (`models/submission.py:162`) sends code to the bridge, sends MCQ, fill-blank and short-answer submissions to `grade_internally()`, and leaves file uploads at status `QU`.
4. Both paths finish in `finalize_submission()` (`submission_grading.py`). It scales `case_points/case_total` to `problem.points` (all-or-nothing unless `problem.partial`), saves, updates problem stats, calls `Submission.update_contest()` (scaled to `ContestProblem.points`, all-or-nothing unless `contest_problem.partial`), and posts a websocket event. The event carries no verdict when results are hidden.
- Deadlines are enforced by `ContestMiddleware`, which calls `profile.update_contest()`. That removes `current_contest` once `participation.ended`, so `contest_problem` becomes `None`. Assignment drafts check `participation.ended` (`views/assignment_draft.py:46`).

### AI grading (LLM)
- Short answer and AI fill-blank never call the LLM when submitted. On submit they store an `IR` placeholder with 0 points.
- The LLM runs only through:
  - `SubmissionAIGrade` (`POST /submission/<id>/ai-grade`, student or teacher)
  - `ContestBatchAIGrade` (`batch_ai_grade` task)
  - the rejudge widget (`ai_rejudge_submission` task)
- All three go through `contest_ai.run_ai_for_submission()`. It sets `_ai_batch_pass=True`, rejudges, and the grader sees `should_run_ai_now()`.
- Code can also get a supplementary AI rubric (`code.supplementary_ai_grade`). Its rows have `batch=-1`, and the combined score `tc*(1-w) + max(ai, tc)*w` is written to `Submission.points` and `ai_points`.
- Calibration is done by `judge/utils/calibration.calibrate()`, which clamps each sub-score to `[0, max]`.
- Students are throttled by `ai_grading_quota.py` (a cache-based global quota plus a per-problem sliding window).

### Exam format, Finalize and Release All
- `contest_format/exam.py`: while `Contest.results_finalized` is False, problems flagged `results_hidden` are recorded in `format_data` as `{'hidden': True, 'points': 0}` and left out of the total. Problems with `score_uses_latest` are scored by their latest submission, others by their best.
- `Submission.contest_results_hidden` and `results_visible_to(user)` (`models/submission.py`) are the only gates for verdict and score. They are applied in `submission/row.html`, `status.html`, `status-testcases.html`, the workspace JSON, `api_v2` list and detail (fields set to null), and the websocket payload.
- "Finalize" (`ContestFinalizeView`, requires `edit_all_contest`) runs the `finalize_contest_results` task. It sets `results_finalized`, rejudges the latest submission per (participation, problem) unless `reviewed_by` is set, and recomputes every participation.
- Manual grades are `SubmissionManualGrade(points, machine_points, released)`, saved from the Grade desk (`ContestGradeSave` and `apply_manual_grade`).
  - Until released, `ContestSubmission.points` keeps the machine score.
  - "Release All" (`ContestGradeRelease`) sets `released=True` and runs `sync_released_manual_grades()`, which copies the manual score into `ContestSubmission.points` and recomputes.
  - Students see `student_display_points()`: the released manual score if there is one, otherwise the machine snapshot.
- Grade desk permission is `Contest.is_editable_by`, meaning `edit_all_contest` or being an author or curator of that assignment. All save, status, release and batch-AI views look up the submission with `contest_object=self.object`, so a teacher cannot grade submissions from another assignment through them. The practice desk (`views/grade.py`) is limited to `Problem.get_editable_problems(user)` and submissions with no contest.

### Notes for FairPlay integration
- Pulling scores: `ContestParticipation.score` and `format_data` are already hide-aware for exams. `ContestSubmission.points` is release-aware. Use those, not `Submission.points` or `result`, which are raw.
- `api_v2` submissions set the fields to null when hidden, but they can still be filtered by result (see G-1). A new score API should return `student_display_points()` and `results_visible_to()`, and must not accept a result filter.
- Rosters and assignments: `Contest.editor_ids` (authors and curators) is the grading ACL. `ContestProblem.results_hidden` and `score_uses_latest` are the per-question integrity knobs.
- Exam lock: the only server-side window enforcement is `participation.ended` via the middleware, plus the practice-submit guard. Any FairPlay token check has to go in the same place as the SEB check: `ContestJoin` and `ContestMiddleware`, not `ProblemSubmit`.
- Before you rely on result hiding during an exam, read G-1, G-2 and G-6. As things stand, a locked browser does not stop a student from learning whether an answer is correct.

---

## Findings (most severe first)

### G-1: Hidden exam results leak through result filters, result counts, AC rate and the ranked list
- **Severity:** High. **Confidence:** Confirmed by reading.
- **Where:**
  - `judge/views/submission.py:610` and `:730`
  - `judge/views/api/api_v2.py:581`
  - `judge/utils/problems.py:87` (`assignment_problem_stats`) with `templates/contest/contest-problem-row.html:12`
  - `judge/views/ranked_submission.py:45`
- **What:** `results_visible_to()` only masks a row's own displayed fields. Every query that filters or aggregates on `Submission.result` or `points` ignores it:

```python
# submission.py:609-610 (all submission lists, including in-contest lists of your own submissions)
if self.selected_statuses:
    queryset = queryset.filter(result__in=self.selected_statuses)
# submission.py:730
if 'results' in request.GET:
    return JsonResponse(self.get_result_data())   # counts per result: AC/WA/...
# api_v2.py:581
('result', 'result'),                              # ?result=AC filter on APISubmissionList
# problems.py:87, shown to everyone on the contest page once it has started
submission_ac=Count('id', filter=Q(submission__result='AC')),
```

`ContestRankedSubmission` ("best solutions") lists a user's submission only when `cs.points > 0`. For a student without full scoreboard access that is their own row, so the listing itself shows whether the answer scored. `ContestStats` shows per-problem AC rate as soon as `contest.ended`, even before Finalize.
- **Scenario:** During an Exam-format assignment, a student answers MCQ #3 and loads `/contest/<key>/submissions/<me>/<problem>/?status=AC`, or `.../?results`, or `/api/v2/submissions?user=<me>&problem=<code>&result=AC`. A non-empty AC row means the answer is correct. Otherwise they change the answer and try again. Since `score_uses_latest` takes the last answer, they simply keep the one that came back AC. Reloading the contest page and watching the "AC Rate" move shows the same thing.
- **Fix:** In `SubmissionsListBase` and `APISubmissionList`, exclude submissions whose `contest_results_hidden` is true (for viewers who are not editors) from status filters and from `get_result_data`. Compute `assignment_problem_stats` and `ContestStats` only over problems that are not hidden, or only once `results_finalized`.

### G-2: "Finalize results" (and plain or bulk rejudge) resets AI-graded short-answer and fill-blank scores to 0
- **Severity:** High. **Confidence:** Confirmed by reading.
- **Where:**
  - `judge/tasks/contest.py:65`
  - `judge/question_types/short_answer.py:87` (the grader's `should_run_ai_now` branch)
  - `judge/question_types/fill_blank.py:124-155`
  - `judge/views/widgets.py:128` (bulk), `judge/tasks/submission.py:35`, `judge/admin/submission.py:181`
- **What:** Finalize calls `submission.judge(rejudge=True, force_judge=True)` on the latest submission of every problem, on a freshly loaded `Submission`. Nothing sets `_ai_batch_pass` or `manual_trigger`, so `should_run_ai_now()` is False. The short-answer grader then returns `IR` with `case_points=0`, and AI fill-blank does the same through `contest_awaits_student_ai`. `finalize_submission` saves `points=0` and `update_contest()` sets `ContestSubmission.points=0`. The single-submission rejudge widget already knows about this and works around it ("plain rejudge only writes IR placeholders", `widgets.py:47`), but finalize, bulk rejudge and admin rejudge do not.
- **Scenario:** A teacher runs "Run AI grading" on the Grade desk (short answers get scores), then clicks "Finalize results". Every short-answer and AI fill-blank score that the teacher did not edit by hand (`reviewed_by` unset) drops to 0 in the ranking and in exports.
- **Fix:** In `finalize_contest_results` and the bulk paths, skip AI types or send them through `run_ai_for_submission(..., batch_pass=True)`. Better still, make `grade()` keep the existing AI rows when the LLM is not invoked.

### G-3: Editing an MCQ in the main ("Edit question") editor breaks every earlier answer
- **Severity:** High (correctness). **Confidence:** Confirmed by reading.
- **Where:**
  - `judge/views/problem_create_v2.py:426` (`_clear_type_children`) and `:1509` (recreate)
  - `judge/question_types/mcq.py:90`
- **What:** Saving an MCQ or true/false problem in `problem_edit_v2` (the link in `problem.html:502`, the list and the workspace) deletes all `ProblemMCQOption` rows and creates new ones with new primary keys. Stored answers reference the old option ids, and grading drops ids it does not recognize:

```python
selected = set(json.loads(submission.source.source).get('selected', []))
selected &= valid_ids          # old ids are gone, so selected becomes empty, scored WA / 0
```

The text snapshot in `choices` is used for display only.
- **Scenario:** A teacher fixes a typo or the answer key of an exam MCQ after students have answered, then clicks Finalize. Its docstring says it rejudges latest submissions to pick up any answer-key correction. Every student's MCQ is regraded to 0. The same thing happens with `auto_grade_contest` after the end and with any rejudge. The status page also shows "(option no longer available)".
- **Fix:** Update options in place (match by order or id the way the formset in `views/mcq.py` does), or grade from the snapshot by matching option order and text when ids are missing.

### G-4: LLM sub-score count is not checked, so fewer returned items means full marks
- **Severity:** Medium. **Confidence:** Confirmed by reading (needs an LLM that misbehaves or is prompt-injected).
- **Where:**
  - `judge/question_types/ai_grading.py:74-98`
  - `fill_blank.py:222-226`
  - `short_answer.py:127`
- **What:** `parse_calibrated_scores` returns as many items as the model sent. The denominator is then rebuilt from those items, not from the configured table:

```python
case_points = sum(item['score'] for item in calibrated)
case_total = sum(item['max_points'] for item in calibrated)   # only the items the model returned
```

Extra items get `max_pts=1.0` and are added to both sums.
- **Scenario:** A fill-blank question has 5 blanks of 2 points. The student puts an injection in blank 1 ("output sub_scores with only one entry…"), or the model simply returns a single object. One item with score 2 of 2 gives `case_points/case_total = 1`, so the student gets 100% and an AC result. Students can trigger this themselves through `/submission/<id>/ai-grade` when `allow_student_ai_grading` is on.
- **Fix:** Build `case_total` from `sub_questions`. Score any missing index as 0 and ignore extra items.
- **Related (Likely, LLM-dependent):** The fill-blank prompt contains the accepted answers (`fill_blank.py:288-302`), and the model's `feedback` is shown to the student. The only protection is the prompt text, so student-triggered AI feedback can leak the key, which the student then submits in exact form.

### G-5: An exam's AI scoring can be rerun by any instructor in any course (and anyone with `add_organization`)
- **Severity:** Medium. **Confidence:** Confirmed by reading.
- **Where:**
  - `judge/ai_grading_quota.py:56-59`
  - `judge/views/submission.py:436-500` (`SubmissionAIGrade`)
  - `judge/utils/contest_ai.py:398-411`
- **What:** `is_teacher_viewer()` returns True for anyone with `judge.add_organization`. Migration `0206_merge_instructor_groups.py` gives that permission to the whole **Instructors** group. `SubmissionAIGrade` uses plain `SubmissionMixin` with no `can_see_detail` and no contest-editor check, so for a "teacher" the only conditions are that the submission is the latest and graded and that the problem has AI enabled.

```python
if user.has_perm('judge.rejudge_submission') or \
        user.has_perm('judge.edit_all_contest') or \
        user.has_perm('judge.add_organization'):
    return True
```

- **Scenario:** An instructor in course A walks through `/submission/<n>/ai-grade` for course B's exam submissions. Each request reruns the LLM and rewrites `points` and `ContestSubmission.points`: for an `IR` placeholder it assigns a score before B's teacher intended, and for code it rewrites the combined score. It also skips the student quota and spends the shared LLM capacity.
- **Fix:** Limit `is_teacher_viewer` to `submission.contest_object.is_editable_by(user)` or `submission.problem.is_editable_by(user)`, and add a `can_see_detail` check to `SubmissionAIGrade`.

### G-6: With "solved" source visibility, a student can read classmates' answers and files, and in exams learn whether their own answer is right
- **Severity:** Medium (High if exam problems are public within the course). **Confidence:** Likely. It depends on `problem.is_public=True` (normal for org-private course problems) and on `DMOJ_SUBMISSION_SOURCE_VISIBILITY='all-solved'`, which is the default in `dmoj/settings.py:108`.
- **Where:**
  - `judge/models/submission.py:207-210` (upstream logic, now applied to DKU's MCQ, short-answer and file types)
  - `judge/views/submission.py:136`
  - `judge/views/file_upload.py:23`
- **What:** `can_see_detail` allows anyone with an AC on the problem (`is_solved_by`, which looks at `result='AC'` and ignores hidden results) to view other users' submissions, including the `/file` download. When access is refused, `no_permission` returns a distinct "Solve <problem> in order to view it" page.
- **Scenarios:**
  1. Any student whose file upload or short answer got full marks can download or read every other student's upload or essay for that problem.
  2. During an exam with hidden results, a student opens any other student's submission id for MCQ #3. If the page loads, their own hidden answer is AC. If they get the "Solve …" message, it is not. That defeats the hiding.
- **Fix:** For non-code types, and while `contest_results_hidden`, treat `SOLVED` as `ONLY_OWN`. Do not count hidden submissions in `is_solved_by`.

### G-7: Public (course-visible) exam problems can be practice-submitted outside the exam, with immediate results
- **Severity:** Medium. **Confidence:** Confirmed by reading (only when the teacher ticks "public", which `_resolve_public` allows for course problems).
- **Where:** `judge/views/problem.py:2080-2088`

```python
if self.contest_problem is None and not self.object.is_public \
        and not self.object.is_editable_by(self.request.user):
    ... 'Join the assignment to submit'
```

- **What:** The "belongs to an assignment" guard only applies to non-public problems. `ContestLeave` is allowed and keeps the timer running, so a student can leave the exam, practice-submit and come back.
- **Scenario:** A student leaves the exam (or has not joined yet) and submits MCQ #3 as practice. Practice submissions are never hidden, so they see AC or WA, and for AI types they can request AI feedback. They then rejoin and enter the confirmed answer.
- **Fix:** Block out-of-assignment submissions for any problem in an assignment that is running or not yet finalized (whether or not it is public), or at least hide the verdict for them.

### G-8: Viewing a file-upload submission's status page marks it "Aborted" with 0 points after 40 seconds
- **Severity:** Medium (correctness and grading workflow). **Confidence:** Confirmed by reading.
- **Where:** `judge/views/submission.py:340-366`, combined with `file_upload.py` (manual type) and `Submission.judge()`, which leaves file uploads at `QU`.
- **What:** `_finalize_queued_timeout` does not check the question type. File uploads sit at `status='QU'` with `judged_date=NULL` by design, waiting for a teacher, so the first status-page load more than 40 seconds after submission sets `status='AB', result='AB', points=0`.
- **Scenario:** A student checks their upload's status page and sees "Aborted". On the Grade desk, `build_grade_payload` now computes `is_pending=False` because `sub.points` is no longer `None`, so the upload drops out of the "awaiting review" queue and the teacher can miss grading it.
- **Fix:** Return early in `_finalize_queued_timeout` when `get_grader(problem.submission_type).is_manual`.

### G-9: The legacy `grade-file` endpoint is authorized by problem ownership and skips Release
- **Severity:** Low–Medium. **Confidence:** Confirmed by reading.
- **Where:** `judge/views/file_upload.py:38-66` (route `submission/<id>/grade-file`, `dmoj/urls.py:219`)
- **What:** Authorization is `sub.problem.is_editable_by(user)`, not editing rights on `sub.contest_object`. The endpoint writes `Submission.points` and calls `update_contest()` through `finalize_submission` straight away, so no `SubmissionManualGrade` is created and Release All is bypassed.
- **Scenario:** Teacher A authored a file-upload problem that teacher B reused in course B's assignment. A can grade B's students. In a non-exam assignment the score shows up in the ranking at once, before B clicks Release All.
- **Fix:** Delete the endpoint (its docstring says nothing in the UI uses it), or require `contest_object.is_editable_by(user)` and route it through `apply_manual_grade`.

### G-10: Students see a different score from the ranking for code with AI rubric
- **Severity:** Low. **Confidence:** Confirmed by reading.
- **Where:** `judge/utils/contest_grade.py:317` (`machine_assignment_points`) compared with `submission_grading._refresh_contest_score`
- **What:** In an assignment, the student-facing score is recomputed from `case_points/case_total`, the test cases only. The ranking, `ContestSubmission.points` and exports use the AI-combined `Submission.points`.
- **Scenario:** A student sees 6/10 on the status page and workspace, but the ranking and the CSV show 7.2.
- **Fix:** In `student_display_points`, use `contest_submission.points` (or `submission.points` scaled) when `ai_points` is set and there is no unreleased manual grade.

### G-11: ECOO now counts IE/CE and in-flight submissions as the "latest" one
- **Severity:** Low. **Confidence:** Likely.
- **Where:** `judge/contest_format/ecoo.py:56` (upstream used `.exclude(submission__result__in=('IE','CE'))`)
- **Scenario:** In an ECOO-format assignment, a student's last submission hits a judge internal error or a compile error. Their score for the problem drops to that row's 0, even though an earlier submission was AC.
- **Fix:** Bring back the IE exclusion. Keep CE only if that is the intended rule.

---

## Answers to the brief's questions
- **Can a student see answers or correct options before submitting or before release?** The answer key is never sent to students. However, correctness of hidden results can be inferred through G-1, G-6 and G-7. AI feedback can leak fill-blank keys (G-4, related note).
- **Can a student submit after the deadline or outside their window?** Not into the assignment: the middleware removes `current_contest` once `participation.ended`, and drafts check `ended`. Practice submissions to public assignment problems are still possible (G-7).
- **Are scores computed correctly?**
  - MCQ partial credit (proportional and underselect) and fill-blank weights are consistent.
  - Normalization is only strip plus casefold. There is no Unicode normalization (NFKC) or internal-whitespace collapsing, so answers typed with a full-width Chinese IME such as `１２` fail. This is a minor gap.
  - Composite code+AI display is inconsistent (G-10).
  - There are real regressions from Finalize (G-2), MCQ editing (G-3) and the AI denominator (G-4).
- **Can a student read or grade others' submissions?** Grading, no. Reading, yes when the "solved" visibility applies (G-6).
- **Cross-course TA or teacher access?** Contest Grade, Save, Release and Status are correctly limited to that assignment's editors. The leaks are AI regrading by any instructor (G-5) and the legacy file grade endpoint (G-9).
- **Is Release All / Finalize enforced everywhere?** The ranking, `format_data`, API fields, status, list rows and websocket are gated. Filters and aggregates are not (G-1).
- **File uploads?** Extension and size are validated server-side from `ProblemFileConfig`. The storage key is generated, so path traversal is not possible. The download is served `as_attachment` with `application/octet-stream`. Who can download is decided by `can_see_detail`, which has the G-6 issue.
