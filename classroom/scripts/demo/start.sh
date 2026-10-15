#!/bin/sh
# Runtime start of Dockerfile.demo: database + Redis, fresh demo data, web server on $PORT.
set -e
cd /app
sh scripts/demo/services.sh
CLASSROOM_DEBUG=1 python manage.py seed_dev_data
echo "[classroom] demo ready (users admin, prof_li, ta_wang, ab101..ab105; password fairplay-dev)"
# runserver --insecure serves the static files itself, so no separate web server is needed.
exec python manage.py runserver --noreload --insecure "0.0.0.0:${PORT:-10000}"
