#!/usr/bin/env bash
# Run a real DMOJ judge locally, the way DKU runs it: bridge (manage.py runbridged) + event
# daemon (websocket/daemon.js) + one `dmoj` judge from PyPI. Development only.
#
#   bash classroom/scripts/dev-judge.sh start     # install if needed, then start all three
#   bash classroom/scripts/dev-judge.sh stop
#   bash classroom/scripts/dev-judge.sh status
#
# Then start the site with the same environment, so it finds the bridge, the event daemon and
# the test data (the values below are printed by `start`):
#   CLASSROOM_PROBLEM_DATA_ROOT=... CLASSROOM_BRIDGE_DJANGO_PORT=... CLASSROOM_EVENT_DAEMON=1 \
#   WS_GET_PORT=... WS_POST_PORT=... bash classroom/scripts/dev-server.sh
# (dmoj/local_settings.example.py reads these variables.)
#
# Env (defaults in brackets):
#   JUDGE_HOME [/srv/classroom-judge]   judge venv, judge.yml, logs and the problem data. Must be
#                                       readable by JUDGE_USER, so not under a private /tmp dir.
#   JUDGE_USER [classroom-judge]        unprivileged user for the judge (it refuses to run as root)
#   JUDGE_NAME [local] JUDGE_KEY [generated once, kept in $JUDGE_HOME/judge.key]
#   BRIDGE_JUDGE_PORT [9999] BRIDGE_DJANGO_PORT [9998]
#   WS_GET_PORT [15100] WS_POST_PORT [15101] WS_HTTP_PORT [15102]; EVENTS=0 skips the event daemon
#   JUDGE_PYTHON [python3.11]  NODE [node]
#
# Needs: gcc/g++ and libseccomp-dev (the judge's sandbox is built from source), Node with the
# repo's node_modules (ws, qu, simplesets) for the event daemon, and ptrace allowed (the
# sandbox traces every syscall; containers without CAP_SYS_PTRACE cannot judge).
#
# The judge data for the hidden `idepractice` problem (IDE runs and the workspace "Run test")
# comes from scripts/dev-judge/idepractice: the stock judge has no IDE mode, see ide_grader.py.
source "$(dirname "${BASH_SOURCE[0]}")/_dev-common.sh"

JUDGE_HOME="${JUDGE_HOME:-/srv/classroom-judge}"
JUDGE_USER="${JUDGE_USER:-classroom-judge}"
JUDGE_NAME="${JUDGE_NAME:-local}"
BRIDGE_JUDGE_PORT="${BRIDGE_JUDGE_PORT:-9999}"
BRIDGE_DJANGO_PORT="${BRIDGE_DJANGO_PORT:-9998}"
WS_GET_PORT="${WS_GET_PORT:-15100}"
WS_POST_PORT="${WS_POST_PORT:-15101}"
WS_HTTP_PORT="${WS_HTTP_PORT:-15102}"
EVENTS="${EVENTS:-1}"
JUDGE_PYTHON="${JUDGE_PYTHON:-python3.11}"
NODE="${NODE:-node}"
PROBLEMS="$JUDGE_HOME/problems"
JVENV="$JUDGE_HOME/venv"
RUN="$JUDGE_HOME/run"

site_env() {
  export CLASSROOM_PROBLEM_DATA_ROOT="$PROBLEMS"
  export CLASSROOM_BRIDGE_JUDGE_PORT="$BRIDGE_JUDGE_PORT" CLASSROOM_BRIDGE_DJANGO_PORT="$BRIDGE_DJANGO_PORT"
  export WS_GET_PORT WS_POST_PORT WS_HTTP_PORT
  if [ "$EVENTS" = 1 ]; then export CLASSROOM_EVENT_DAEMON=1; fi
}

as_judge_user() {
  if [ "$(id -u)" = 0 ]; then
    setpriv --reuid="$JUDGE_USER" --regid="$JUDGE_USER" --clear-groups env HOME="$JUDGE_HOME" "$@"
  else
    "$@"
  fi
}

