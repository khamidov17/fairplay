# DKU OJ review: problem authoring, import/export, file storage

Reviewer scope: `judge/views/problem_create_v2.py`, `judge/utils/problem_create.py`, `judge/views/problem_import.py`, `judge/utils/import_export.py`, `judge/utils/excel_embedded_documents.py`, `judge/utils/simple_xlsx.py`, `judge/views/editor_attachment.py`, `judge/models/editor_attachment.py`, `judge/models/stored_file.py`, `judge/storage/*`, DKU changes in `judge/views/problem_data.py`, `judge/utils/problem_data.py`, `judge/views/problem_manage.py`, `judge/admin/problem.py`, `judge/utils/pdfoid.py` and the contest PDF/Word export in `judge/views/contests.py`, `judge/management/commands/export_contest_*` / `import_users_csv*`, `judge/utils/markdown_preview.py`, `judge/views/preview.py`.

Everything here comes from reading the code. Nothing was run against a live instance. I ran pandoc locally once, to confirm that TeX math passes through to LaTeX unchanged (AUTH-06).

## Findings summary

| ID | Sev | Conf | Title |
|---|---|---|---|
| AUTH-01 | Critical | Confirmed by reading | Any student who can see an assignment can export every problem statement to PDF/Word before it starts |
| AUTH-02 | High | Confirmed by reading | Students can download hidden test input and expected output during an exam, one test per submission |
| AUTH-03 | High | Likely (depends on DEBUG/nginx) | Excel-import test-data ZIPs are stored under public MEDIA with guessable names and are never deleted |
| AUTH-04 | High | Confirmed by reading | Stored XSS: create_v2 editor puts unescaped JSON (`edit_data_json`, `problem_types_json`) inside `<script>` |
| AUTH-05 | High | Confirmed by reading | create_v2 lets an editor without `problem_full_markup` rewrite the description of a full-markup (unsanitized) problem |
| AUTH-06 | Medium | Likely / needs runtime check | LaTeX injection and pandoc resource fetching in contest PDF/Word export (server file read, SSRF) |
| AUTH-07 | Medium | Confirmed by reading | Hidden harness ("tester") downloadable by any logged-in user, with no access check on the problem |
| AUTH-08 | Medium | Confirmed by reading | Students' short-answer text is rendered with the problem's markdown style (no sanitizer on full-markup problems) |
| AUTH-09 | Low | Confirmed by reading | `is_public` bypass: a non-existent organization id makes a problem globally public without `change_public_visibility` |
| AUTH-10 | Low | Confirmed by reading | create_v2 and Excel import let any instructor attach problems to any course |
| AUTH-11 | Low | Confirmed by reading | Zip bomb in a DOCX embedded in the Excel import (inner archive has no size or ratio limits) |
| AUTH-12 | Low | Confirmed by reading | `import_users_csv_old` command still ships with default password `123456` |

---

### AUTH-01: Problem statements can be exported before the assignment starts (Critical, Confirmed by reading)

- Files: `judge/views/contests.py:3040` (`ContestExportPDF.post`), `judge/views/contests.py:3650` (`ContestExportWord.post`). Routes: `dmoj/urls.py:315-316` (`/contest/<key>/export-pdf/`, `/export-word/`).
- What is wrong: the only check is `_find_contest(request, contest_key, private_check=True)`, which is `contest.is_accessible_by(user)`. There is no `is_editable_by`, no login requirement, and no check on start time or problem visibility. The view then renders the description of every `contest.contest_problems` entry into the document.

```python
class ContestExportPDF(ContestMixin, View):
    def post(self, request, *args, **kwargs):
        ...
        contest, exists = _find_contest(request, contest_key, private_check=True)
        if not exists:
            return contest
        contest_problems = contest.contest_problems.order_by('order').select_related('problem')
```

  By contrast, `ContestExportScores` and `ContestExportSubmissions` (`contests.py:4248`, `4300`) do check `is_editable_by`.
- Scenario: a student enrolled in the course sees an upcoming exam in the list, because `access_check` passes for course members of a visible contest. With their own CSRF token they send `POST /contest/<key>/export-pdf/` and get a PDF of every exam question days before the start. For a public visible contest, an anonymous user can do the same.
- Fix: require `contest.is_editable_by(request.user)` (or `is_changeable_by`) in both export views, and return 404 otherwise.

### AUTH-02: Hidden test cases leak during an exam (High, Confirmed by reading)

