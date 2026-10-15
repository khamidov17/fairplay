#!/bin/sh
# Build-time step of Dockerfile.demo: settings, an initialised database with the schema
# applied, compiled translations. Demo data itself is loaded at start (scripts/demo/start.sh)
# so assignment times are relative to when the demo starts.
set -e
cd /app
cp dmoj/local_settings.example.py dmoj/local_settings.py
cat scripts/demo/settings.py >> dmoj/local_settings.py
sh scripts/demo/services.sh
mysql_tzinfo_to_sql /usr/share/zoneinfo 2>/dev/null | mariadb -uroot mysql
mariadb -uroot <<SQL
CREATE DATABASE IF NOT EXISTS classroom CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'classroom'@'localhost' IDENTIFIED BY 'classroom';
CREATE USER IF NOT EXISTS 'classroom'@'127.0.0.1' IDENTIFIED BY 'classroom';
GRANT ALL ON classroom.* TO 'classroom'@'localhost';
GRANT ALL ON classroom.* TO 'classroom'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
python manage.py migrate --noinput
python manage.py loaddata navbar language_small demo
python manage.py compilejsi18n
mariadb-admin -uroot shutdown
redis-cli shutdown nosave || true
