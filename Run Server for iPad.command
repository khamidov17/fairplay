#!/bin/bash
# Double-click: FairPlay server reachable from your iPad/phone on the same Wi-Fi (local testing).
cd "$(dirname "$0")/server"
IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)"
echo "============================================================"
echo "  On your iPad (same Wi-Fi) open:  http://${IP:-THIS-MAC-IP}:8000/teacher"
echo "  Log in: teacher / teacher123"
echo "============================================================"
echo "==> Installing missing Python packages (only slow the first time)..."
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt \
  || .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
export FAIRPLAY_DEV=1
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
