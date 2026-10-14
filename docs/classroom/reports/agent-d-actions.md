# Teacher and admin actions in a real browser (agent D, 2026-10-14)

The click-through crawl did not press save, upload, import, export or delete buttons. This pass did. Each action was run in headless Chromium (Playwright) on a dev server with the seeded Classroom (`seed_dev_data`), as prof_li (instructor), ta_wang (class TA), admin and the students ab101 to ab105. Each result was checked on the page and in the database. Downloads were caught with Playwright's download event and opened: the CSV files were parsed, the ZIP files listed and read, the PDF passed through `pdftotext`, and `word/document.xml` was read from the DOCX.

**Final run:** 10 flows, 156 checks, all pass. The database was restored from the seed snapshot before each flow. No JavaScript exceptions and no 5xx.

| Flow | Checks | What it does |
|---|---|---|
| roster | 36 | Course CSV import (Canvas roster and plain CSV, update on and off, into a class, bad file). Class created with a CSV. Import CSV on the class edit page. Members removed and added (select2) and saved. Students tab. Class deleted. Course created, edited and deleted. Course join requests (approve and reject from the row, bulk approve of a rejected one). Kick from a course. Class join request and approval. TA: class import allowed, course import refused. |
| questions | 35 | One question of each type from `/problems/new/`: single choice with an image and a PDF in the statement, multiple choice, true/false, fill-in-the-blank, short answer, file upload, and code with a test-data zip and paired cases. Edit ("Save and continue editing"). Clone. Bulk delete through the confirmation dialog. Delete from one row. Also run as admin. |
| excel | 13 | Import Excel dialog. Template download. Wrong file type. Upload of an `.xlsx` with 2 MCQ, true/false, fill-in-the-blank and short answer. Preview. Create all. Options, correct answers and course checked in the database. |
| assign | 26 | Create an assignment: course, class, "Quiz" preset tag, a new tag, 3 library problems, reorder by drag, remove one. Database order, tags and scope checked. Reopen. Every visibility mode (public, course, private with an assignee, hidden). Access code (set by admin) kept when the instructor saves. Student join with a wrong code, then the right one. Clone from the list. Delete from the list and from the editor. |
| grading | 13 | Save a manual grade. Release All, with the database and the participation score checked. Rejudge. Export Scores CSV. Export joined scores with a Canvas gradebook. Export Submissions ZIP. PDF and Word export (pandoc + lualatex). |
| student | 15 | `.exe` refused, 21 MB file refused (20 MB limit), valid PDF, resubmit, student downloads own file. MCQ resubmit. Comment posted, edited, then hidden by admin. Ticket created, then replied to and closed by admin, then reopened by the student. |
| materials | 7 | Class Materials as TA: upload 2 files, new folder, upload into the folder. Student downloads. Bulk delete of a file and a non-empty folder. |
| testdata | 4 | Classic test-data page: zip upload, 2 cases, save. `init.yml` checked. |
| userdata | 4 | "Download your data": prepare, then download the ZIP. Needs `DMOJ_USER_DATA_DOWNLOAD` and a Celery worker. The dev server here ran Celery eagerly. |
| rejudge | 2 | Admin rejudge on the grading page with no judge online. The request is queued and an MCQ is regraded at once. |

## Bugs found and fixed

