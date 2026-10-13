#!/usr/bin/env bash
# Run the FairPlay Classroom test suite locally, the same way upstream CI does
# (`manage.py test judge` on MariaDB with a local-memory cache).
#
#   bash classroom/scripts/dev-test.sh                       # full suite (label: judge)
#   bash classroom/scripts/dev-test.sh judge.views.tests.test_contest_editor
#   bash classroom/scripts/dev-test.sh judge --parallel 2 -v 2
#
# Any arguments are passed to `manage.py test`. With no arguments the label is `judge`.
# Env: CLASSROOM_TEST_CACHE=redis uses Redis instead of locmem; CLASSROOM_JUNIT=1 writes
# JUnit XML to classroom/test-reports/.
source "$(dirname "${BASH_SOURCE[0]}")/_dev-common.sh"

ensure_mariadb
ensure_redis
ensure_python

cd "$CLASSROOM_DIR"
if [ "$#" -eq 0 ]; then
  set -- judge
fi
case "$1" in
  -*) set -- judge "$@" ;;
esac
exec "$PY" manage.py test --noinput "$@"
