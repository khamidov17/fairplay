# P1 report: problem authoring, import/export, files, rendering

Branch `p1-authoring`, worktree `/home/claude/wt/authoring`. Findings come from `docs/classroom/REVIEW.md` and `review-details/authoring.md` (AI-7 is in `review-details/ai_analytics_ide.md`).

Every finding has at least one regression test. To check that the tests fail before the fix, I ran them against an export of the unfixed `HEAD` with the new test files copied in (`test_classroom_authoring_pre` DB): all of them failed or errored there. The exceptions are the positive controls and the "safe already" checks, which are listed per finding below.

## Per finding

| ID | Status | Summary |
|---|---|---|
| AUTH-01 | fixed | The PDF/Word export is limited to editors of the assignment. The empty-description bug is fixed. |
| AUTH-02 | fixed | No test-case download while the problem is in an upcoming or running assignment, or in an exam with unfinalized hidden results. Results must also be visible to the user. |
| AUTH-03 | fixed | Import ZIPs moved to private storage with random keys. They are deleted after creation or when the draft is discarded. Legacy files move in a migration. DEBUG MEDIA blocks the old prefix. |
| AUTH-04 | fixed | All inline-script JSON goes through `script_json` (escapes `< > &` and U+2028/2029). That covers 15 sinks, not just the 2 named. |
| AUTH-05 | fixed | create_v2 refuses to change the description or translations of a full-markup problem without `problem_full_markup`. |
| AUTH-06 | fixed (runtime-verified) | Lua filter, pandoc `--sandbox` (PDF), lualatex `-no-shell-escape`, `openin_any=p`/`openout_any=p`, escaped titles, no CDN/network. |
| AUTH-07 | fixed | `download_tester` checks problem access like the problem page. It is also held back during an open assignment. |
| AUTH-08 / AI-7 | fixed | Student short answers are rendered with the sanitized `default` style. This also fixes the grade desk fallback. |
| AUTH-09 | fixed | `is_public` is decided on the courses that are really attached. An unknown id is rejected. |
| AUTH-10 | fixed | Instructors can attach only courses they teach. Courses the problem is already in are kept. |
| AUTH-11 | fixed | The nested DOCX is capped: 10 MB per member, 32 MB total, ratio 100. The size is checked again on the bytes produced. |
| AUTH-12 | fixed | `import_users_csv_old` and `export_contest_submissions_old` are deleted. They were unused. |
| Exam integrity | checked and tested | No other statement path in my area serves an upcoming assignment's private problem to a student (see below). |

### AUTH-01: assignment PDF/Word export

- **What changed.** `judge/views/contests.py`: `ContestExportPDF` and `ContestExportWord` were two copies of the same ~600-line view. Both now call `_contest_document_export()`. It returns 404 (the same answer as an unknown key) unless `contest.is_editable_by(user)` (`edit_all_contest`, author or curator). The HTML builder and pandoc runner moved to the new `judge/utils/contest_export.py`.
- **Description bug.** `description = problem.description or ''` is now evaluated per problem. Before, an empty description reused the previous problem's text, or raised NameError on the first problem.
- **Behaviour changes.**
  - The unused `<style>` block (pandoc ignores CSS) is gone.
  - The KaTeX CDN `<link>`/`<script>` tags and the print-button script are gone. They were dead code in a pandoc conversion and a network reference.
  - Errors return a generic JSON message. The 500 response no longer echoes pandoc/LaTeX stderr, which can contain file contents.
- **Tests** (`judge/views/tests/test_authoring_security.py`, `ContestDocumentExportAccessTest`):
  - `test_student_cannot_export_upcoming_assignment`
  - `test_anonymous_cannot_export_public_assignment`
  - `test_editor_can_export_and_empty_description_is_not_reused`
  - `test_first_problem_without_description_exports`
  - `test_titles_are_escaped`

### AUTH-02: test-case download

- **What changed.** `judge/views/problem_data.py`, `download_test_case`. For users who cannot edit the problem, the download now also requires:
  - `submission.results_visible_to(user)`, and
  - not `problem_in_protected_contest(problem)`. That is true while any assignment containing the problem is upcoming or running (`end_time > now`), or is an exam (`has_hidden_results`) whose hidden results are not finalized.
- **Decision.** I picked the safest reading: the download is closed during any open assignment with the problem, and not only when results are hidden. Otherwise a practice submission outside the exam, or a code problem whose results are shown, still leaks the hidden tests one case at a time. As a consequence, a teacher who enables "view test cases" for a homework problem gets the downloads only after the homework ends. If that is too strict, the switch is one line in `problem_in_protected_contest` (or make it a setting).
- **Tests** (`HiddenTestMaterialDownloadTest`):
  - `test_no_download_while_problem_is_in_running_assignment`
  - `test_no_download_while_problem_is_in_upcoming_assignment`
  - `test_no_download_when_exam_results_are_hidden`
  - Controls: `test_practice_download_allowed_without_assignment`, `test_editor_keeps_access_during_assignment`. They pass before and after.

