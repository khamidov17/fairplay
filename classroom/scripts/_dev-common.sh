# Shared helpers for scripts/dev-test.sh and scripts/dev-server.sh. Source, do not run.
# Local development only: starts MariaDB and Redis on this machine if they are not running,
# creates the `classroom` database and user, and checks the virtualenv and local settings.
set -euo pipefail

CLASSROOM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${CLASSROOM_VENV:-$CLASSROOM_DIR/.venv}"
PY="$VENV/bin/python"
LOG_DIR="${CLASSROOM_LOG_DIR:-${TMPDIR:-/tmp}/classroom-dev}"
mkdir -p "$LOG_DIR"

DB_NAME="${CLASSROOM_DB_NAME:-classroom}"
DB_USER="${CLASSROOM_DB_USER:-classroom}"
DB_PASSWORD="${CLASSROOM_DB_PASSWORD:-classroom}"

log() { echo "[classroom] $*" >&2; }

ensure_mariadb() {
  if mariadb-admin ping --silent >/dev/null 2>&1 || mysqladmin ping --silent >/dev/null 2>&1; then
    :
  else
    log "starting MariaDB (log: $LOG_DIR/mariadb.log)"
    mkdir -p /run/mysqld && chown mysql:mysql /run/mysqld 2>/dev/null || true
    nohup mysqld_safe --user=mysql >"$LOG_DIR/mariadb.log" 2>&1 &
    for _ in $(seq 1 60); do
      mariadb-admin ping --silent >/dev/null 2>&1 && break
      sleep 1
    done
    mariadb-admin ping --silent >/dev/null 2>&1 || { log "MariaDB did not start"; exit 1; }
  fi
  # Database, user and test-database grants (Django creates test_<name> and, with
  # --parallel, test_<name>_N). Needs root over the local socket; skipped if that fails.
  if mariadb -uroot -e 'SELECT 1' >/dev/null 2>&1; then
    mariadb -uroot <<SQL
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASSWORD';
CREATE USER IF NOT EXISTS '$DB_USER'@'127.0.0.1' IDENTIFIED BY '$DB_PASSWORD';
GRANT ALL ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
GRANT ALL ON \`$DB_NAME\`.* TO '$DB_USER'@'127.0.0.1';
GRANT ALL ON \`test\\_$DB_NAME%\`.* TO '$DB_USER'@'localhost';
GRANT ALL ON \`test\\_$DB_NAME%\`.* TO '$DB_USER'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
    # Time-zone tables: without them date_hierarchy / CONVERT_TZ queries return NULL
    # ("Database returned an invalid datetime value").
    if [ "$(mariadb -uroot -N -e 'SELECT COUNT(*) FROM mysql.time_zone_name')" = "0" ]; then
      log "loading MariaDB time-zone tables"
      mysql_tzinfo_to_sql /usr/share/zoneinfo 2>/dev/null | mariadb -uroot mysql
    fi
  fi
}

ensure_redis() {
  if ! redis-cli ping >/dev/null 2>&1; then
    log "starting Redis"
    redis-server --daemonize yes --dir "$LOG_DIR" --logfile "$LOG_DIR/redis.log" >/dev/null
    for _ in $(seq 1 20); do redis-cli ping >/dev/null 2>&1 && break; sleep 0.5; done
  fi
}

ensure_python() {
  if [ ! -x "$PY" ]; then
    log "no virtualenv at $VENV. Create it first (see docs/classroom/BASELINE.md):"
    log "  python3.11 -m venv $VENV && $VENV/bin/pip install -r $CLASSROOM_DIR/requirements.txt mysqlclient coverage unittest-xml-reporting"
    exit 1
  fi
  if [ ! -f "$CLASSROOM_DIR/dmoj/local_settings.py" ]; then
    log "creating dmoj/local_settings.py from local_settings.example.py"
    cp "$CLASSROOM_DIR/dmoj/local_settings.example.py" "$CLASSROOM_DIR/dmoj/local_settings.py"
  fi
  # base.html calls inlinei18n(), which reads the compiled catalog from STATIC_ROOT.
  if [ ! -d "$CLASSROOM_DIR/static/jsi18n" ]; then
    log "compiling JS i18n catalogs"
    (cd "$CLASSROOM_DIR" && "$PY" manage.py compilejsi18n >/dev/null)
  fi
}
