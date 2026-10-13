#!/usr/bin/env python3
"""DEV BUILDS ONLY: offline exit code for a locked dev FairPlay Mac, without the server.

    python3 server/tools/offline_exit_code.py K7QM-3XPA

Real exams: the code comes from the teacher's live room (pick the student, type the Mac
code). It is derived from a per-attempt key the server gives the app, never from the
app key, so this tool cannot produce it. A dev build that never got that key falls back
to the app key, which is what this computes (public dev key unless FAIRPLAY_APP_KEY).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys

DEV_APP_KEY = "fairplay-dev-app-key-not-secret"
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def code_for(key: str, mac_code: str) -> str:
    mac = hmac.new(key.encode(), f"offline-exit|{mac_code}".encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(mac[:4], 'big') % 100_000_000:08d}"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip())
        return 2
    c = "".join(ch for ch in argv[1].upper() if ch.isalnum())
    if len(c) != 8 or any(ch not in ALPHABET for ch in c):
        print("A Mac code has 8 letters and digits, like K7QM-3XPA")
        return 2
    keys = [k.strip() for k in os.environ.get("FAIRPLAY_APP_KEY", "").split(",") if k.strip()]
    if not keys:
        print("(FAIRPLAY_APP_KEY not set: dev builds only)")
        keys = [DEV_APP_KEY]
    for k in keys:
        print(code_for(k, c))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