### AUTH-03: import test-data ZIPs

- **Storage and key.**
  - `ProblemImportItem.test_data_zip` now uses `storage=submission_storage`, the private S3/Box store (local `submission_files/` in dev), which is never MEDIA.
  - It uses `upload_to=problem_import_test_data_upload_to`, which builds a random key `import_test_data/YYYY/MM/<draft>/<uuid>.zip` (`generate_problem_import_key` in `judge/storage/resilient.py`).
  - Changed files: `judge/models/question.py`, `judge/storage/__init__.py`.
- **Deletion after creation.** The ZIP is deleted on commit once the problem is created (`_discard_test_data_zip_on_commit` in `judge/views/problem_import.py`).
- **Deletion with the draft.** The existing `post_delete` signal removes it when the draft or item is deleted.
- **Re-upload.** Replacing the ZIP in the editor now deletes the previous file.
- **Migration** `judge/migrations/0220_problem_import_zip_private_storage.py`:
  - AlterField (max_length 255).
  - RunPython: moves legacy `media/problem_import_test_data/*` files of open drafts into the private store, and deletes the ones whose problem was already created.
- **DEBUG.** `dmoj/urls.py` returns 404 for `<MEDIA_URL>problem_import_test_data/...` before the DEBUG static route.
- **Deployments.** There is no nginx config in the repo. Production nginx should also deny `/media/problem_import_test_data/` until the migration has run, and the directory can be deleted after.
- **Tests** (`ImportTestDataStorageTest`):
  - `test_zip_is_private_and_randomly_named`
  - `test_zip_deleted_after_problem_is_created`
  - `test_debug_media_does_not_serve_legacy_import_zips`
  - Control: `test_zip_deleted_when_draft_is_discarded`.

### AUTH-04: JSON inside `<script>`

- **Helper.** New `judge/utils/safe_json.py`: `script_json(value, **dumps_kwargs)`, which is `json.dumps` with `<`, `>`, `&`, U+2028 and U+2029 escaped, returned as a SafeString.
- **Sinks fixed.** Every `json.dumps` that reaches a template unescaped (`|safe` or `mark_safe`) now uses it:
  - `problem_create_v2.py`: `edit_data_json`, `problem_types_json`, `languages_json`, `checkers_json`, `harness_languages_json`, `current_profile_json`.
  - `problem_import._safe_json`.
  - `admin/contest.py` and `admin/problem.py`: `simple_preset_defaults`.
  - `submission.py`: `results_json`, `results_colors_json`.
  - `problem_data.py`: `valid_files_json`, which holds test-file names from an uploaded ZIP and is attacker-controlled; `language_ace_map`.
  - `contests.py` stats.
  - `user.py`: `rating_data`, which holds contest names written by instructors: stored XSS on every user page with a rating; `submission_data`, `submission_metadata`.
  - `contest_analytics.py`: three values that only escaped `</`.
- **Checked and safe.**
  - Jinja `|tojson` (escapes `<>&'`).
  - `json_script` in `organization.py`.
  - The `|json` filter, used only in auto-escaped attributes.
  - `ticket.py` id lists: integers, auto-escaped.
- **Tests** (`InlineScriptJsonTest`):
  - `test_create_page_escapes_problem_types`
  - `test_edit_page_escapes_edit_data`
  - `test_user_page_escapes_contest_names_in_rating_data`
  - `test_script_json_escapes`

### AUTH-05: full-markup statement in create_v2

- **What changed.** `ProblemCreateV2View._check_full_markup_text` runs before any edit save. If the problem `is_full_markup` and the user lacks `judge.problem_full_markup`, any change to `description` gets a 403. So does any change to the translation set (language, name or description) when `translations` is sent. This mirrors `ProblemAdmin.get_readonly_fields` and `ProblemTranslationInline`.
- **Comparison.** Line endings and surrounding whitespace are normalized before comparing. An unchanged description passes, so other fields stay editable.
- **Uncertain.** If the rich editor re-serializes an untouched full-markup description into different Markdown, such a user gets a 403 and cannot save the problem from create_v2. That matches the admin, where the description is read-only for them.
- **Tests** (`ProblemCreateV2PermissionTest`):
  - `test_full_markup_description_is_read_only_without_permission`
  - `test_full_markup_translations_are_read_only_without_permission`
  - Controls: `test_full_markup_problem_other_fields_still_editable`, `test_full_markup_permission_allows_description_change`.

### AUTH-06: pandoc/lualatex hardening

