#!/bin/sh
# Start script for docker-compose.local.yml: settings, database, demo data, dev server.
set -e
cd /app
[ -f dmoj/local_settings.py ] || cp dmoj/local_settings.example.py dmoj/local_settings.py
python manage.py migrate --noinput
python manage.py compilejsi18n >/dev/null
if ! python manage.py shell -c "from django.contrib.auth.models import User; import sys; sys.exit(0 if User.objects.filter(username='prof_li').exists() else 1)"; then
    echo "[classroom] loading demo data"
    python manage.py loaddata navbar language_small demo
    python manage.py seed_dev_data
fi
echo "[classroom] ready: open http://localhost:8000  (users admin, prof_li, ta_wang, ab101..ab105; password fairplay-dev)"
exec python manage.py runserver --noreload 0.0.0.0:8000
