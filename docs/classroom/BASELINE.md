# FairPlay Classroom: P0 baseline

Recorded 2026-10-13 on branch `fairplay-classroom`, on top of commit `4755d4e` (classroom/ imported unchanged). Only setup, configuration, attribution and helper scripts were added in P0. No application code, template, migration or test changed.

**Result: the full suite passes.** `manage.py test judge` ran 1637 tests: 1637 OK, 1 expected failure (marked in the code), 0 skipped, 0 failures, 0 errors. This uses the same configuration as upstream CI (MariaDB, local-memory cache).

## Environment used

| Component | Version |
|---|---|
| OS | Linux container (Ubuntu 24.04 packages), 2 CPUs, 7 GB RAM |
| Python | 3.11.17 (`/usr/bin/python3.11`), venv at `classroom/.venv` |
| Django | 4.2.30 |
| DB driver | mysqlclient 2.3.0 (needs `libmariadb-dev` to build). PyMySQL 1.2.3 is installed too but is not used. |
| MariaDB | 10.11.14 (upstream CI uses `mariadb:11`) |
| Redis | 7.0.15 server, redis-py 8.1.0 |
| Celery | 5.6.3 (no worker runs during tests) |
| Node / npm | 22.22.0 / 10.9.4 (`/opt/node22/bin`) |
| sass / postcss | dart-sass 1.105.1, postcss-cli + autoprefixer (npm global) |
| pandoc / LuaTeX | pandoc 3.1.3, LuaHBTeX 1.17.0 (TeX Live 2023) + `texlive-luatex` |
| site-assets (`resources/libs`) | DMOJ/site-assets `master` @ `cba2efbf86e6` (the same commit upstream DMOJ pins), copied in as plain files with no submodule |

The other Python packages come from `classroom/requirements.txt` with no pins, plus `mysqlclient coverage unittest-xml-reporting tblib`. To list them all, run `classroom/.venv/bin/pip freeze` (123 packages). pip reports one harmless conflict: `wheel 0.48 requires packaging>=24` while requirements pin `packaging<22`.

## One-time setup (exact commands)

Run these as root in the container, from the repo root `/home/claude/fairplay`.

```bash
# System packages that were missing
apt-get install -y --no-install-recommends libmariadb-dev   # mysqlclient build (pkg-config libmariadb)
apt-get install -y --no-install-recommends texlive-luatex   # luaotfload; only for contest PDF export

# Python
python3.11 -m venv classroom/.venv
classroom/.venv/bin/pip install -U pip wheel
classroom/.venv/bin/pip install -r classroom/requirements.txt mysqlclient coverage unittest-xml-reporting tblib

# Static vendor assets (git submodule upstream; vendored here as files)
git clone --depth 1 -b master https://github.com/DMOJ/site-assets.git /tmp/site-assets
rm -rf /tmp/site-assets/.git && cp -a /tmp/site-assets/. classroom/resources/libs/
# (already done and committed. Repeat only to refresh.)

# Settings (gitignored copy). SECRET_KEY comes from $CLASSROOM_SECRET_KEY or is generated once
# into classroom/dmoj/.dev_secret_key (gitignored).
cp classroom/dmoj/local_settings.example.py classroom/dmoj/local_settings.py

# DB + Redis + test-DB grants + MariaDB time-zone tables are handled by the scripts below.
# By hand, it is:
#   mysqld_safe --user=mysql &        redis-server --daemonize yes
#   CREATE DATABASE classroom CHARACTER SET utf8mb4;  user classroom/classroom @localhost and @127.0.0.1
#   GRANT ALL ON `classroom`.* and `test\_classroom%`.*   (Django creates test_classroom[_N])
#   mysql_tzinfo_to_sql /usr/share/zoneinfo | mariadb -uroot mysql

# Front-end build (same as the upstream CI "styles" job)
export PATH=/opt/node22/bin:$HOME/.npm-global/bin:$PATH
npm install -g sass postcss-cli postcss autoprefixer
(cd classroom && npm ci && npm run build:editor && npm run test:editor && ./make_style.sh)
```

## Day-to-day