- File: `judge/views/problem_data.py:364-410` (`download_test_case`, DKU-added). Route: `dmoj/urls.py:215`.
- What is wrong: the view gates only on `problem.view_test_cases` (a per-problem flag) and `submission.can_see_detail(user)`, which is true for the submission's own author. It ignores contest state and `results_visible_to` (hidden or unreleased exam results). It returns the input and the expected output of the first non-AC case.

```python
if not submission.problem.view_test_cases: raise Http404()
if not submission.can_see_detail(request.user): raise Http404()
...
first_failed = submission.test_cases.exclude(status='AC').exclude(status='SC').order_by('case').first()
```

- Scenario: a teacher turns on "view test cases" for a practice problem and later reuses it in an exam. During the exam a student submits, downloads `.../test_case/<n>/out` for the first failing case, hard-codes that answer, resubmits, and gets the next case. Repeating this dumps the whole hidden test set and produces a full-score "solution". Results are hidden during the exam, but the download still works.
- Fix: refuse the download when `submission.contest_object` is set and the contest is still running or its results are hidden (`not submission.results_visible_to(user)`), or make the flag per-contest-problem.

### AUTH-03: Hidden test-data ZIPs from Excel import sit in public MEDIA (High, Likely)

- Files: `judge/models/question.py:49` (`test_data_zip = models.FileField(upload_to='problem_import_test_data/')`, default storage = `MEDIA_ROOT`), `judge/views/problem_import.py:588` and `:986`, `dmoj/urls.py:546-548`, `dmoj/settings.py:628-629`.
- What is wrong: the import stores the full test data (inputs and expected outputs) in the default storage under `media/problem_import_test_data/<original filename>`. The filename is predictable: `embedded.zip` for a directly embedded ZIP (`excel_embedded_documents._extract_embedded_file`), the OLE filename, or `test_data.zip`. Django adds a random suffix only on a name collision. With `DEBUG=True`, which is what the live site showed, `urls.py` serves `MEDIA_ROOT` at `/media/` without authentication. The standard nginx deployment also serves `/media/`. The draft's ZIP is not deleted after the problem is created (`_create_problem` deletes only `item.images`). It is removed only when the draft is deleted by `cleanup_problem_import_drafts`, which is a cron job.
- Scenario: a teacher imports an exam set whose code problem embeds `tests.zip`. A student fetches `https://oj/media/problem_import_test_data/tests.zip` (or `embedded.zip`, or `test_data.zip`) and gets every hidden test with its expected output.
- Fix: store `test_data_zip` in `problem_data_storage` or the private `submission_storage` with a uuid key (as `EditorAttachment` does), and delete it once the problem is created.

### AUTH-04: Stored XSS in the create_v2 editor via unescaped inline JSON (High, Confirmed by reading)

- Files: `judge/views/problem_create_v2.py:174` and `:203`, `templates/problem/create_v2.html:971` and `:977`.
- What is wrong: the template comment claims "View escapes <>& to \uXXXX so this is not a script-breakout vector", but only `current_profile_json` and `harness_languages_json` are escaped. `edit_data_json` and `problem_types_json` are plain `json.dumps` output inserted with `|safe` into a `<script>` block, and `json.dumps` does not escape `</script>`. The import editor uses `_safe_json` (`problem_import.py:270`) and is fine.

```python
context['edit_data_json'] = json.dumps(self._build_edit_data(problem))       # :174
'problem_types_json': json.dumps([{'name': t['name'], 'full': t['full_name'] or t['name']} ...])  # :203
```
```html
const EDIT_DATA = {{ edit_data_json|safe }};
const SERVER_PROBLEM_TYPES = {{ problem_types_json|safe }};
```

- Scenario 1 (instructor to admin): any user with `create_private_problem` sends `POST /problems/new/problem-type/` with `full_name = "</script><script>fetch('/admin/...')</script>"` (100 characters are allowed). From then on, every instructor or superuser who opens `/problems/new/` or any `/problems/edit/<code>/` runs that script in their own session.
- Scenario 2 (editor to co-editor/admin): put `</script><script>...` in the problem description, a translation, a clarification, the editorial, a test-case input or output (test data is read back into `edit_data_json` by `_read_testcases`), or a harness. Opening the edit page then runs the script. The markdown sanitizer is never involved here.
- Fix: escape with `.replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')` (or use `json_script`) for both values.