| Area | Problem | Fix |
|---|---|---|
| Excel import | **"Create all" always failed with "Draft save timed out".** The per-question editor on the preview page is an iframe, but every page is sent with `X-Frame-Options: DENY`, so the frame was blank. | `ProblemImportItemEditorView` allows same-origin framing (`xframe_options_sameorigin`). |
| Excel import | "Download the Excel template" pointed at an admin URL, so instructors (not staff) landed on the admin login. | New `/problems/import-excel/template/` for anyone who may import. It reuses the admin's template builder. |
| Excel import | A refused upload from the dialog (wrong file, missing columns) showed a page of raw JSON. | A plain form post goes back to the problem list with the error message. XHR callers still get JSON. |
| Export Submissions ZIP | File-upload answers were left out (they have no source text). A lab-report assignment exported an empty ZIP. | Each upload is added as `<user>/<problem>-<submission>-<original name>`. |
| PDF/Word export | Multiple-choice questions were printed **without their choices**. Points came from the problem instead of the assignment. Times were raw UTC with microseconds. | Choices are printed as A, B, C (never marked correct), with "Select all that apply" when several answers are allowed. Points come from the assignment. Times show as `YYYY-MM-DD HH:MM TZ` in the teacher's time zone. |
| Joined Canvas export | Rows without a DKUOJ participant got an empty score cell. That erased the Canvas "Points Possible" row and grades of students graded elsewhere. | When the column already exists, those cells keep their value. |
| Assignment page | The file input of the score-import dialog used `\|tojson` inside a double-quoted `onchange`. Choosing a file threw "Unexpected end of input" and the file name never showed. | Single-quoted attribute. |
| Assignment clone | The copy lost its classes, so a class-scoped assignment became visible to the whole course. It also lost its joining organizations, so with "limit joining" on, nobody could join. | `classes` and `join_organizations` are copied. |
| Course page | The course CSV import page (`/organization/<id>/import/users`) had no link anywhere. | Course admins get "Import students (CSV)" in the course action panel. |
| Roster import | In a Canvas roster, a row with an empty Email cell created the account with no e-mail, even though `netid@duke.edu` was filled in. | A blank Email falls back to the other e-mail column for that row. |
| Roster import | "Updated: N" counted accounts the import did not change: protected accounts, or the same data again. | Only accounts that really changed count as updated. The others count as skipped. |
| Problem creator | A new single or multiple choice question started with the options "HTTP" and "TCP" filled in as real text. | New questions start with two empty options. |
| Editor uploads | The upload returned an absolute URL. That baked the uploader's host and scheme into the statement: `http://` behind a TLS proxy is blocked as mixed content, and a different hostname breaks the link for students. | The link is site-relative (`/widgets/files/<uuid>/`). The PDF export already resolves this path. |
| File answers | "File is too large (22020105 bytes); the limit is 20971520 bytes." appeared under a page that says "Max size: 20 MB". | Sizes are given in MB ("21.0 MB ... 20.0 MB"). |
| Course form | The description is validated as required but was not marked with `*`. | Marked. |
| seed_dev_data | The finalized quiz was recomputed before it was finalized, so every score was 0 and the score exports showed 0 for everyone. | Recompute after finalizing, as the finalize task does. |

## Tests

- New `judge/views/tests/test_teacher_actions.py`, with 17 tests. Each one was run against the code before its fix and failed there; this was checked by reverting the fixed files to HEAD.
- `test_editor_attachment` now also asserts that the link is relative.
- Run together: `test_click_through_fixes test_teacher_actions test_organization_courses test_problem_list test_problem_create_v2 test_contest_v2 test_problem_import test_problem_import_creation test_editor_attachment test_file_upload utils.test_import_export utils.test_contest_export`: **315 tests OK**. The full suite was not run (shared CPUs).
- flake8 is clean on every changed Python file. No migrations were added.

## Left as they are (product decisions or upstream DMOJ behaviour)

1. **Instructors cannot set access codes, clone or delete assignments.** The Instructors group has no `contest_access_code`, `clone_contest` or `delete_contest`, so the anti-cheating section, the Clone action and the Delete action are hidden for them, even on their own assignments. This pass did these steps as admin. Should instructors get these on their own assignments?
2. **Class TAs cannot grade or export.** ta_wang gets a 404 on the grading page and a 403 on the exports for their own class's assignments. TAs can manage class members, materials and CSV imports.
3. **Rejudge on the grading page** is disabled for instructors; the tooltip says "Requires staff + rejudge permission". The same tooltip also shows for admins, for whom the button works.
4. **No multi-select delete on the assignment list.** This is deliberate (an existing test asserts it, matching the admin's "no batch delete"). Delete is per row, with a confirmation dialog.
5. **Classic test-data page:** uploading only a zip saves the empty extra row as case #1 and shows "Points must be defined for non-batch case #1" until cases are filled in. This is DMOJ formset behaviour. The new problem editor does not have this issue.
6. **Comments and tickets (Martor):** "Post!" and "Create" are enabled only on the Preview tab. This is DMOJ behaviour.
7. **Course member "Kick"** appears only on row hover, so it cannot be reached on touch screens.
8. **Times:** `DEFAULT_USER_TIME_ZONE` is still `America/Toronto`. A DKU deployment should set `Asia/Shanghai`; otherwise teachers who never set a time zone enter assignment times 12 hours off.
9. **User data download** is off by default (`DMOJ_USER_DATA_DOWNLOAD = False`) and needs a Celery worker. It works when enabled.
10. The submissions ZIP stores MCQ and fill-in-the-blank answers as their JSON source (`{"selected": [2], "choices": [...]}`). The JSON is readable but not formatted for people.

## How to repeat

The Playwright scripts are in the agent scratch directory (`/tmp/claude-0/d/*.js`, shared helpers in `lib.js`, runner `runall.sh`). They are not in the repository. They expect a seeded database snapshot and a dev server on port 8104. They can be moved under `classroom/e2e/` if you want them kept.
