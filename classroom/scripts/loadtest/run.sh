#!/bin/bash
# Run one load-test round against an already running app server.
#
#   bash scripts/loadtest/run.sh OUTDIR [users] [ramp_seconds] [duration]
#
# Re-seeds the load-test data (fresh participations), flushes the Redis cache, turns the
# MariaDB slow query log on (long_query_time=0.2) for the run, samples CPU/memory with
# monitor.py and runs Locust headless. Results: OUTDIR/{stats_stats.csv,stats_stats_history.csv,
# summary.txt,monitor.csv,slow.log}.
#
# Env: LT_PY (python with Django deps), LT_LOCUST (locust binary), LT_HOST (default
# http://127.0.0.1:8201), LT_MON_PY (python with psutil, default LT_PY), LT_REDIS_PORT (default 6392), plus the LT_* knobs of locustfile.py and the
# CLASSROOM_DB_* settings of the app.
set -euo pipefail
OUT=${1:?outdir}; USERS=${2:-300}; RAMP=${3:-120}; DURATION=${4:-9m}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${LT_PY:-python}
LOCUST=${LT_LOCUST:-locust}
HOST=${LT_HOST:-http://127.0.0.1:8201}
RPORT=${LT_REDIS_PORT:-6392}
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

(cd "$HERE/../.." && "$PY" scripts/loadtest/seed.py --students "$USERS" --reset)
redis-cli -p "$RPORT" FLUSHALL > /dev/null
SLOW=/var/lib/mysql/lt-slow.log
mariadb -uroot -e "SET GLOBAL slow_query_log=OFF; SET GLOBAL slow_query_log_file='$SLOW'; \
  SET GLOBAL long_query_time=0.2; SET GLOBAL slow_query_log=ON; FLUSH SLOW LOGS;"
sudo_truncate() { : > "$1" 2>/dev/null || true; }
sudo_truncate "$SLOW"

SPAWN=$(python3 -c "print(round($USERS / $RAMP, 3))")
"${LT_MON_PY:-$PY}" "$HERE/monitor.py" "$OUT/monitor.csv" --redis-port "$RPORT" &
MON=$!
trap 'kill $MON 2>/dev/null || true' EXIT
"$LOCUST" -f "$HERE/locustfile.py" --headless -u $((USERS + 1)) -r "$SPAWN" -t "$DURATION" -H "$HOST" \
  --csv "$OUT/stats" --csv-full-history --only-summary > "$OUT/summary.txt" 2>&1 || true
kill $MON 2>/dev/null || true
mariadb -uroot -e "SET GLOBAL slow_query_log=OFF; SET GLOBAL long_query_time=10;"
cp "$SLOW" "$OUT/slow.log" 2>/dev/null || true
tail -60 "$OUT/summary.txt"