### AUTH-05: Full-markup description can be rewritten without `problem_full_markup` (High, Confirmed by reading)

- Files: `judge/views/problem_create_v2.py:870-877` and the `problem.description = payload.get('description')` assignments at `:1166`, `1482`, `1532`, `1569`, `1614`, `1672`. Compare `judge/admin/problem.py:627-630`.
- What is wrong: the classic admin makes `description` read-only when `obj.is_full_markup` and the user lacks `judge.problem_full_markup`. Full-markup problems render with style `problem-full` (`MARKDOWN_ADMIN_EDITABLE_STYLE`: `safe_mode: False`, no bleach). create_v2 only refuses to change the flag; it overwrites the description and translations unconditionally:

```python
elif bool(payload.get('is_full_markup')) != bool(problem.is_full_markup):
    raise PermissionError(...)
```

- Scenario: an instructor is co-author or curator of a problem that an admin marked full-markup (or has `edit_public_problem` and the problem is public). They save it from `/problems/edit/<code>/` with `is_full_markup: true` (unchanged) and a description containing `<script>`. Every student and admin who opens the problem runs the script.
- Fix: in `_create_*`, when `problem.is_full_markup` and the user lacks `judge.problem_full_markup`, reject any change to `description` or translation descriptions, as the admin does.

### AUTH-06: LaTeX injection and pandoc fetching in contest export (Medium, Likely / needs runtime check)

- File: `judge/views/contests.py:3480-3620` (PDF) and `~4100-4215` (Word).
- What is wrong: problem descriptions go through Python-Markdown and are then handed to `pypandoc.convert_text(..., extra_args=['--pdf-engine=lualatex', '--self-contained', '-f', 'html+tex_math_dollars+tex_math_single_backslash', ...])`. I confirmed locally that pandoc copies `$...$` math into the LaTeX source verbatim, so `$\text{\input{/abs/path}}$` becomes `\(\text{\input{/abs/path}}\)`. `lualatex` then reads that file into the PDF (TeX Live default `openin_any=a`), and `\directlua{...}` allows Lua `io.open`. Separately, `--self-contained` makes pandoc fetch any `<img src=...>`, which is an SSRF to internal hosts. `contest.name` and `contest.description` are also f-string-interpolated without escaping. Full compilation could not be finished locally because local TeX fonts are missing, so this needs a runtime check.
- Scenario: an instructor puts `$\text{\input{/app/dmoj/local_settings.py}}$` in a problem used in their contest and exports the PDF. The SECRET_KEY and DB/S3/Box credentials appear in the PDF. That leads to admin access, and to session forgery because SECRET_KEY is known.
- Fix: run pandoc/lualatex with `--sandbox` (pandoc 2.15+) and `-no-shell-escape`, set `openin_any=p`, and render math with KaTeX first or strip TeX control sequences; drop `--self-contained` or allow-list image hosts.

### AUTH-07: Hidden harness download has no access check (Medium, Confirmed by reading)

- File: `judge/views/problem_data.py:422-470` (`download_tester`), route `dmoj/urls.py:178`.
- What is wrong: `problem = get_object_or_404(Problem, code=problem)`, then only `if not problem.view_tester: raise Http404()`. There is no `is_accessible_by(user)` and no contest check. The model calls the harness "Hidden test code compiled/run with the student submission" (`models/problem_data.py`).
- Scenario: problem codes are sequential (`p000123`, generated by `generate_problem_code`). A logged-in student from any course enumerates `/problem/p0001../tester` and downloads harnesses for private course problems and for exam problems that are not yet visible.
- Fix: add `if not problem.is_accessible_by(request.user): raise Http404()` (plus the contest-visibility check used by problem detail).

### AUTH-08: Student short-answer HTML is rendered with the problem's markdown style (Medium, Confirmed by reading)

- File: `judge/views/problem.py:304-315` (`get_workspace_answer_display`), which calls `render_preview_html(text, submission.problem.markdown_style)` (`judge/utils/markdown_preview.py`).
- What is wrong: student text is rendered with `problem` (the staff bleach list, which allows `<style>`, `style=` attributes and `<noscript>`) or, on full-markup problems, with `problem-full` (no sanitizer). The resulting `item.html` reaches teacher views: `templates/contest/grade.html:976-980` falls back to `item.html` when the client-side `RichEditor.mdToHtml` returns nothing. The workspace page shows it only to the owner.
- Scenario: on a full-markup short-answer exam question, a student answers `<img src=x onerror=...>`. If the grading page uses the server HTML (RichEditor not loaded or failing), the script runs in the grader's session. On normal problems a student can still inject page-wide `<style>`, for example to hide or alter what the grader sees.
- Fix: render student answers with a student style (`'default'` or `'comment'`, `safe_mode: True`), never `problem.markdown_style`.

