# Agent A: teachers finalize their own exams, and the background jobs running for real

Branch `agent-a-finalize-jobs` (from `fafbc74`). No migrations.

## How it was tested

Own dev DB `classroom_a` (migrate, `loaddata navbar language_small demo`, `seed_dev_data`), dev server on
127.0.0.1:8101, and a real Celery worker
(`celery -A dmoj_celery worker --loglevel=info --concurrency=1`) on a private Redis (port 6391), so no
other agent's task reached it. In the gitignored `local_settings.py`: `ASSIGNMENT_HARVEST_ENABLED = True`,
`DMOJ_USER_DATA_DOWNLOAD = True`. Every flow was driven in Chromium through Playwright as the real user
(login form, buttons, confirm dialogs), then checked in the worker log and in MariaDB.

## Task 1: Finalize / Un-finalize for the assignment's own editors

Before: the editor's Finalize / Un-finalize buttons and `ContestFinalizeView` required
`judge.edit_all_contest` (site contest admin), so an instructor could not finalize their own exam.

Now both follow the rule for editing the assignment, `Contest.is_changeable_by`: `edit_own_contest`
plus author/curator, or `edit_all_contest`. The Django admin finalize/un-finalize actions already checked
`ContestAdmin.has_change_permission`, which is the same rule, so they are unchanged. A test pins them too.
There are no other entry points: grep for `results_finalized` / `finalize_contest_results` finds only
the editor view, the admin, the task and the seed.

Live check (`cs101exam1`):

| user | editor | button | POST finalize |
|---|---|---|---|
| prof_li (author, instructor) | 200 | yes | 200 `{"ok": true}`, task ran |
| ta_wang (class admin) | 403 | no | 403 |
| ab101 (student) | 403 | no | 403 |

Tests (`judge/views/tests/test_teacher_finalize.py`):
- the author sees the button and can finalize and un-finalize (POST works, the task is queued on commit);
- a non-author instructor, a curator without `edit_own_contest` and a student get 403 and no button;
- a curator who has `edit_own_contest` (a co-editor) may finalize;
- in the admin, the author may finalize and a grading-only curator gets 403.

One old test asserted the previous rule (an `edit_own_contest` curator refused). It now asserts that this
curator is allowed, and a new case checks that a curator without `edit_own_contest` is refused.

## Task 2: jobs run end to end against a real worker

### a) Finalize and un-finalize (`finalize_contest_results`, `unfinalize_contest_results`)

ab103 joined the running exam and answered through the workspace (MCQ wrong, fill-in-the-blank right).
Before finalize, the student saw "Results hidden" on the submission page and in the workspace, the
ranking showed 0 for everyone (teacher view), and `format_data` had `"hidden": true`. prof_li then clicked
Finalize in the editor and got the toast "Finalizing — results will appear shortly.". The worker logged
`finalize_contest_results ... succeeded in 0.17s: 4`.

Afterwards:
- DB: `results_finalized=1`, scores recomputed (ab101 5, ab103 5, ab102 0);
- the ranking showed the per-problem scores;
- the submission page showed "RESULT WA POINTS 0/5";
- the workspace (`?submission=13`) showed "Result: WA Score: 0 / 5";
- the assignment page's AC rate updated.

Un-finalize: `unfinalize_contest_results ... succeeded: 3`, everything was masked again and the scores
went back to 0 / hidden.

After the exam ended and was finalized, the student ranking showed every row, including answers that were
collected automatically.

**Bug fixed:** a multiple-choice answer's submission page showed nothing once its result was visible.
There were no test cases and no Result/Points summary, because the MCQ branch of
`submission/status-testcases.html` was empty. This was true for every MCQ answer, finalized exam or not.
The page now shows Result + Points. Test: `FinalizedAnswerOnSubmissionPageTestCase`.

### b) Auto-submit (harvest)

The switch is `ASSIGNMENT_HARVEST_ENABLED` (settings, default False). With it on:

- **Leave:** ab104 picked answers in the homework without submitting. The workspace autosave POSTed
  `/contest/cs101hw1/draft/...` (200) and two `AssignmentDraft` rows existed. After Leave → overlay →
  confirm, the drafts were marked `harvested_at` and submissions 15 and 16 were created and judged (AC 5,
  AC 5). Leave harvests synchronously, not through Celery.
- **Deadline sweep:** ab105 drafted both exam answers. prof_li moved the deadline to +3 min in the editor,
  which stored `harvest_scheduled_task_id`. At the deadline the worker logged
  `harvest_contest_drafts ... : 1` then `harvest_participation_drafts ... : 2`, and submissions 17 and 18
  appeared at 08:46:00.
- **Post-deadline auto-grade (`auto_grade_contest`):** I set the homework deadline to +2 min, auto-grade
  delay 1 min, and corrected the MCQ answer key in between. The task ran at end+1 min:
  `succeeded: 6`. ab103's answer was re-graded WA → AC 5 with the corrected key.

**Bug fixed: ETA tasks multiplied.** With Redis as broker, a task with an ETA stays unacknowledged in the
worker and Redis re-delivers it every `visibility_timeout` (default 1 h). I reproduced this with a real
worker and a 60 s timeout:
- the deadline harvest was received 3 times and ran 3 times;
- the 2-hour auto-grade task was received again every minute.

