#!/bin/sh
# Start MariaDB (small memory footprint) and Redis in the background and wait for them.
mkdir -p /run/mysqld && chown mysql:mysql /run/mysqld
[ -d /var/lib/mysql/mysql ] || mariadb-install-db --user=mysql --datadir=/var/lib/mysql >/dev/null
mariadbd --user=mysql --skip-name-resolve --innodb-buffer-pool-size=32M --performance-schema=OFF \
         --key-buffer-size=8M --max-connections=40 --bind-address=127.0.0.1 >/tmp/mariadb.log 2>&1 &
redis-server --daemonize yes --save '' --appendonly no --maxmemory 32mb >/dev/null
for i in $(seq 1 60); do mariadb-admin ping --silent 2>/dev/null && break; sleep 1; done
mariadb-admin ping --silent || { cat /tmp/mariadb.log; exit 1; }