install() {
  mkdir -p "$PROBLEMS" "$RUN"
  if [ "$(id -u)" = 0 ] && ! id "$JUDGE_USER" >/dev/null 2>&1; then
    log "creating user $JUDGE_USER"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$JUDGE_USER"
  fi
  if [ ! -x "$JVENV/bin/dmoj" ]; then
    log "installing the dmoj judge into $JVENV (builds its sandbox; needs libseccomp-dev)"
    "$JUDGE_PYTHON" -m venv "$JVENV"
    "$JVENV/bin/pip" install -q dmoj
  fi
  if [ ! -f "$JUDGE_HOME/judge.key" ]; then
    (umask 077; "$PY" -c 'import secrets; print(secrets.token_urlsafe(48))' > "$JUDGE_HOME/judge.key")
  fi
  JUDGE_KEY="${JUDGE_KEY:-$(cat "$JUDGE_HOME/judge.key")}"
  if [ ! -f "$JUDGE_HOME/judge.yml" ]; then
    log "detecting runtimes (dmoj-autoconf)"
    {
      echo "id: $JUDGE_NAME"
      echo "key: $JUDGE_KEY"
      echo "problem_storage_globs:"
      echo "  - $PROBLEMS/*"
      as_judge_user "$JVENV/bin/dmoj-autoconf" 2>/dev/null
    } > "$JUDGE_HOME/judge.yml.tmp"
    mv "$JUDGE_HOME/judge.yml.tmp" "$JUDGE_HOME/judge.yml"
  fi
  chmod 600 "$JUDGE_HOME/judge.key" "$JUDGE_HOME/judge.yml"
  if [ "$(id -u)" = 0 ]; then chown "$JUDGE_USER" "$JUDGE_HOME/judge.yml"; fi
  mkdir -p "$PROBLEMS/idepractice"
  cp "$CLASSROOM_DIR"/scripts/dev-judge/idepractice/* "$PROBLEMS/idepractice/"
  chmod -R a+rX "$PROBLEMS"
  # The site's side: the Judge row (name + key) and the hidden IDE problem.
  (cd "$CLASSROOM_DIR" && JUDGE_NAME="$JUDGE_NAME" JUDGE_KEY="$JUDGE_KEY" "$PY" manage.py shell -c '
import os
from judge.models import Judge, Language, Problem, ProblemGroup
judge, _ = Judge.objects.get_or_create(name=os.environ["JUDGE_NAME"], defaults={"auth_key": os.environ["JUDGE_KEY"]})
if judge.auth_key != os.environ["JUDGE_KEY"]:
    judge.auth_key = os.environ["JUDGE_KEY"]
    judge.save(update_fields=["auth_key"])
if not Problem.objects.filter(code="idepractice").exists():
    group = ProblemGroup.objects.first() or ProblemGroup.objects.create(name="ide", full_name="IDE")
    ide = Problem.objects.create(code="idepractice", name="IDE sandbox", description="Code runs from the IDE.",
                                 points=1, partial=False, time_limit=2, memory_limit=65536, group=group,
                                 is_public=False)
    ide.allowed_languages.set(Language.objects.exclude(key="ANSWER"))
')
}

pid_alive() { [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }

start() {
  ensure_mariadb
  ensure_redis
  ensure_python
  install
  site_env
  cd "$CLASSROOM_DIR"
  if ! pid_alive bridge; then
    log "starting the bridge: judges on :$BRIDGE_JUDGE_PORT, site on :$BRIDGE_DJANGO_PORT (log: $RUN/bridge.log)"
    nohup "$PY" manage.py runbridged > "$RUN/bridge.log" 2>&1 &
    echo $! > "$RUN/bridge.pid"
    sleep 2
  fi
  if [ "$EVENTS" = 1 ] && ! pid_alive events; then
    log "starting the event daemon on :$WS_GET_PORT/:$WS_POST_PORT (log: $RUN/events.log)"
    NODE_PATH="${NODE_PATH:-$CLASSROOM_DIR/node_modules}" nohup "$NODE" websocket/daemon.js > "$RUN/events.log" 2>&1 &
    echo $! > "$RUN/events.pid"
  fi
  if ! pid_alive judge; then
    log "starting judge $JUDGE_NAME (log: $RUN/judge.log)"
    nohup bash -c "$(declare -f as_judge_user); JUDGE_USER='$JUDGE_USER' JUDGE_HOME='$JUDGE_HOME' as_judge_user \
      '$JVENV/bin/dmoj' -c '$JUDGE_HOME/judge.yml' -p '$BRIDGE_JUDGE_PORT' --no-ansi 127.0.0.1" \
      > "$RUN/judge.log" 2>&1 &
    echo $! > "$RUN/judge.pid"
  fi
  log "started. Run the site with:"
  echo "  CLASSROOM_PROBLEM_DATA_ROOT=$PROBLEMS CLASSROOM_BRIDGE_JUDGE_PORT=$BRIDGE_JUDGE_PORT" \
       "CLASSROOM_BRIDGE_DJANGO_PORT=$BRIDGE_DJANGO_PORT$([ "$EVENTS" = 1 ] && echo " CLASSROOM_EVENT_DAEMON=1 WS_GET_PORT=$WS_GET_PORT WS_POST_PORT=$WS_POST_PORT")" \
       "bash classroom/scripts/dev-server.sh"
}

stop() {
  for name in judge events bridge; do
    if pid_alive "$name"; then
      log "stopping $name"
      pkill -TERM -P "$(cat "$RUN/$name.pid")" 2>/dev/null || true
      kill "$(cat "$RUN/$name.pid")" 2>/dev/null || true
    fi
    rm -f "$RUN/$name.pid"
  done
}

status() {
  for name in bridge events judge; do
    if pid_alive "$name"; then echo "$name: running (pid $(cat "$RUN/$name.pid"))"; else echo "$name: stopped"; fi
  done
  (cd "$CLASSROOM_DIR" && "$PY" manage.py shell -c '
from judge.models import Judge
for j in Judge.objects.all():
    print("judge %s: %s, problems: %s" % (j.name, "online" if j.online else "offline",
                                          ", ".join(j.problems.values_list("code", flat=True)) or "-"))
')
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
