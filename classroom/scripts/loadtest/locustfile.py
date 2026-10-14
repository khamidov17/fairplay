"""Locust scenario: 300 students sit an exam while a teacher grades (scripts/loadtest/README.md).

Every student: log in, open the exam, join, then for each of the 10 questions open the
workspace (page + its "my submissions" list), save a draft every DRAFT_MIN..DRAFT_MAX seconds
(DRAFTS_PER_QUESTION times), submit, poll the submission status once and reload the list; at the
end check the assignment submissions page and leave. Then the user starts over (logs in again).

The teacher (one user, fixed) loops over the grading page, analytics and the ranking.

Static files are not requested: in production nginx serves /static/ and the app server never sees
those requests.

Environment knobs:
    LT_EXAM=lt300exam  LT_PASSWORD=loadtest-pw  LT_STUDENTS=300
    LT_DRAFT_MIN=15 LT_DRAFT_MAX=30 LT_DRAFTS_PER_QUESTION=2   (seconds / count)
"""
import itertools
import json
import os
import random
import re
import threading

from locust import FastHttpUser, between, task

EXAM = os.environ.get('LT_EXAM', 'lt300exam')
PASSWORD = os.environ.get('LT_PASSWORD', 'loadtest-pw')
STUDENTS = int(os.environ.get('LT_STUDENTS', '300'))
DRAFT_MIN = float(os.environ.get('LT_DRAFT_MIN', '15'))
DRAFT_MAX = float(os.environ.get('LT_DRAFT_MAX', '30'))
DRAFTS_PER_QUESTION = int(os.environ.get('LT_DRAFTS_PER_QUESTION', '2'))

_ids = itertools.count(1)
_ids_lock = threading.Lock()

CSRF_RE = re.compile(r"name=['\"]csrfmiddlewaretoken['\"] value=['\"]([^'\"]+)")
OPTION_RE = re.compile(r'name="options"[^>]*value="(\d+)"|value="(\d+)"[^>]*name="options"')
PROBLEM_RE = re.compile(r'href="/problem/(lt_[a-z0-9]+)"')


def next_student():
    with _ids_lock:
        return 'lt%03d' % ((next(_ids) - 1) % STUDENTS + 1)


class Base(FastHttpUser):
    abstract = True
    username = None

    def csrf(self):
        for cookie in self.client.cookiejar:
            if cookie.name == 'csrftoken':
                return cookie.value
        return ''

    def post(self, url, data, name=None, headers=None, **kw):
        h = {'X-CSRFToken': self.csrf(), 'Referer': self.host + url}
        h.update(headers or {})
        return self.client.post(url, data=data, headers=h, name=name or url, **kw)

    def login(self):
        self.client.cookiejar.clear()
        with self.client.get('/accounts/login/', name='GET /accounts/login/', catch_response=True) as r:
            m = CSRF_RE.search(r.text or '')
            if not m:
                r.failure('no csrf token on login page')
                return False
        with self.post('/accounts/login/', {'csrfmiddlewaretoken': m.group(1), 'username': self.username,
                                            'password': PASSWORD},
                       name='POST /accounts/login/', allow_redirects=False, catch_response=True) as r:
            if r.status_code != 302:
                r.failure('login failed: HTTP %s' % r.status_code)
                return False
        return True


class Student(Base):
    wait_time = between(1, 3)

    def on_start(self):
        self.username = next_student()

    @task
    def sit_exam(self):
        if not self.login():
            self.wait()
            return
        r = self.client.get('/contest/%s' % EXAM, name='GET /contest/[key]')
        self.post('/contest/%s/join' % EXAM, {}, name='POST /contest/[key]/join', allow_redirects=False)
        r = self.client.get('/contest/%s' % EXAM, name='GET /contest/[key]')
        problems = list(dict.fromkeys(PROBLEM_RE.findall(r.text or '')))
        if not problems:
            # Not joined (or page changed): nothing else to do this round.
            self.client.get('/contest/%s' % EXAM, name='GET /contest/[key] (retry)')
            return
        for code in problems:
            self.answer(code)
        self.client.get('/contest/%s/submissions/%s/' % (EXAM, self.username),
                        name='GET /contest/[key]/submissions/[user]/')
        self.post('/contest/%s/leave' % EXAM, {}, name='POST /contest/[key]/leave', allow_redirects=False)

    def answer(self, code):
        r = self.client.get('/problem/%s' % code, name='GET /problem/[code] (workspace)')
        page = r.text or ''
        self.client.get('/problem/%s/workspace/submissions/?scope=my' % code,
                        name='GET /problem/[code]/workspace/submissions/', headers={'Accept': 'application/json'})
        if 'name="options"' in page:
            options = [a or b for a, b in OPTION_RE.findall(page)]
            choice = random.choice(options) if options else '0'
            draft = json.dumps({'selected': [int(choice)]})
            form = {'options': choice}
        elif 'name="fb_0"' in page:
            word = random.choice(['for', 'None', 'except', 'while'])
            draft = json.dumps({'answers': [word]})
            form = {'fb_0': word}
        else:
            words = ['list', 'tuple', 'value', 'mutable', 'keys', 'reuse']
            text = 'Answer from %s: ' % self.username + ' '.join(random.choice(words) for _ in range(40))
            draft = json.dumps({'text': text})
            form = {'answer': text}
        for _ in range(DRAFTS_PER_QUESTION):
            self._sleep(random.uniform(DRAFT_MIN, DRAFT_MAX))
            self.post('/contest/%s/draft/%s' % (EXAM, code), {'content': draft},
                      name='POST /contest/[key]/draft/[code]')
        form.update({'csrfmiddlewaretoken': self.csrf(), 'workspace': '1'})
        with self.post('/problem/%s/submit' % code, form, name='POST /problem/[code]/submit',
                       headers={'X-Workspace-Submit': '1', 'Accept': 'application/json'},
                       catch_response=True) as r:
            try:
                payload = r.json()
            except Exception:
                r.failure('submit: not JSON (HTTP %s)' % r.status_code)
                return
            if r.status_code != 200 or 'submission_id' not in payload:
                r.failure('submit: HTTP %s %s' % (r.status_code, str(payload)[:120]))
                return
        if payload.get('status_url'):
            self.client.get(payload['status_url'], name='GET /problem/[code]/workspace/submission/[id]',
                            headers={'Accept': 'application/json'})
        self.client.get('/problem/%s/workspace/submissions/?scope=my' % code,
                        name='GET /problem/[code]/workspace/submissions/', headers={'Accept': 'application/json'})

    def _sleep(self, seconds):
        import gevent
        gevent.sleep(seconds)


class Teacher(Base):
    fixed_count = 1
    wait_time = between(10, 20)

    def on_start(self):
        self.username = 'lt_prof'
        self.login()

    @task(3)
    def grade(self):
        self.client.get('/contest/%s/grade' % EXAM, name='TEACHER GET /contest/[key]/grade')

    @task(2)
    def analytics(self):
        self.client.get('/contest/%s/analytics' % EXAM, name='TEACHER GET /contest/[key]/analytics')

    @task(1)
    def ranking(self):
        self.client.get('/contest/%s/ranking/' % EXAM, name='TEACHER GET /contest/[key]/ranking/')
