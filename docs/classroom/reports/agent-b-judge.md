# Agent B: code questions against a real DMOJ judge

**Result:** code questions work end to end with a real judge, set up the way DKU runs it: site, bridge (`manage.py runbridged`), event daemon (`websocket/daemon.js`) and the `dmoj` 4.1.0 judge from PyPI. The judge's ptrace/seccomp sandbox works in this container, so nothing was run without the sandbox. Testing found 7 site bugs, all fixed with tests. Two features (IDE runs and the `allac`/`llm` checkers) need DKU's judge fork. For local work, IDE runs now use a stand-in grader.

## Setup (repeatable)

`classroom/scripts/dev-judge.sh start|stop|status` does all of the following. It needs root, or an existing unprivileged user.

1. `apt-get install libseccomp-dev`. Without it the `dmoj` wheel fails to build (`seccomp.h: No such file`).
2. `python3.11 -m venv $JUDGE_HOME/venv && $JUDGE_HOME/venv/bin/pip install dmoj` (version 4.1.0). Put `$JUDGE_HOME` somewhere the judge user can read. `/tmp/claude-0` is mode 700, and the harness resets it, so these runs used `/srv/fairplay-b`.
3. Create an unprivileged user (`useradd --system classroom-judge`), because the judge refuses to run as root. The script uses `setpriv` to switch to it.
4. Write `judge.yml` with `id`, `key`, `problem_storage_globs: [$JUDGE_HOME/problems/*]`, and the `runtime:` block from `dmoj-autoconf`. Runtimes found here: C, C11, C++03/11/14/17/20, Python 3.13.
5. Register the judge in the site: the `Judge` row with name and key (`manage.py addjudge <name> <key>` or the shell). The script also creates the hidden `idepractice` problem if it is missing, and copies its judge data from `scripts/dev-judge/idepractice/`.
6. Start `manage.py runbridged`, `node websocket/daemon.js` (with `NODE_PATH` set to the repo's `node_modules`) and `dmoj -c judge.yml -p <bridge port> 127.0.0.1`.
7. Start the site with the same environment. `local_settings.example.py` now reads `CLASSROOM_PROBLEM_DATA_ROOT`, `CLASSROOM_BRIDGE_JUDGE_PORT`, `CLASSROOM_BRIDGE_DJANGO_PORT`, `CLASSROOM_EVENT_DAEMON=1`, `WS_GET_PORT` and `WS_POST_PORT`. When the event daemon is on, it also takes `EVENT_DAEMON_SUBMISSION_KEY` from `SECRET_KEY`. Otherwise the settings check refuses the well-known default key.

My run used judge port 9991, Django port 9992, event ports 15110/15111/15112 and site port 8102. I also checked the script itself on a fresh `JUDGE_HOME` with other ports: start, status, then stop. The bridge now exits on SIGTERM.

## What I tested and how

Everything ran in Playwright (Chromium) against my dev server on port 8102. The scripts are in my scratch directory.

| Flow | Result |
|---|---|
| Teacher `prof_li`, `/problem/cs_sum/test_data`: upload a zip (5 cases), save; add 5 rows (inputs autofill outputs), "Distribute points evenly", save → `init.yml` with `archive` and 5 × 2 points | Works after fixes 1 and 2. |
| Judge picks up the new data (watchdog) and lists `cs_sum` and `idepractice` | Works. |
| ab101 joins `cs101_hw1`; workspace **Run test** with custom input `20 22` | Output `42`, "Result: AC". Needs the IDE problem (fix 3) and the stand-in grader. |
| Submit Python correct / wrong | AC 10/10 and WA 0/10, shown live in the Result tab within about 2 s (workspace polling). |
| Submit C++11 that does not compile | CE with compiler output in the Result tab. |
| Submit `while True: pass` | TLE on all 5 cases, 0/10. |
| Submit C (correct) | AC 10/10, with compiler warnings shown. |
| `/submission/<id>` (student and teacher) | Correct verdict, cases and points. |
| Live updates with the event daemon on | Status page websocket to `:15110`, `test-case` / `grading-end` frames, page updates without reload. |
| Live updates with the event daemon off | Workspace polling works. The status page did not update (fix 6). |
| Assignment ranking | ab101 10, ab105 10, ab103 0 (best submission counts). |
| Teacher grading page `/contest/cs101_hw1/grade` | Code item with version selection (all attempts, best starred), source, verdict and per-case table. |
| `/ide/` as ab102 | Language list comes from the online judge. Python run with stdin prints the right output. C++ compile error is shown. |
| `cs_sum` as a public practice problem (ab102, not in the homework) | Workspace submit gives AC 10/10. |
| Classic `/problem/cs_sum/submit` page (Ace) as ab104 | Redirects to `/submission/<id>`, AC 10/10. |

No JS exceptions or 5xx responses in any flow.

## Bugs fixed

1. **The first zip upload failed with "Points must be defined for non-batch case #1."** The page numbers its always-present blank row (`order=1`), so the form saw it as changed and saved an empty case. Now a new case row where only `order` is set counts as unchanged (`ProblemCaseForm.has_changed`, `judge/views/problem_data.py`).
2. **Re-uploading a test-data zip deleted the test data.** `ProblemData.save()` deleted the old file with `FieldFile.delete()`, which also sets the instance's `zipfile` to `None`, so the new upload was dropped. The problem was left with no archive, and the next `init.yml` save failed. The old file is now removed through the storage (`judge/models/problem_data.py`). This was confirmed in the browser before and after the fix. It is upstream DMOJ code.
3. **`/ide/` and workspace "Run test" returned 500 ("IDE feature not configured") on a seeded database**, because nothing creates the hidden `idepractice` problem. `seed_dev_data` now creates it (hidden, all languages), and so does `dev-judge.sh` for existing databases.
4. **A second bridge that could not bind marked every judge offline and every in-progress submission as IE** before failing with "Address already in use". `judge_daemon()` now binds both ports before it resets anything, and closes the first listener if the second fails (`judge/bridge/daemon.py`).
5. **The bridge did not exit on SIGTERM while a judge was connected.** Connection threads were non-daemon and the listening sockets were never closed, so the port stayed bound and a restart failed. The fix uses `daemon_threads` and `server_close()` (`judge/bridge/server.py`).
6. **Without the event daemon, `/submission/<id>` stayed on "RESULT —" until reloaded.** The live script only loads when `last_msg` is set. A polling fallback now fetches the test cases every 2 s until a `sub-graded-marker` in the fragment reports graded (`templates/submission/status.html`, `status-testcases.html`).
7. **The grading page meta line showed the raw type code**, e.g. "A. Sum of two numbers · C · C" for a C program. It now shows "Programming" (and the other type names), taken from the Type filter (`templates/contest/grade.html`).

Tests: `judge/views/tests/test_judge_e2e_fixes.py` has 14 tests. 13 of them fail without the fixes; the zip-clear test is a guard and passes either way. Commands run:

- `bash scripts/dev-test.sh judge.views.tests.test_judge_e2e_fixes`: 14 OK.
- Related modules: `test_problem` (models), `test_ai_hints`, `test_click_through_fixes`, `test_contest_grade`, `test_contest_grade_fixes`, `test_hidden_results`, `test_ide`, `test_problem_create_v2`, `test_problem_import_creation`, `test_problem_workspace`, `test_authoring_security`, `test_file_upload`, plus the new module: 402 tests OK.
- flake8 is clean on every changed Python file.

No migrations.

## Limited by the judge build, not site bugs

- **IDE mode.** The bridge sends IDE runs as problem `idepractice` with `meta.custom_input`, which is a feature of DKU's judge fork. The stock judge ignores it. `scripts/dev-judge/idepractice/ide_grader.py` is a custom grader that feeds that input to the program and accepts any run that finishes. It is for local work only; production needs DKU's judge.
- **Checkers `allac` and `llm`.** The site offers them in the test-data editor, but the stock judge does not have them, so a case that uses them gives IE ("error loading checker"). They need the fork.
- **`update-problems` packet.** The bridge sends it after data edits, and the stock judge logs "Unknown packet" (harmless). The stock judge relies on its directory watchdog instead, which worked here.
- **`time.sleep` gives RTE.** `clock_nanosleep` is disallowed by the standard DMOJ sandbox.
- **TLE timing.** Cases show TLE at 3–4 s of CPU for a 2 s limit, because 2 CPUs are shared by 5 agents. The verdicts are correct.

## Left / notes

- After an AJAX submit, the workspace only polls (every 1 s), even when the event daemon is on. Its event subscription needs a `data-sub-channel` that is set only when the page is opened with `?submission=`. Polling is enough, so I left this as is.
- The grading page header says "Not graded yet" for a judged code submission. This means no manual review has been done, which is by design.
- The page `<title>` still says "DMOJ: Modern Online Judge" (known, P2).
- My dev database still has an offline judge row, `b-script`, from the script test. It only exists in `classroom_b`.