In production the impact is:
- a homework due in a week gains a copy of its auto-submit task every hour (~170 copies);
- permanent assignments (end = now + 100 years) gain copies without limit;
- each copy runs at the deadline. The harvest is idempotent, so this costs extra work only. The
  auto-grade re-judges every objective answer once per copy.

The fix: deadline tasks are scheduled at most `CLASSROOM_ETA_HORIZON` (45 min, below the explicit
`CELERY_BROKER_TRANSPORT_OPTIONS` visibility timeout of 3600 s) ahead, using `judge.utils.celery.eta_hop`.
- A hop that fires before its deadline and is still the task id stored on the row schedules the next
  hop. A stale hop (replaced after a deadline change, with its revoke lost) stops.
- `auto_grade_contest` no longer grades when a scheduled run fires early, or after auto-grade was switched
  off (−1).
- Called directly (tests, admin shell), it behaves as before.

Live re-check with a 40 s horizon and a 60 s timeout:
- one hop every 40 s, each with a new id, with no re-deliveries;
- a teacher edit revoked the current hop (`Discarding revoked task`);
- exactly one fan-out at 09:14:00 and one grading run at 09:18:00.

Tests: `judge/views/tests/test_deadline_task_hops.py` (11). Two existing scheduling tests now pin a
deadline within one hop.

### c) Other tasks started from the UI

| task | trigger | result |
|---|---|---|
| `prepare_user_data` | student: Edit profile → Download your data → Prepare | Redirect to `/tasks/status/…` showing "Preparing your data... Applying filters 1 / 2", then back to the page with "Your data is ready!"; the zip downloaded (1.4 kB). Hidden exam verdicts are masked in it (`"result": null`). The worker logged `succeeded: 3`. If no checkbox is ticked, the page says "select at least one thing" (DMOJ behaviour). |
| `rescore_problem` | admin: problem → Manage submissions → Rescore all | Progress page, then "8 submissions were successfully rescored." The worker logged `succeeded: 8`. The message is printed twice on the page (cosmetic). |
| `rejudge_problem_filter` | admin: Manage submissions → Rejudge | Progress page, then "Successfully scheduled 8 submissions for rejudging.", `succeeded: 8`. |
| `batch_ai_grade`, `ai_rejudge_submission` | Grade page "Batch grading" / AI rejudge | Not reachable: the button only appears for problems with AI grading, and no LLM is configured locally. Not run. |
| `run_moss` | contest MOSS tab | The tab is hidden without `MOSS_API_KEY` (needs the internet). Skipped. |
| `mirror_to_backup` | admin stored files | Needs backup storage. Skipped. |

Instructors cannot open "Manage submissions": DMOJ requires `is_staff` and `rejudge_submission`. The Grade
page is their tool.

There were no errors or tracebacks in any worker log. The only warning is Celery's deprecation notice for
the `CELERY_RESULT_BACKEND` setting name.

### Seed data

**Bug fixed:** `seed_dev_data` created assignments with keys like `cs101_exam1`, which fail
`Contest.key`'s `^[a-z0-9]+$` validator. Saving any seeded assignment in the editor returned 400
"Contest id must be ^[a-z0-9]+$". The keys are now `cs101hw1`, `cs101exam1`, `cs101quiz1`,
`cs101quiz2`, `math201hw1`. **Scripts that use the old keys need updating.** Test:
`SeededAssignmentsAreEditableTestCase`.

Problem codes (`cs_mcq_loop`, ...) also break `Problem.code`'s validator (Django admin edit would refuse
them). The frontend problem editor does not run `full_clean`, so I left them alone to avoid breaking more
scripts.

## Tests run (all with my env, single modules)

- `test_teacher_finalize` + `test_contest_editor`: 177 OK.
- `test_teacher_finalize` + `test_hidden_results`: 33 OK.
- `test_deadline_task_hops` + `test_assignment_auto_submit` + `models.tests.test_assignment_harvest`
  + `test_contest_ai_grading` + `models.tests.test_exam_contest`: 142 OK.
- `test_teacher_finalize` + `test_click_through_fixes`: 26 OK.
- Each new test was run against the code before its fix and failed.
- flake8 is clean on every changed file.

## Left / uncertain

- The editor's Finalize button only shows a toast. It does not change to Un-finalize until the page is
  reloaded, and there is no progress display. The job takes under 1 s here, but a large class with code
  rejudges takes longer. Not changed, because reloading could lose unsaved editor edits.
- On a finalized exact-match fill-in-the-blank, the submission page shows a section titled "AI Feedback"
  and no points.
- When a student is not currently inside the exam, the exam's private problems do not appear in their
  "All my submissions" list, even after finalize (DMOJ filter on visible problems).
- During an exam with scoreboard "after contest", a student opening the ranking gets "No such contest"
  (DMOJ's 404 for a hidden scoreboard).
- `seed_dev_data` creates contests with the ORM, so no deadline tasks are scheduled for them. Run
  `manage.py reschedule_assignment_harvests` after seeding, or rely on the `harvest_assignment_drafts`
  cron sweep.
- Revokes of ETA tasks live only in worker memory, so after a worker restart a replaced hop can come
  back. Hops check their stored id, so it stops at once. Before the fix it would run at the old time.
- No Celery beat or cron is defined in `docker-compose.yml`. The `harvest_assignment_drafts` backstop
  must be added to the host crontab, as its help text says.
