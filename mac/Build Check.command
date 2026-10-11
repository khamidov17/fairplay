#!/bin/bash
# Builds only and writes build.log (used to check the build remotely).
cd "$(dirname "$0")"
bash ./build.sh > build.log 2>&1; echo "EXIT $?" >> build.log
tail -5 build.log