### AUTH-09: `is_public` bypass in create_v2 (Low, Confirmed by reading)

- File: `judge/views/problem_create_v2.py:713-724` plus `:825-830`.
- What is wrong: `_resolve_public` trusts `bool(payload.get('organizations'))`, but `_attach_orgs` then sets `Organization.objects.filter(pk__in=ids)`. With a non-existent id the set is empty, so `is_organization_private` becomes False and `is_public` stays True. The admin checks the real `obj.is_organization_private` (`admin/problem.py:1481`). The same path is reused by Excel import (`_apply_editor_configuration`).
- Scenario: an instructor without `change_public_visibility` saves `{"is_public": true, "organizations": [999999]}` and the problem becomes visible site-wide.
- Fix: decide `is_public` after `_attach_orgs`, using `problem.organizations.exists()`.

### AUTH-10: Instructors can attach problems to any course (Low, Confirmed by reading)

- Files: `problem_create_v2.py:180` (`orgs = Organization.objects...` all), `:825-830` ("Users who can edit the field may assign any course"), `problem_import.py:1243-1265`. The admin Excel path instead scopes to `Organization.objects.filter(admins=user.profile)` (`admin/problem.py:899-904`).
- Scenario: instructor A attaches a problem to course B, which they don't teach. B's students see it in their course problem list, and B's org admins get org-admin rights over it (clone, view). The fix is to apply the same scoping as `_visible_excel_organizations`. `ProblemCreateV2OrganizationView` also lets any instructor create new organizations and become their admin.

### AUTH-11: Nested zip bomb in an embedded DOCX (Low, Confirmed by reading)

- File: `judge/utils/excel_embedded_documents.py:215` and `:236`.
- What is wrong: the outer XLSX gets size and ratio checks (`simple_xlsx._validate_archive_members`), but the DOCX inside an OLE object (up to 4 MB) is opened with no checks: `ET.fromstring(archive.read('word/document.xml'))`, and `archive.read(image_path)` runs before the 10 MB check in `validate_docx_image`. About 4 MB of deflate can expand to about 4 GB in a web worker.
- Scenario: an instructor (or a stolen instructor session) uploads such an XLSX to `/problems/import-excel/`, and the gunicorn worker is OOM-killed. Fix: run `_validate_archive_members` and `_safe_archive_read` on the inner archive too. XXE is not an issue, because stdlib ElementTree/expat does not resolve external entities and expat ≥2.4 limits entity expansion.

### AUTH-12: Legacy user-import command defaults to password 123456 (Low, Confirmed by reading)

- File: `judge/management/commands/import_users_csv_old.py:29` (`default="123456"`). Any accounts created with it have a known local password, if password login is enabled. The new `import_users_csv` uses unusable passwords. Fix: delete the `_old` command, and check for users created by it.

