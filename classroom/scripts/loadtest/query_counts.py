#!/usr/bin/env python
"""Count SQL queries and time per request for the load test's hot endpoints.

    CLASSROOM_ALLOWED_HOSTS=testserver python scripts/loadtest/query_counts.py [--user lt001] [--verbose] [--all]

Runs against the seeded load-test database (scripts/loadtest/seed.py; best after a load run,
so the exam has ~300 participations and thousands of submissions). Uses Django's test client
in-process, so it measures the view code only (no app server, no network). With --verbose it
prints the queries that repeat with different parameters (the N+1 suspects).
"""
import argparse
import os
import re
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dmoj.settings')
try:
    import MySQLdb  # noqa: F401
except ImportError:
    import pymysql
    pymysql.install_as_MySQLdb()

import django  # noqa: E402
django.setup()  # noqa: I202
from django.contrib.auth.models import User  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from judge.models import Contest  # noqa: E402

NUM = re.compile(r"\b\d+\b|'[^']*'")


def measure(client, method, url, verbose, **kw):
    with CaptureQueriesContext(connection) as ctx:
        start = time.perf_counter()
        response = getattr(client, method)(url, **kw)
        elapsed = (time.perf_counter() - start) * 1000
    shapes = Counter(NUM.sub('?', q['sql']) for q in ctx.captured_queries)
    repeated = [(n, s) for s, n in shapes.most_common() if n > 2]
    print('%-4s %-62s %3d  %4d queries  %6.0f ms%s' % (
        method.upper(), url[:62], response.status_code, len(ctx), elapsed,
        '  (max repeat %d)' % repeated[0][0] if repeated else ''))
    if verbose:
        for n, s in repeated[:6]:
            print('        x%-3d %s' % (n, s[:220]))
    return response


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--user', default='lt001')
    ap.add_argument('--teacher', default='lt_prof')
    ap.add_argument('--exam', default='lt300exam')
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--all', action='store_true', help='every question, not one of each answer type')
    args = ap.parse_args()
    v = args.verbose
    contest = Contest.objects.get(key=args.exam)
    codes = list(contest.contest_problems.order_by('order').values_list('problem__code', 'problem__submission_type'))

    client = Client()
    client.force_login(User.objects.get(username=args.user))
    measure(client, 'get', '/contest/%s' % args.exam, v)
    measure(client, 'post', '/contest/%s/join' % args.exam, v)
    measure(client, 'get', '/contest/%s' % args.exam, v)
    if not args.all:
        # One question of each answer type is enough to see the per-request costs.
        codes = list({kind: (code, kind) for code, kind in reversed(codes)}.values())
    for code, kind in codes:
        if kind not in ('M', 'B', 'S'):
            continue
        measure(client, 'get', '/problem/%s' % code, v)
        measure(client, 'get', '/problem/%s/workspace/submissions/?scope=my' % code, v,
                HTTP_ACCEPT='application/json')
        if kind == 'S':
            draft, form = '{"text": "hello"}', {'answer': 'hello'}
        elif kind == 'B':
            draft, form = '{"answers": ["for"]}', {'fb_0': 'for'}
        else:
            option = re.search(r'name="options"[^>]*value="(\d+)"',
                               client.get('/problem/%s' % code).content.decode()).group(1)
            draft, form = '{"selected": [%s]}' % option, {'options': option}
        measure(client, 'post', '/contest/%s/draft/%s' % (args.exam, code), v, data={'content': draft})
        form['workspace'] = '1'
        r = measure(client, 'post', '/problem/%s/submit' % code, v, data=form, HTTP_X_WORKSPACE_SUBMIT='1')
        if r.status_code == 200 and r.json().get('status_url'):
            measure(client, 'get', r.json()['status_url'], v, HTTP_ACCEPT='application/json')
    measure(client, 'get', '/contest/%s/submissions/%s/' % (args.exam, args.user), v)
    measure(client, 'post', '/contest/%s/leave' % args.exam, v)

    teacher = Client()
    teacher.force_login(User.objects.get(username=args.teacher))
    measure(teacher, 'get', '/contest/%s/grade' % args.exam, v)
    measure(teacher, 'get', '/contest/%s/analytics' % args.exam, v)
    measure(teacher, 'get', '/contest/%s/ranking/' % args.exam, v)


if __name__ == '__main__':
    main()
