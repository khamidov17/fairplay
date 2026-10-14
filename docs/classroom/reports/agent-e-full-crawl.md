# Full crawl, every route, every role (agent E, 2026-10-14)

The earlier click-through stopped at about 220 pages per role. This crawl has no page cap and also
visits every URL route of `dmoj/urls.py` directly, with its arguments filled from the seeded data.

## How

- **Data.** `seed_dev_data` plus `e2e/full-crawl/seed_extra.py` (a blog post, a comment on a problem and
  on the post, a ticket with a message, a pending course request, a contest tag, a practice code
  submission by ab101 and a homework code submission by ab102), so every detail route has an object.
- **Seed URLs.** `e2e/full-crawl/gen_urls.py` walks `get_resolver()` and fills each route's arguments from
  the database: contest keys, problem codes, usernames, course and class ids/slugs, submission ids,
  ticket ids, blog posts, comments, course requests, (contest, problem) and (contest, user) pairs. It
  also adds the change list, the add page and the first object's change page of every model registered
  in the admin. 1074 URLs. Skipped: routes that need a token, task id, file UUID or draft, and session
  changers (logout, impersonate, language switch, social login).
- **Crawler** (`e2e/full-crawl/crawl.js`, Playwright/Chromium). It visits every seed URL, then follows every
  same-origin link. It keeps up to 3 pages per URL pattern (digits collapsed; admin filter queries
  count as one pattern). It clicks the non-destructive buttons, tabs and toggles on the first page of
  each pattern. **Every link a page shows is checked**, including links beyond the pattern cap (a plain
  GET with the role's cookies), so "a link shown to this role leads to 4xx" is complete.
  It records:
  - 5xx responses, JavaScript exceptions and console errors;
  - failed or 4xx sub-requests;
  - requests to other hosts (all aborted and logged);
  - visible template syntax, tracebacks, `undefined`, `NaN` or `[object Object]` in the page text;
  - raw JSON reached through a link;
  - horizontal overflow that is still there one second after load.
- **Roles.** anonymous; student ab101; ab101 joined to the homework `cs101_hw1`; ab101 joined to the
  running exam `cs101_exam1`; TA ta_wang; instructor prof_li; admin. The two joined passes join
  through the real POST and leave at the end.
- **Layout pass** (`e2e/full-crawl/shots.js`). The top 15 pages per role were screenshotted:
  - student and instructor at 390x844 (iPhone 13), full page;
  - every role on desktop at 1366x900.

  An in-page check reported sideways scrolling, elements past the right edge, fixed bars that
  overlap, and fixed panels taller than 40% of the screen. I looked at the screenshots myself.

## Pages per role (final run, after the fixes)

| Role | Pages visited | Links checked | 2xx | 403 | 404 | 405 | 5xx |
|---|---|---|---|---|---|---|---|
| anonymous | 1007 | 120 | 611 | 72 | 251 | 42 | 503 ×1 (NetID, by design) |
| student ab101 | 1038 | 189 | 493 | 197 | 227 | 110 | 503 ×1 |
| ab101 in homework | 1019 | 156 | 464 | 200 | 234 | 110 | 503 ×1 |
| ab101 in exam | 1021 | 154 | 447 | 200 | 253 | 110 | 503 ×1 |
| TA ta_wang | 1052 | 200 | 461 | 232 | 241 | 110 | 503 ×1 |
| instructor prof_li | 1087 | 423 | 747 | 79 | 132 | 115 | 503 ×1 |
| admin | 1306 | 1964 | 1009 | 23 | 132 | 127 | 503 ×1 |

Most 403, 404 and 405 responses come from the seed URLs: every route is tried for every role, and
POST-only endpoints answer 405 to a GET. None of them is linked to that role, except the cases listed
under "Remaining". In total, 7530 page visits plus 3206 checked links. Final layout pass: no
sideways scrolling and no overlapping fixed bars on any of the 30 phone pages or 75 desktop pages.

**First run, before the fixes:** 7 HTTP 500s on 4 routes, 9 dead links, 5 JavaScript or sub-request
errors, and phone layouts with sideways scrolling of 140 to 711 px on 5 of the 15 instructor pages.

**External hosts contacted:** none, in any role or page.

## Bugs found and fixed

| # | Where | Problem | Fix |
|---|---|---|---|
| 1 | `/organization/<id>/request` (anonymous) | 500 (`TypeError`): the view checked membership with `request.profile` = None before `LoginRequiredMixin` ran. | Visitors are sent to log in first. |
| 2 | Class edit, class create, roster import (anonymous) | Edit: 500. Create and import: "Access denied" instead of login, for the same reason. An AST scan found no other view with this pattern. | Same as #1. |
| 3 | `/comments/<id>/history/ajax` | 500 ("Negative indexing") for comments with no saved version, such as comments made in the admin or by scripts. | 404. |
| 4 | `/contest/<key>/submissions/<user>/` and `.../rank/<problem>/` | Anonymous visitors and outsiders could open these lists for a course-only assignment. The page title showed the assignment name. | `ForceContestMixin` also requires `contest.is_accessible_by`. |
| 5 | Submission status and source pages | The page linked to the question and to "Resubmit" even when the viewer cannot open the question (a student's exam answer outside the exam): two 404 links. | Both links are shown only if the question is open to the viewer. |
| 6 | An assignment's per-problem lists, viewed while sitting another assignment | The tabs (All, Mine, Best) linked to the problem's site-wide lists, which are 404 during an assignment. | The tabs stay inside the assignment. "All" has no link there. |
| 7 | Login page | The "Duke NetID" button showed even when NetID is not configured, and led to a 503 page. | Shown only when `AUTH_HOSTS['Duke-netid']` is set. |
| 8 | Comments | A user who may not comment yet (no solved problem) got a Reply link. Clicking it cloned an empty form: JS error `ace.edit can't find div #martor-reply-N`. | No Reply link without a comment form. |
| 9 | Ace fullscreen toggle | `href="./"` was a real link (404 on `/problem/<code>/resubmit/<id>`). Fullscreen mode loaded the missing `ace/img/contract.png` (404). | `href="#"`, and FontAwesome compress icon. |
| 10 | Submit page with no judge online | It requested `/widgets/template` without a language id (404 on every visit). | No request without a language. |
| 11 | Admin side menu (wpadmin) | "+" (add) for every model, including submissions, profiles and point votes, which nobody may add (403). | `judge.utils.wpadmin_menu.PermissionAwareLeftMenu` keeps "+" only where the admin app list allows adding. |
| 12 | Language fixtures | AWK, Pike and Turing named Ace modes that do not exist (`mode-AWK.js` 404 in the admin). | Changed to `text` in `language_small.json` and `language_all.json`. Existing databases keep their value: set it in the admin. |
| 13 | Phones: assignment, ranking, analytics, problem list, test-data pages | The whole page scrolled sideways (140 to 711 px), and content was cut off on the right. `#content` has auto side margins inside a flex column, so it grew to the width of its widest row (a row of tabs, the join button). | On ≤760px: no side margins, `max-width: 100%`, and the tab rows wrap. |
| 14 | Phones: problem list | The hidden select2 `<select>` was forced to 100% width with `!important`. The tag search box kept the inline width select2 gave it (1024px). | The hidden-select width rule excludes `.select2-hidden-accessible`. The search box has `max-width: 100%`. |
| 15 | Phones: assignment editor, test data, submissions | The visibility switch cut off "Hidden". The test-data tables were wider than the page. The statistics card was 30px too wide. | The switch wraps. The tables scroll inside themselves. The card uses `box-sizing: border-box`. |
| 16 | Navigation bar "About" | 404 on a fresh install: the navbar fixture links to `/about/`, and no page exists there. | `seed_dev_data` creates the flat page. **Production must have an `/about/` flat page** (normal DMOJ setup). |
| 17 | Dev seed | Math was written as `$a$`. DMOJ and the rich editor use `~a~`, so it showed as raw text. | Seed text fixed. |

No migrations were added. The compiled CSS is gitignored: run `./make_style.sh` (or `scripts/dev-assets.sh`)
after pulling. The SCSS changes are in `base.scss`, `problem.scss`, `contest_v2.scss`,
`submission.scss` and `ace-dmoj.scss`.

## Tests

- New file `judge/views/tests/test_full_crawl_fixes.py` with 21 tests. Every fix test fails on the
  code before its fix. Fixes #1 to #10 and #16 were checked by reverting the source and running the
  tests: 12 failed or errored, and the 3 control tests passed. The admin-menu test failed on the
  wpadmin menu. The fixture, icon and SCSS-rule tests were checked against the previous file
  contents. The three style-only tests assert the SCSS rules, because the suite has no browser. The
  layout itself was checked in Chromium.
- Related modules run together with no `--parallel`:
  - test_exam_contest, test_submission, test_pwned
  - test_ai_grading_dual_role, test_ai_hints, test_click_through_fixes
  - test_contest_ai_grading, test_contest_grade_fixes, test_contest_v2
  - test_file_upload, test_full_crawl_fixes, test_grade_entry
  - test_hidden_results, test_ide, test_organization_courses
  - test_problem_create_v2, test_problem_list, test_problem_workspace, test_submission_list

  Result: **568 tests OK**.
- flake8 is clean on all changed Python files.
- A first version of the about-page test ran the whole `seed_dev_data` command inside the suite. That
  broke 39 tests in later modules, because MariaDB auto-increment values are not rolled back. The test
  now calls only the helper that creates the page.

## Remaining findings (not changed) and why

- **`/accounts/login_netid_redirect/` 503** in every role. The URL is only reached from the route list
  now (fix #7 removed the button). The 503 is the intended "not configured" page.
- **Admin links that answer 405 to a GET:** contest rejudge, finalize, un-finalize, "rate all" and
  submission rejudge. They are buttons: a JavaScript handler asks for confirmation and POSTs.
  Working as designed.
- **Admin "View on site"** (`/admin/r/...`) redirects to the `Site` domain, which is `localhost:8081` from the
  demo fixture. This is configuration: production sets the Site domain. `og:url` and the canonical
  URL show `http:///...` because `SITE_FULL_URL` is empty in the dev settings.
- **18 admin "navigation interrupted" entries.** These come from the crawler: a popup link or a
  JavaScript redirect fired while the next page was loading. No error page was behind them.
- **`/problem/<code>/vote` opened directly:** JS error `init_problem_vote_form is not defined`. This URL
  is an AJAX fragment that is never linked. Upstream DMOJ behaves the same way.
- **Student sitting an assignment → `/user/<other>/solved` links "Best" lists of problems outside the
  assignment** (404 during the assignment). This is upstream DMOJ behaviour on a profile page, with low
  impact. Fixing it needs the viewer's current-contest problem list on that page.
- **TA (class admin) gets 404 on the assignment grading page.** The TA is never shown a link to it, so
  this is not a broken link. Whether class TAs should grade is a product decision.
- **Design points seen in the screenshots** (product decisions, not bugs):
  - assignment and exam pages have a public comment thread with Facebook and Twitter share icons,
    and students could post there during an exam;
  - course member lists are public (already in REVIEW.md §5);
  - "All submissions" shows classmates' graded quiz answers with scores;
  - the admin is titled "DMOJ: Modern Online Judge" while the site says "DKU OJ";
  - the exam page button still says "Join contest".
- **Phone details that are acceptable but not polished:** wide tables (problem list, test data)
  scroll inside their card. The workspace's tab strip and progress bar are clipped at the right
  edge, and the tab strip scrolls.
- **The fixture change (#12) does not update existing databases.** On production, set the Ace mode of
  AWK, Pike and Turing to `text` in the admin.

## How to repeat

```bash
bash scripts/dev-assets.sh            # or ./make_style.sh after SCSS changes
# fresh DB, then:
manage.py migrate && manage.py loaddata navbar language_small demo && manage.py seed_dev_data
manage.py shell < e2e/full-crawl/seed_extra.py
manage.py shell < e2e/full-crawl/gen_urls.py > e2e/full-crawl/urls.json
manage.py runserver --noreload 127.0.0.1:8000 &
PORT=8000 node e2e/full-crawl/crawl.js r_A.json anon student student_hw student_exam   # about 40 min
PORT=8000 node e2e/full-crawl/crawl.js r_B.json admin ta teacher                       # about 55 min
python3 e2e/full-crawl/summarize.py r_A.json r_B.json
PORT=8000 node e2e/full-crawl/shots.js shots student:mobile teacher:mobile student:desktop ta:desktop admin:desktop anon:desktop
python3 e2e/full-crawl/layout_sum.py shots/layout.json
```
The scripts use Playwright from `/opt/node22/lib/node_modules/playwright`.
