#!/usr/bin/env python
"""Sample CPU and memory of the app server, MariaDB, Redis and the load generator during a run.

    python scripts/loadtest/monitor.py OUT.csv [--interval 5] [--redis-port 6392]

Writes one CSV row per interval: total CPU busy % of the machine, then for each process group
(gunicorn, mariadbd, redis, locust) the summed CPU % (100 = one core) and RSS in MB, plus Redis
ops/s and connected clients. Stops on Ctrl-C / SIGTERM. Needs psutil (installed with locust).
"""
import argparse
import csv
import signal
import socket
import sys
import time

import psutil

GROUPS = {
    'gunicorn': lambda p: 'gunicorn' in ' '.join(p.info['cmdline'] or []),
    'mariadb': lambda p: p.info['name'] in ('mariadbd', 'mysqld'),
    'redis': lambda p: p.info['name'] == 'redis-server',
    'locust': lambda p: 'locust' in ' '.join(p.info['cmdline'] or []),
}


def redis_info(port):
    try:
        s = socket.create_connection(('127.0.0.1', port), timeout=1)
        s.sendall(b'INFO\r\n')
        data = b''
        while True:
            chunk = s.recv(65536)
            data += chunk
            if not chunk or data.count(b'\r\n') > 5 and data.endswith(b'\r\n\r\n') or len(chunk) < 65536:
                break
        s.close()
        info = dict(line.split(':', 1) for line in data.decode(errors='ignore').splitlines() if ':' in line)
        return info.get('instantaneous_ops_per_sec', ''), info.get('connected_clients', '')
    except OSError:
        return '', ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--interval', type=float, default=5)
    ap.add_argument('--redis-port', type=int, default=6392)
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    procs = {}
    psutil.cpu_percent()
    with open(args.out, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['t', 'cpu_total_pct'] + ['%s_%s' % (g, k) for g in GROUPS for k in ('cpu', 'rss_mb', 'n')]
                   + ['redis_ops', 'redis_clients'])
        t0 = time.time()
        while True:
            for p in psutil.process_iter(['pid', 'name', 'cmdline']):
                if p.pid not in procs:
                    for g, match in GROUPS.items():
                        try:
                            if match(p):
                                procs[p.pid] = (g, p)
                                p.cpu_percent()
                        except (psutil.Error, TypeError):
                            pass
                        if p.pid in procs:
                            break
            time.sleep(args.interval)
            totals = {g: [0.0, 0.0, 0] for g in GROUPS}
            for pid, (g, p) in list(procs.items()):
                try:
                    totals[g][0] += p.cpu_percent()
                    totals[g][1] += p.memory_info().rss / 2**20
                    totals[g][2] += 1
                except psutil.Error:
                    del procs[pid]
            row = [round(time.time() - t0), psutil.cpu_percent()]
            for g in GROUPS:
                row += [round(totals[g][0]), round(totals[g][1]), totals[g][2]]
            row += list(redis_info(args.redis_port))
            w.writerow(row)
            f.flush()


if __name__ == '__main__':
    main()