### Checked and not reported
- Zip-slip: `_parse_zip_test_data` rejects `/` and `..`. Zip member names in create_v2 are used only as names inside the zip or as `basename`, and `ProblemDataStorage` (FileSystemStorage `safe_join`) raises on traversal.
- XLSX limits (2 MB upload, 8 MB total, 4 MB per part, 100× ratio) are sound. DOCX image paths are restricted to `word/media/`, and images are PNG/JPG/GIF only.
- `editor_attachment`: non-images are served `as_attachment` + `application/octet-stream`. Students may upload images only, rate-limited 10/min and 40/day. Downloads are unauthenticated, keyed by uuid, and that is by design. Note: `SecurityMiddleware` is not in `MIDDLEWARE`, so no `nosniff` header is sent (low impact, since types are image/* or octet-stream).
- Storage (`resilient.py`, `s3.py`, `box.py`, `mirror.py`): Box host key pinned with RejectPolicy, credentials only from settings, private bucket with no presigned URLs, sha256-verified mirroring. No secrets in code. The settings defaults are empty, and there is a guard for SECRET_KEY when `DEBUG=False`. With `DEBUG=True` (observed live) that guard is skipped, so check that the real SECRET_KEY is not the default `5*9f5q...`.
- Draft ownership: every import view goes through `_draft_for` (`owner=request.user.profile` and `create_private_problem`). The draft image view is owner-only.
- `problem_manage.py` and `utils/problem_data.py` diffs are the harness and on-commit judge notification, with no new exposure. The batch preview endpoint uses bleach style `problem`, as upstream does.

---

## How it works (for integrators)

**Authoring entry points (session + CSRF, JSON).**
- `/problems/new/` and `/problems/edit/<code>/` map to `ProblemCreateV2View` (`judge/views/problem_create_v2.py`). Access gate: `_can_create_any` = `create_private_problem`, or `is_staff` with `add_problem`. Edit additionally needs `problem.is_editable_by(user)` or authorship (`_get_editable_problem`).
- POST takes JSON `{type: programming|single|multiple|truefalse|blank|short|file, name, description, organizations[], classes[], is_public, problemTypes[], difficulty, testcases[], harness, solution, translations, ...}`. A programming problem with a ZIP uses multipart (`payload` JSON + `testcase_zip`).
- Each type maps to `Problem.submission_type` plus a config model: `ProblemMCQConfig`/`ProblemMCQOption`, `ProblemFillBlankConfig`/`ProblemBlank`, `ProblemShortAnswerConfig`, `ProblemFileConfig`, `ProblemCodeConfig` (AI rubric), `ProblemHarness`.
- Code test data is written to `problem_data_storage` (`DMOJ_PROBLEM_DATA_ROOT/<code>/`) as loose files plus a rebuilt `data.zip`. `ProblemDataCompiler.generate` then writes `init.yml` (now including a `harness_grader` block), and `notify_judges_of_problem_data` runs on commit.
- Helper endpoints: `/problems/new/problem-type/`, `/problem-group/`, `/organization/`, and the AI difficulty/tag/decompose endpoints (LiteLLM).
- Every save writes a reversion revision and an admin `LogEntry` (`utils/problem_create.py`).

**Excel import** (`judge/views/problem_import.py`, `utils/simple_xlsx.py`, `utils/excel_embedded_documents.py`):
1. Upload `.xlsx`.
2. `ProblemImportDraft` and one `ProblemImportItem` per row are created, owned by the uploader. Embedded DOCX/MD objects become description text, DOCX images become `ProblemImportImage` blobs served privately, and an embedded ZIP becomes `test_data_zip`.
3. The teacher reviews each item (optional LLM "format" step), saving through the reused create_v2 editor, which stores `editor_data`.
4. Create one item, or create all in one transaction. Images are promoted to public `media/martor/<uuid>.png`.

The admin also has older batch Markdown/Excel creators (`admin/problem.py`, `edit_own_problem`).

**Files.**
- `submission_storage()` = `ResilientStorage(S3/Garage primary, Box SFTP backup)`, with each object tracked in `StoredFileRecord` (sha256, status) and mirrored by Celery or `storage_mirror_sweep`.
- Keys are generated (`<kind>/YYYY/MM/<id>/<uuid><ext>`).
- `EditorAttachment` files stream through `/widgets/files/<uuid>/`.
- Problem test data stays on local disk (`problem_data_storage`), not in S3.
- Import ZIPs currently go to `default_storage` (MEDIA, public; see AUTH-03).

**Exports.**
- `/contest/<key>/export-pdf/` and `/export-word/` (pandoc + lualatex) produce paper exams. See AUTH-01 and AUTH-06.
- `/export/scores` and `/export/submissions` (editor-only, CSV) and the management commands `export_contest_scores` / `export_contest_submissions` are the existing ways to pull scores. There is no REST API for problems, rosters or scores in this area. Everything is session-authenticated HTML/JSON views.

**FairPlay notes.**
- A launch token and exam lock will sit on the contest/participation layer, not here. But the lock is pointless while AUTH-01 (pre-start statement export), AUTH-02 (test-case download inside the exam), AUTH-03 (public test ZIPs) and AUTH-07 (harness download) stay open. All four are outside any client lockdown, because a student can call them from another device.
- Ask DKU to fix these before an exam goes "Require FairPlay".
- To pull problems or assignments, a FairPlay integration should get a scoped, token-authenticated read API, not reuse the create_v2 JSON endpoints. Those endpoints are write-oriented, session plus CSRF, and their responses embed answers (MCQ `is_correct`, blank answers, test data).
