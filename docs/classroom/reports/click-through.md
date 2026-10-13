# Click-through test (2026-10-14)

The whole site was clicked through in a real browser (headless Chromium), on a local dev server with a seeded Classroom (`manage.py seed_dev_data`). Two parts:

- **Crawl.** Five roles: anonymous, student, class TA, instructor, site admin. For each role the crawler followed every same-origin link (up to about 220 pages per role) and clicked every non-destructive button, tab and toggle on each page. It recorded HTTP errors, JavaScript exceptions, console errors and failed sub-requests.
- **Scripted flows** for the things the crawler deliberately does not click (save, submit, upload):
  - **Student:** join a homework; answer multiple choice, fill-in-the-blank, short answer, file upload and code; move between questions; check the submissions list; leave; open an exam.
  - **Instructor:** create an assignment in the editor (course, problem from the library), edit it, grade on the grading page, open analytics and the ranking.

**Final run:** 675 pages, no 5xx and no JavaScript errors, apart from the two items under "Left as they are". Every flow step passes except Finalize; see "Product decisions" 1.

## Bugs found and fixed

| Area | Problem | Fix |
|---|---|---|
| Every page with math | MathJax loaded from cdnjs and jsdelivr. Where the CDN is blocked, the page throws `MathJax.typesetPromise is not a function` and the rest of that script stops. | MathJax 3 is an npm dependency, served from `static/libs/mathjax`. All calls are guarded, and martor loads it only once per page. |
| Admin forms, test-data editor | Ace and select2 loaded from cdnjs (`ACE_URL`, `SELECT2_*`), so pickers and code fields broke. | Ace comes from npm (`ace-builds`, served from `libs/ace`). select2 uses the copy already in `libs/select2`. The settings now default to local copies. |
| Every avatar | Loaded from gravatar.com, which is blocked in mainland China. | Avatars are drawn locally (an SVG with the initial). `DMOJ_USE_GRAVATAR = True` switches back. The admin menu uses the same avatar. |
| Martor editor, timezone map | Martor's stylesheet imported Google Fonts; the timezone map was hot-linked from static.dmoj.ca. | Both removed; plain fallbacks are used. |
| Code editor (Monaco) | Missing in dev: nothing copied it to `libs/monaco` outside the Docker build. | `scripts/dev-assets.sh` runs the same asset steps as the Dockerfile. |
| `/accounts/login_netid_redirect/` | 500 crash when `AUTH_HOSTS` is not configured. | It explains that NetID sign-in is not configured (503). `AUTH_HOSTS = {}` by default. |
| Instructors | Could create an assignment (`add_contest`) but got 403 opening it again (`edit_own_contest` was missing from the Instructors group). | Added to the group's permission list. Migration 0224 adds it to existing databases. |
| Admin change lists | `$(...).actions is not a function` on every list: dmoj-wpadmin calls a jQuery plugin removed in Django 4.2. | `templates/admin/change_list.html` override without that call (Django 4.2 initializes actions itself). |
| Admin login page | `django is not defined`. | The inline script is guarded. |
| Editing and replying to comments | Martor found the textarea by `#id_<field>`. The edit form uses `id_edit`, and a reply form is a clone, so the editor never started. | Martor looks for the textarea and the editor inside its own container. |
| Assignment problem table | The user-count link went to "best solutions", which 404s while the scoreboard is hidden. Editors also got 404 before the start. | The link follows the page's own rule. Editors may open it before the start. |
| Submission lists | Showed a "view" link on classmates' answers that the page then refuses (403). | The row uses the same rule as the page (G-6: "solved" visibility is for code only). |
| Course page | "New question" was shown to class TAs, who get a 404. | The menu item needs `create_private_problem`. |
| Test-data page | "View YAML" was always shown (`has_yml` was not called), so it was a 404 for most problems. Short-answer questions linked to test data they cannot have. | `has_yml()` is called. Short answer links to the question editor. |
| Ace fullscreen toggle | Its icon file was not shipped, so the button was blank. | A FontAwesome icon is used instead. |
| Workspace | Code Submit was disabled with no explanation while no judge is online. The first-visit notice said "Pre-exam reminder" on homework too. | The button gets a tooltip that explains it. The title is now "Before you start". |

## Tests
- New file `judge/views/tests/test_click_through_fixes.py`, with 14 tests. It includes a scan that fails if any template, script or stylesheet references a public CDN, Gravatar or Google Fonts.
- Full suite: **1784 tests OK**. flake8 is clean, and `makemigrations --check` reports no changes.

## Product decisions for you
1. **Instructors cannot finalize exam results.** Hidden exam answers are revealed only after Finalize, and Finalize needs `edit_all_contest` (site admins). "Release All" on the grading page publishes manual grades, not hidden verdicts. Today an instructor has to ask an admin to publish exam results. Do instructors finalize their own exams, or should it stay admin-only?
2. **One question in a running exam closes it everywhere.** A question used in a running or upcoming exam that hides results cannot be submitted in a homework or as practice until the exam ends (G-7). The student sees a clear message. In practice, instructors should not reuse an exam question in a concurrent homework. (Already listed in P1-verification "Behaviour changes 1".)

## Left as they are
- `/stats/...` and the password check: `api.pwnedpasswords.com` is called on password change. It has a 2-second timeout and fails open, so it only adds a short delay where it is blocked.
- Admin "add" pages for submissions, profiles and point votes return 403 (no add permission by design). They are reachable only by typing the URL.

## How to repeat
```bash
bash classroom/scripts/dev-assets.sh
mariadb -uroot -e "DROP DATABASE IF EXISTS classroom; CREATE DATABASE classroom CHARACTER SET utf8mb4;"
classroom/.venv/bin/python classroom/manage.py migrate
classroom/.venv/bin/python classroom/manage.py loaddata navbar language_small demo
classroom/.venv/bin/python classroom/manage.py seed_dev_data      # password: fairplay-dev
bash classroom/scripts/dev-server.sh
```
The crawler and flow scripts are Playwright scripts. They are not in the repository yet; they can be added under `classroom/e2e/` if you want them in CI.