```bash
bash classroom/scripts/dev-test.sh                      # full suite, label `judge` (what CI runs)
bash classroom/scripts/dev-test.sh --parallel 2         # same, about 40% faster on 2 CPUs
bash classroom/scripts/dev-test.sh judge.views.tests.test_contest_editor -v 2
bash classroom/scripts/dev-server.sh                    # migrate + runserver on 127.0.0.1:8000
bash classroom/scripts/dev-server.sh --celery           # also start a Celery worker
bash classroom/scripts/dev-assets.sh                    # front-end build + Monaco/MathJax into resources/libs
classroom/.venv/bin/python classroom/manage.py seed_dev_data   # demo course, users, assignments (DEBUG only)
```

The code editor (Monaco) and math rendering (MathJax) are npm packages served from `resources/libs/`, never from a CDN. `dev-assets.sh` copies them there, as the Dockerfile does; without it the IDE and workspace editors and all math fail to load. `seed_dev_data` needs a freshly migrated database with `loaddata navbar language_small demo`; every seeded password is `fairplay-dev`.

Both scripts start MariaDB and Redis if they are not running. They create the DB, the user and the grants, load the time-zone tables if they are empty, copy `local_settings.example.py` if `local_settings.py` is missing, and run `compilejsi18n` once. Logs go to `${TMPDIR:-/tmp}/classroom-dev/`. Arguments to `dev-test.sh` are passed through to `manage.py test`. Environment switches:

- `CLASSROOM_TEST_CACHE=redis`: run the tests on Redis instead of locmem (not the CI setup; see below).
- `CLASSROOM_JUNIT=1`: write JUnit XML to `classroom/test-reports/`, as CI does.
- `CLASSROOM_DB_*`, `CLASSROOM_REDIS_URL`, `CLASSROOM_SECRET_KEY`, `CLASSROOM_DEBUG`, `CLASSROOM_ALLOWED_HOSTS`: settings overrides.

How upstream CI runs the tests (`.gitlab-ci.yml` `test` job; `.github/workflows/build.yml` does the same): it copies `.ci.settings.py` to `dmoj/local_settings.py` (MySQL/MariaDB as root, a `LocMemCache`, `COMPRESS_OUTPUT_DIR='cache'`, `STATIC_ROOT=static`, the XML test runner), then runs `manage.py compilejsi18n`, `manage.py check_migration_graph judge` and `coverage run --source=. manage.py test judge`. `local_settings.example.py` copies these settings, apart from the DB user and the plain test runner. For CI-style coverage, run `cd classroom && .venv/bin/coverage run --source=. manage.py test judge && .venv/bin/coverage report`. Coverage was not measured for this baseline.

## Results

| Check | Result |
|---|---|
| `manage.py check` | OK. "System check identified no issues (86 silenced)." |
| `manage.py check_migration_graph judge` | OK. Single leaf `0219_contest_allow_spectate`. |
| `manage.py makemigrations --check --dry-run` | OK. "No changes detected": models and migrations agree. |
| `manage.py migrate` (fresh DB) | OK in 39 s. |
| `manage.py compilejsi18n` | OK |
| `flake8` (CI lint set: flake8 + import-order, future-import, commas, logging-format, quotes) | Clean, 0 findings. Run with `--extend-exclude node_modules` locally, because the KaTeX sources in `node_modules` are not ours. |
| `npm ci` / `npm run build:editor` / `npm run test:editor` | OK. Builds `resources/rich_editor.js` (1.85 MB) and `resources/vendor/katex*`. Vitest: 41/41 pass. |
| `./make_style.sh` | OK. Writes default and dark CSS into `resources/` and `resources/dark/` (both gitignored build output). |
| Test suite, serial, locmem (CI setup) | **1637 run, OK, 1 expected failure.** 131 s for the tests, 3 min 11 s wall clock including test-DB creation. |
| Test suite, `--parallel 2`, locmem | **1637 run, OK, 1 expected failure.** 65 s for the tests, 1 min 53 s wall clock. |
| Test suite, Redis cache (serial and `--parallel 2`) | 1637 run, **2 failures**, 1 expected failure (see "Config-dependent"). |
| Dev server | `runserver` boots. Anonymous requests: `/`, `/problems/`, `/contests/`, `/organizations/` and `/accounts/login/` return 200, and `/admin/` returns 302 to login. Logged in as a superuser: `/`, `/admin/`, `/admin/judge/problem/`, `/ide/`, `/user`, `/submissions/` and `/edit/profile/` return 200. All 14 `/static/...` URLs referenced by these pages return 200. The server log shows no errors. (The superuser was deleted afterwards.) |

