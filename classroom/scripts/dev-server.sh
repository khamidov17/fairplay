#!/usr/bin/env bash
# Start a local FairPlay Classroom dev server (Django runserver, DEBUG on).
#
#   bash classroom/scripts/dev-server.sh            # http://127.0.0.1:8000/
#   bash classroom/scripts/dev-server.sh --celery   # also start a Celery worker
#   PORT=8001 HOST=0.0.0.0 bash classroom/scripts/dev-server.sh
#
# Starts MariaDB/Redis if needed and applies migrations first. Create an admin with
#   classroom/.venv/bin/python classroom/manage.py createsuperuser
# The judge bridge, judges and event daemon are not started; scripts/dev-judge.sh does that.
source "$(dirname "${BASH_SOURCE[0]}")/_dev-common.sh"

CELERY=0
for arg in "$@"; do
  case "$arg" in
    --celery) CELERY=1 ;;
    *) log "unknown argument: $arg"; exit 2 ;;
  esac
done

ensure_mariadb
ensure_redis
ensure_python

cd "$CLASSROOM_DIR"
"$PY" manage.py migrate --noinput

if [ "$CELERY" = 1 ]; then
  log "starting Celery worker (log: $LOG_DIR/celery.log)"
  "$VENV/bin/celery" -A dmoj_celery worker --loglevel=info >"$LOG_DIR/celery.log" 2>&1 &
  CELERY_PID=$!
  trap 'kill "$CELERY_PID" 2>/dev/null || true' EXIT
fi

"$PY" manage.py runserver "${HOST:-127.0.0.1}:${PORT:-8000}"
