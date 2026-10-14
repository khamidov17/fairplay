#!/usr/bin/env python
"""Seed a 300-student course for the Classroom load test (docs/classroom/reports/load-test.md).

Creates, in the database named by the usual CLASSROOM_DB_* settings:

* teacher ``lt_prof`` (instructor, admin of course LT300) and students ``lt001`` .. ``ltNNN``,
  all with password ``loadtest-pw``;
* course ``LT300`` with one class ``LT300-01`` holding every student;
* a running exam ``lt300exam`` (format ``exam``, results hidden) of 10 questions: 4 multiple
  choice, 3 fill in the blank, 3 short answer (no code, so no judge is needed, no AI);
* a running homework ``lt300hw`` (format ``default``) of 4 other questions of the same types.

Run from classroom/ with the app's virtualenv:

    python scripts/loadtest/seed.py [--students 300] [--reset]

``--reset`` deletes everything a previous run created first (users ``lt*``, course, problems).
It refuses to touch a database whose name does not contain ``lt`` or ``load`` unless
``--force`` is given, so it cannot be pointed at a real site by accident.
"""
import argparse
import os
import sys
from datetime import timedelta

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
from django.conf import settings  # noqa: E402
from django.contrib.auth.hashers import make_password  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from judge.models import (Class, Contest, ContestProblem, Language, Organization, Problem,  # noqa: E402
                          ProblemBlank, ProblemFillBlankConfig, ProblemGroup, ProblemMCQConfig, ProblemMCQOption,
                          ProblemShortAnswerConfig, ProblemType, Profile)
from judge.models.notification import ensure_instructors_group  # noqa: E402

PASSWORD = 'loadtest-pw'
COURSE_SLUG = 'lt300'
EXAM_KEY = 'lt300exam'
HW_KEY = 'lt300hw'

# (code suffix, type, text, payload) — payload: MCQ options+correct index, blank answer, or reference.
EXAM_QUESTIONS = [
    ('mcq1', 'M', 'Which keyword defines a function in Python?', (['func', 'def', 'lambda', 'fn'], 1)),
    ('mcq2', 'M', 'What does `len([1, 2, 3])` return?', (['2', '3', '4'], 1)),
    ('mcq3', 'M', 'Which type is immutable?', (['list', 'dict', 'tuple', 'set'], 2)),
    ('mcq4', 'M', 'What is `7 // 2`?', (['3', '3.5', '4'], 0)),
    ('blank1', 'B', 'A loop over a range uses the ___ keyword.', 'for'),
    ('blank2', 'B', 'The value of an empty function call result is ___.', 'None'),
    ('blank3', 'B', 'Exceptions are caught with try and ___.', 'except'),
    ('short1', 'S', 'Explain the difference between a list and a tuple.', 'Lists are mutable; tuples are not.'),
    ('short2', 'S', 'What is a dictionary used for?', 'Mapping keys to values.'),
    ('short3', 'S', 'Why write functions?', 'Reuse and readability.'),
]
HW_QUESTIONS = [
    ('hwmcq', 'M', 'What does `print(2 ** 3)` show?', (['6', '8', '9'], 1)),
    ('hwblank', 'B', 'Strings are joined with the ___ method.', 'join'),
    ('hwshort1', 'S', 'Describe what a variable is.', 'A name bound to a value.'),
    ('hwshort2', 'S', 'What does a return statement do?', 'Ends the function and gives back a value.'),
]


def reset():
    Contest.objects.filter(key__in=(EXAM_KEY, HW_KEY)).delete()
    Problem.objects.filter(code__startswith='lt_').delete()
    Class.objects.filter(slug='lt300-01').delete()
    Organization.objects.filter(slug=COURSE_SLUG).delete()
    User.objects.filter(username__regex=r'^lt(\d+|_prof)$').delete()


