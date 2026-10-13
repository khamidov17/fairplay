#!/bin/bash
# Double-click: runs the FairPlay server on http://127.0.0.1:8000 (SQLite, single process).
cd "$(dirname "$0")/server"
PY=""
for c in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "Python 3.10+ not found. Install it from python.org"; read -n1; exit 1; }
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import sys" 2>/dev/null; then
  echo "==> First run: creating Python environment with $PY ..."
  rm -rf .venv 2>/dev/null; "$PY" -m venv .venv || { echo "venv failed"; read -n1; exit 1; }
  .venv/bin/python -m pip install -q --upgrade pip
fi
echo "==> Checking Python packages (first time after an update takes a minute)..."
.venv/bin/python -m pip install -q -r requirements.txt || { echo "pip install failed"; read -n1; exit 1; }
HOST="${HOST:-127.0.0.1}"
# Local demo: random secret kept in data/secret.key, public dev app key (matches a
# dev build of the Mac app). Never use FAIRPLAY_DEV=1 for real exams.
export FAIRPLAY_DEV="${FAIRPLAY_DEV:-1}"
echo "==> FairPlay server on http://$HOST:8000   teacher UI: http://127.0.0.1:8000/teacher"
echo "    Demo: teacher/teacher123  alice/alice123  bob/bob123  carol/carol123   (Ctrl-C to stop)"
( sleep 2; open "http://127.0.0.1:8000/teacher" ) &
exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port 8000