### Expected failure (in the code, not a regression)

- `judge.views.tests.test_contest_grade_variants.ContestGradeSaveVariantTestCase.test_nan_score_does_not_crash` is marked `@unittest.expectedFailure`. It documents a real bug: a `score=nan` POST passes the clamp, and saving NaN into a MySQL DOUBLE raises, so the view returns a 500 instead of a 400. This belongs to P1.

### Failing tests: environment-related (fixed in the environment, not in code)

- `judge.models.tests.test_assignment_draft.AssignmentDraftAdminTestCase.test_changelist_and_detail_open` errored on the first run with `ValueError: Database returned an invalid datetime value. Are time zone definitions for your database installed?` (the admin `date_hierarchy` uses `CONVERT_TZ`). The cause was missing MariaDB time-zone tables. The test passes after `mysql_tzinfo_to_sql /usr/share/zoneinfo | mariadb -uroot mysql`, which `_dev-common.sh` now runs automatically. The official `mariadb:11` Docker image used by CI ships these tables loaded.

### Config-dependent (only when tests run on Redis; CI uses locmem)

- `judge.views.tests.test_editor_attachment.EditorAttachmentTest.test_normal_user_hits_daily_quota`: the first upload gets a 429 instead of a 200.
- `judge.views.tests.test_editor_attachment.EditorAttachmentTest.test_normal_user_hits_rate_limit`: the same.

These tests use `@override_settings(CACHES=locmem)`, but the upload rate limiter still counts in the real default cache, so counters from earlier tests in the same run carry over. This is a test-isolation weakness, and it may also mean the limiter binds its cache at import time. It does not affect the CI configuration. Worth checking in P4, when Redis becomes the real cache. Note also that `CLASSROOM_TEST_CACHE=redis` runs `cache.clear()` against Redis db 0, the same database the dev server uses as its cache.

### Real failures

None. With the CI configuration, every test that is not an expected failure passes.

### Observations (not test failures, for later phases)

- The canonical link renders as `http:///…` in dev, because `DMOJ_CANONICAL = ''` and Jinja's `default()` does not replace an empty string. Upstream DMOJ behaves the same. Deployments set `DMOJ_CANONICAL`.
- Pages load MathJax from `cdnjs.cloudflare.com` (7 CDN references in `templates/` and settings). This is relevant to P2: no CDNs (mainland China hosting).
- The visible branding in a fresh install is still "DMOJ: Modern Online Judge" (`SITE_NAME`/`SITE_LONG_NAME`). It is left unchanged until P2.
- `classroom/.gitmodules` still declares `resources/libs` as a submodule. It is inert, because only the repo-root `.gitmodules` counts and the files are now vendored. It was left unchanged.

## What could not be run, and why

- **The judge itself** (the `dmoj/judge-server` sandbox, `runbridged`, the judge bridge). It is not installed, it needs ptrace/seccomp sandboxing and language runtimes, and the test suite does not need it, because judging is simulated through models and mocks. As a result, real code execution, the IDE "run", and submission grading against test data were not exercised end to end.
- **The event daemon / websocket** (`websocket/`, Node `wsevent`) is not started, and `EVENT_DAEMON_USE = False`.
- **Celery workers**: the tests do not need a broker. The dev server queues tasks to Redis db 1, and they run only if you start `dev-server.sh --celery`. That was not exercised beyond `celery` importing.
- **LLM calls** (AI hints, AI grading, question import, decompose): no `LITELLM_*` or `AZURE_*` credentials or endpoint are configured. The tests mock the LLM layer.
- **Contest PDF/Word export** through the UI was not exercised end to end. The toolchain works: after `apt-get install texlive-luatex` (it was missing `luaotfload`), pandoc 3.1.3 with `--pdf-engine=lualatex` and the same flags as `ContestExportPDF` renders a test HTML file to PDF. Problem PDFs via pdfoid (`DMOJ_PDF_PDFOID_URL`) are not configured.
- **S3 / Box SFTP submission storage**: not configured, so the code falls back to local disk (`submission_files/`, gitignored). The tests use the local backend and fakes.
- **MOSS** (`MOSS_API_KEY`) and **social login** (Google, Facebook, GitHub OAuth): not configured.
- **Python 3.12** (the second leg of the CI matrix) was not run. Only 3.11 was.