def make_users(count, password_hash, language):
    prof_user = User.objects.create(username='lt_prof', email='lt_prof@example.edu', first_name='Load',
                                    last_name='Teacher', password=password_hash, is_active=True)
    prof = Profile.objects.create(user=prof_user, language=language)
    prof_user.groups.add(ensure_instructors_group())
    users = User.objects.bulk_create([
        User(username='lt%03d' % i, email='lt%03d@example.edu' % i, first_name='Student', last_name='%03d' % i,
             password=password_hash, is_active=True)
        for i in range(1, count + 1)
    ])
    # bulk_create on MySQL does not return primary keys.
    users = list(User.objects.filter(username__regex=r'^lt\d+$').order_by('username'))
    Profile.objects.bulk_create([Profile(user=u, language=language) for u in users])
    students = list(Profile.objects.filter(user__in=users))
    return prof, students


def make_problem(code, kind, text, payload, course, author, group, ptype, public):
    problem = Problem.objects.create(
        code='lt_' + code, name=text[:60], description=text, points=5, partial=True, time_limit=2,
        memory_limit=262144, group=group, submission_type=kind, is_public=public, is_organization_private=True,
    )
    problem.types.add(ptype)
    problem.authors.add(author)
    problem.organizations.add(course)
    if kind == 'M':
        options, correct = payload
        ProblemMCQConfig.objects.create(problem=problem, multiple_select=False)
        for i, option in enumerate(options):
            ProblemMCQOption.objects.create(problem=problem, order=i, text=option, is_correct=(i == correct))
    elif kind == 'B':
        ProblemFillBlankConfig.objects.create(problem=problem)
        ProblemBlank.objects.create(problem=problem, order=0, label='', answers=payload)
    elif kind == 'S':
        ProblemShortAnswerConfig.objects.create(problem=problem, reference_answer=payload)
    return problem


def seed(count):
    now = timezone.now()
    language = Language.get_default_language()
    group, _ = ProblemGroup.objects.get_or_create(name='course', defaults={'full_name': 'Course work'})
    ptype, _ = ProblemType.objects.get_or_create(name='exercise', defaults={'full_name': 'Exercise'})
    prof, students = make_users(count, make_password(PASSWORD), language)

    course = Organization.objects.create(name='LT300 Load test course', slug=COURSE_SLUG, short_name='LT300',
                                         subject='cs', is_open=False, about='Load test.')
    course.admins.add(prof)
    course.instructors.add(prof)
    course.members.add(*students)
    section = Class.objects.create(organization=course, name='LT300-01', slug='lt300-01', description='All students.',
                                   academic_year='2026-2027', term_session='Fall 2026')
    section.members.add(*students)

    def assignment(key, name, fmt, questions, public):
        contest = Contest.objects.create(
            key=key, name=name, format_name=fmt, start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(days=2), is_visible=True, is_organization_private=True,
            scoreboard_visibility=Contest.SCOREBOARD_AFTER_CONTEST, description='Load test %s.' % name)
        contest.authors.add(prof)
        contest.organizations.add(course)
        contest.classes.add(section)
        for order, (code, kind, text, payload) in enumerate(questions, start=1):
            problem = make_problem(code, kind, text, payload, course, prof, group, ptype, public)
            ContestProblem.objects.create(contest=contest, problem=problem, points=5, order=order, partial=True)
        return contest

    assignment(EXAM_KEY, 'Load test exam', 'exam', EXAM_QUESTIONS, public=False)
    assignment(HW_KEY, 'Load test homework', 'default', HW_QUESTIONS, public=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--students', type=int, default=300)
    parser.add_argument('--reset', action='store_true', help='delete a previous load-test seed first')
    parser.add_argument('--force', action='store_true', help='allow a database name without "lt"/"load"')
    args = parser.parse_args()
    name = settings.DATABASES['default']['NAME']
    if not args.force and 'lt' not in name and 'load' not in name:
        sys.exit('Refusing to seed database %r (name lacks "lt"/"load"; pass --force).' % name)
    with transaction.atomic():
        if args.reset:
            reset()
        elif User.objects.filter(username='lt_prof').exists():
            sys.exit('Already seeded (lt_prof exists). Use --reset to recreate.')
        seed(args.students)
    print('Seeded %d students in %s: exam %s, homework %s; password %r.'
          % (args.students, name, EXAM_KEY, HW_KEY, PASSWORD))


if __name__ == '__main__':
    main()