- **Runtime check.** I confirmed the hole first, with pandoc 3.1.3 and the old arguments:
  - `$\text{\input{/path}}$` and `<script type="math/tex">\text{\input{/etc/hostname}}</script>` printed the file into the PDF.
  - `\directlua{io.open(...)}` did too, even with `openin_any=p`.
- **Fix** (`judge/utils/contest_export.py` + `judge/utils/pandoc_export_filter.lua`):
  - **Lua filter on the pandoc AST.** It catches math from `$...$`, `\(...\)`, MathJax script tags and raw HTML alike. A Math element is turned into literal code text if its control words match a file, Lua, definition, catcode, `pdf*`, font, verbatim or graphics blocklist, if a name contains `@`, if it uses the `^^` notation, or if it opens a blocked environment. Raw TeX becomes code text. Normal math such as `\frac`, `\sqrt` and `\alpha` is kept.
  - **Images.** They are kept only if the server resolved them beforehand to a local file, either under `MEDIA_ROOT` or an `EditorAttachment` uuid, and they are put into the mediabag by the filter. Every other image becomes its alt text, so pandoc never fetches a URL or reads a path from the statement.
  - **PDF.** pandoc runs with `--sandbox`. lualatex runs with `-no-shell-escape` and the kpathsea env `openin_any=p`, `openout_any=p`, `shell_escape=f`, so `\input` of an absolute path fails even if something passes the filter.
  - **Word.** pandoc 3.1.3 cannot find its own docx data files under `--sandbox` (it fails with "Could not find data file data/data/docx/[Content_Types].xml"). Word therefore relies on the filter. The docx writer runs no TeX.
  - pandoc is called through `subprocess` in a private temporary directory, with a 180 s timeout and a controlled environment, instead of `pypandoc.convert_text`.
  - Titles, the assignment description and points are HTML-escaped.
- **Residual risk.** The filter is a blocklist. lualatex keeps `\directlua` (`--safer` breaks luaotfload), so a TeX construct that spells a primitive without its name and without `\csname` or `^^` would bypass the filter. I know of none, and catcode, def and let changes are blocked. Only editors can export, which narrows this to an instructor attacking the server. Moving the export to a Celery worker without secrets (P4) would remove it.
- **Tests** (`judge/utils/tests/test_contest_export.py`; skipped where pandoc, lualatex or pdftotext are missing):
  - `PandocFilterTest.test_dangerous_tex_never_reaches_latex`
  - `PandocRuntimeTest.test_pdf_export_does_not_expand_input_of_server_files`: real PDF; the secret marker file is absent from the text and the bytes.
  - `test_word_export_does_not_embed_server_files`
  - `test_lualatex_cannot_read_absolute_paths`: defence in depth.
  - `test_site_media_images_are_kept`
- **Speed.** About 15 s for the module.

### AUTH-07: tester download

- **What changed.** `download_tester` returns 404 unless `problem.is_accessible_by(user)`. For non-editors, it also returns 404 while `problem_in_protected_contest`, because the harness is hidden test code.
- **Tests:** `HiddenTestMaterialDownloadTest.test_tester_needs_problem_access` (private problem, other-course org-private problem). The tester is also covered by the running and upcoming tests above.

### AUTH-08 / AI-7: student short answers

- **What changed.** `judge/views/problem.py`, `get_workspace_answer_display`. It renders the student's text with `render_preview_html(text, 'default')` (`safe_mode`, HTML escaped), never with `problem.markdown_style`.
- **Grade desk.** The grade desk's server fallback (`item.html` in `templates/contest/grade.html`, built by `utils/contest_grade.py` from this function) and the workspace both use this output, so both are fixed. No template change was needed.
- **Tests** (`StudentAnswerRenderingTest`):
  - `test_full_markup_problem_does_not_render_student_html`
  - `test_normal_problem_does_not_allow_student_style`

### AUTH-09 and AUTH-10: courses in create_v2 and the Excel import

- **New helper.** `assignable_organizations(user, problem=None)` in `problem_create_v2.py` returns the courses where the user is a course admin, a course instructor or the admin of a class in the course. `edit_all_organization` gives every course. On an edit, the courses the problem is already in are also included.
- **Save path.** `_payload_organizations` validates every submitted id against that set: a malformed id gives a 400, a disallowed or unknown id gives a 403. Both `_resolve_public` (AUTH-09: it now uses the courses that are really attached) and `_attach_orgs` use it.
- **Offered lists.** The editor's course list is scoped the same way. So is the Excel import's organization matching (`_resolve_import_organizations`). The import's create path turns the PermissionError into the import's normal ValueError message.
- **Decision.** "Manage" includes course instructors and class admins (`Organization.profile_teaches_course`), not only `Organization.admins` as in the legacy admin Excel path. A class admin must be able to attach their course, or the class picker could never apply.
- **Not changed.** `ProblemCreateV2OrganizationView` still lets any instructor create a new course and become its admin. That is a courses-area or product question.
- **Existing tests updated.** These encoded the old behaviour:
  - `test_problem_create_v2.py`: `test_course_dropdown_lists_all_courses` became `test_course_dropdown_lists_taught_courses`, and `test_create_private_problem_can_attach_any_course` became `..._taught_course`.
  - `test_problem_import.py`: three organization tests now make the creator a course admin.
- **New tests:**
  - `test_unknown_course_id_does_not_make_problem_public`
  - `test_cannot_attach_course_not_taught`
  - `test_can_attach_taught_course`
  - `test_existing_foreign_course_is_kept_on_edit`
  - `test_problem_import.py::test_upload_does_not_match_course_the_creator_does_not_teach`

### AUTH-11: nested DOCX

- **What changed.** `judge/utils/excel_embedded_documents.py`: `_validate_docx_archive` runs before any member is read (10 MB per member, 32 MB total, compression ratio at most 100), and `_read_docx_member` re-checks the size of the bytes actually produced. This applies to `document.xml`, the relationships part and images.
- **Tests** (`test_problem_import_parsing.py`):
  - `test_embedded_docx_zip_bomb_is_rejected`: a 64 MB `document.xml` in a DOCX of less than 4 MB.
  - `test_embedded_docx_with_highly_compressed_part_is_rejected`
  - Control: `test_embedded_docx_with_normal_compression_is_read`.

### AUTH-12: legacy commands

- **What changed.** Both `_old` commands were deleted. Nothing referenced them in code, docs, scripts or CI.
- **Tests:** `LegacyCommandsRemovedTest.test_legacy_commands_are_gone`.
- **Operational follow-up.** Ask whether accounts were ever created with password `123456`.

### Exam integrity: other statement paths in my area

I looked for any path that serves an assignment's problem statement to a non-editor before the start.

- **Problem page** (`ProblemMixin`, workspace and `?legacy=1`), problem PDF (`ProblemPdfView`) and the assignment preview drawer: all use `Problem.is_accessible_by`. For a private problem, its only contest path is `current_contest`, which requires a joined participation, and joining requires `contest.started` for non-editors/testers. Safe already, and now covered by a test.
- **Tester, test-case download, PDF/Word export:** fixed above.
- **Markdown preview endpoints** (`/widgets/preview/*`): they render text the caller posts and never load stored problems.
- **Import previews, draft editor and draft images:** owner-only through `_draft_for`.
- **Attachments:**
  - Editor attachments: unauthenticated by unguessable uuid, by design. The uuid appears only in the statement.
  - Martor images: random uuid names under MEDIA.
- **Tests** (`UpcomingAssignmentStatementTest`):
  - `test_no_statement_before_start` covers the problem page, the legacy page, the tester, the PDF and Word exports and the preview drawer. Before the fix, the tester and both exports failed this test.
  - Control: `test_teacher_still_sees_statement`.
- **Not in my area.** A *public* problem used in an upcoming exam stays visible on its problem page (G-7, grading area). `views/grade.py` (practice grade) also renders statements; that is the grading area.

## Files touched outside my area

- `judge/views/contests.py`: only the two export views. The export code moved out, and `stats` uses `script_json`.
- `judge/views/user.py`, `judge/views/submission.py`, `judge/views/contest_analytics.py`, `judge/admin/contest.py`, `judge/admin/problem.py`: one-line `json.dumps` → `script_json` changes for AUTH-04, which asked for all sinks.
- `judge/views/problem.py`: one line, for AUTH-08.
- `judge/storage/resilient.py`, `judge/storage/__init__.py`: new key generator.
- `dmoj/urls.py`: DEBUG MEDIA guard.

## Tests and checks

- Targeted modules: green.
- Full suite (`bash classroom/scripts/dev-test.sh judge`, serial, locmem, DB `test_classroom_authoring`): **1678 tests, OK, 1 expected failure** (baseline 1637 + 41 new).
- `flake8` on every changed Python file: clean.
- `makemigrations --check`: no changes. The new migration is `0220_problem_import_zip_private_storage` (depends on 0219; renumber on merge if needed).
- **Test-isolation pitfall I hit.** `override_settings(DEBUG=...)` resets Django's template engines. django-jinja then pops the `debug` option, so `response.context` is `None` for every later test (about 70 failures in my first full run). The urls test patches `settings.DEBUG` directly instead. Keep this in mind for other P1 tests.
- **Noise.** "Pkg: FLAGS_MISMATCH" lines in the output come from lualatex/pdftotext in the runtime export tests. They are harmless.
