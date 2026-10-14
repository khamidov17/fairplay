"""Fill a local development database with a small, realistic Classroom.

Only for local development and click-testing (refuses to run unless DEBUG is on). It creates:

* ``admin`` (superuser), ``prof_li`` (instructor, admin of two courses and one class),
  ``ta_wang`` (class admin) and five students ``ab101`` .. ``ab105``. All passwords are
  ``fairplay-dev``.
* Courses ``CS101`` (subject cs) and ``MATH201`` (subject math), class ``CS101-001``.
* One problem of each question type in CS101: code, multiple choice, fill in the blank,
  short answer and file upload, plus one MCQ in MATH201, and the hidden ``idepractice`` problem
  that IDE runs use (judge data for it: ``scripts/dev-judge.sh``).
* Assignments: a running homework, a running exam (results hidden), an upcoming quiz, and an
  ended, finalized quiz with graded answers.

Run it on a freshly migrated database (after ``loaddata navbar language_small demo``):

    .venv/bin/python manage.py seed_dev_data
"""
import json
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.flatpages.models import FlatPage
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from judge.models import (Class, Contest, ContestParticipation, ContestProblem, ContestSubmission, Language,
                          Organization, Problem, ProblemBlank, ProblemFileConfig, ProblemFillBlankConfig,
                          ProblemGroup, ProblemMCQConfig, ProblemMCQOption, ProblemShortAnswerConfig,
                          ProblemType, Profile, Submission, SubmissionSource)
from judge.models.notification import ensure_instructors_group

PASSWORD = 'fairplay-dev'


def create_about_page():
    """The navigation bar (``loaddata navbar``) links to /about/, a flat page every site creates."""
    about, _created = FlatPage.objects.get_or_create(url='/about/', defaults={
        'title': 'About', 'content': '<p>DKU OJ: courses, assignments and an online judge.</p>'})
    about.sites.add(Site.objects.get_current())
    return about


class Command(BaseCommand):
    help = 'Fill a local development database with a small Classroom for click-testing (DEBUG only).'

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError('seed_dev_data only runs with DEBUG on.')
        if User.objects.filter(username='prof_li').exists():
            raise CommandError('The database is already seeded (user prof_li exists).')
        with transaction.atomic():
            self._seed()
        self.stdout.write(self.style.SUCCESS('Seeded. Users: admin, prof_li, ta_wang, ab101..ab105; '
                                             'password "%s".' % PASSWORD))

    # ------------------------------------------------------------------ helpers
    def _user(self, username, first, last, *, staff=False, superuser=False):
        # The demo fixture already has ``admin``; reuse it with the known password.
        user = User.objects.filter(username=username).first() or User(username=username)
        user.email = '%s@example.edu' % username
        user.first_name, user.last_name = first, last
        user.set_password(PASSWORD)
        user.is_active = True
        user.is_staff = staff
        user.is_superuser = superuser
        user.save()
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.language = Language.get_default_language()
        profile.save()
        return profile

    def _problem(self, code, name, submission_type, course, author, description, points=10, public=True):
        problem = Problem.objects.create(
            code=code, name=name, description=description, points=points, partial=True,
            time_limit=2, memory_limit=262144, group=self.group, submission_type=submission_type,
            is_public=public, is_organization_private=True,
        )
        problem.types.add(self.ptype)
        problem.authors.add(author)
        problem.organizations.add(course)
        if submission_type == 'C':
            problem.allowed_languages.set(Language.objects.filter(key__in=('PY3', 'CPP11', 'C', 'JAVA8')))
        return problem

    def _answer(self, profile, problem, contest, contest_problem, participation, source, result, points):
        sub = Submission.objects.create(
            user=profile, problem=problem, language=Language.get_answer_language(), status='D',
            result=result, points=points, case_points=points, case_total=contest_problem.points,
            time=0.0, memory=0, contest_object=contest,
        )
        SubmissionSource.objects.create(submission=sub, source=json.dumps(source))
        ContestSubmission.objects.create(submission=sub, problem=contest_problem, participation=participation,
                                         points=points)
        return sub

    # ------------------------------------------------------------------ data
    def _seed(self):
        now = timezone.now()
        self.group, _ = ProblemGroup.objects.get_or_create(name='course', defaults={'full_name': 'Course work'})
        self.ptype, _ = ProblemType.objects.get_or_create(name='exercise', defaults={'full_name': 'Exercise'})

        self._user('admin', 'Site', 'Admin', staff=True, superuser=True)
        prof = self._user('prof_li', 'Wei', 'Li')
        ta = self._user('ta_wang', 'Fang', 'Wang')
        # What approving an instructor does: the group with its permissions attached.
        instructors = ensure_instructors_group()
        prof.user.groups.add(instructors)
        students = [self._user('ab10%d' % i, first, 'Student')
                    for i, first in enumerate(('Aziz', 'Bella', 'Chen', 'Dilnoza', 'Emre'), start=1)]

        cs = Organization.objects.create(name='CS101 Intro to Programming', slug='cs101', short_name='CS101',
                                         subject='cs', is_open=False, about='Introductory programming.')
        math = Organization.objects.create(name='MATH201 Linear Algebra', slug='math201', short_name='MATH201',
                                           subject='math', is_open=False, about='Matrices and vector spaces.')
        for course in (cs, math):
            course.admins.add(prof)
            course.instructors.add(prof)
        cs.members.add(*students, ta)
        math.members.add(*students[:3])
        section = Class.objects.create(organization=cs, name='CS101-001', slug='cs101-001',
                                       description='Monday/Wednesday section.', academic_year='2026-2027',
                                       term_session='Fall 2026')
        section.admins.add(ta)
        section.members.add(*students)

        code = self._problem('cs_sum', 'Sum of two numbers', 'C', cs, prof,
                             'Read two integers ~a~ and ~b~ and print ~a + b~.')
        mcq = self._problem('cs_mcq_loop', 'How many times does the loop run?', 'M', cs, prof,
                            'How many times does `for i in range(3):` run its body?', points=5)
        ProblemMCQConfig.objects.create(problem=mcq, multiple_select=False)
        mcq_options = [ProblemMCQOption.objects.create(problem=mcq, order=i, text=text, is_correct=(text == '3'))
                       for i, text in enumerate(('2', '3', '4'))]
        blank = self._problem('cs_blank_print', 'Complete the print call', 'B', cs, prof,
                              'Python prints text with the ___ function.', points=5)
        ProblemFillBlankConfig.objects.create(problem=blank)
        ProblemBlank.objects.create(problem=blank, order=0, label='', answers='print')
        short = self._problem('cs_short_mutable', 'Lists vs tuples', 'S', cs, prof,
                              'In one or two sentences, explain the difference between a list and a tuple.')
        ProblemShortAnswerConfig.objects.create(problem=short, reference_answer='Lists are mutable; tuples are not.')
        upload = self._problem('cs_file_report', 'Upload your lab report', 'F', cs, prof,
                               'Upload your lab report as a PDF.')
        ProblemFileConfig.objects.create(problem=upload, allowed_extensions='pdf')
        # Exams get their own questions: a question used in a running or upcoming exam that hides
        # results cannot be submitted anywhere else meanwhile (G-7), so sharing one with the
        # homework would close it there.
        exam_mcq = self._problem('cs_exam_scope', 'Variable scope', 'M', cs, prof,
                                 'Which keyword makes an assignment inside a function change a module variable?',
                                 points=5, public=False)
        ProblemMCQConfig.objects.create(problem=exam_mcq, multiple_select=False)
        exam_options = [ProblemMCQOption.objects.create(problem=exam_mcq, order=i, text=text,
                                                        is_correct=(text == 'global'))
                        for i, text in enumerate(('local', 'global', 'static'))]
        exam_blank = self._problem('cs_exam_len', 'Length of a list', 'B', cs, prof,
                                   'The built-in ___ returns the number of items in a list.', points=5, public=False)
        ProblemFillBlankConfig.objects.create(problem=exam_blank)
        ProblemBlank.objects.create(problem=exam_blank, order=0, label='', answers='len')
        quiz2_mcq = self._problem('cs_quiz2_types', 'Type of 3 / 2', 'M', cs, prof,
                                  'What is the type of `3 / 2` in Python 3?', points=5, public=False)
        ProblemMCQConfig.objects.create(problem=quiz2_mcq, multiple_select=False)
        for i, text in enumerate(('int', 'float')):
            ProblemMCQOption.objects.create(problem=quiz2_mcq, order=i, text=text, is_correct=(text == 'float'))
        # The hidden problem that /ide/ and the workspace "Run test" are judged as (judge/views/ide.py).
        # Its judge-side data comes from scripts/dev-judge.sh.
        ide = Problem.objects.create(
            code='idepractice', name='IDE sandbox', description='Code runs from the IDE.', points=1,
            partial=False, time_limit=2, memory_limit=65536, group=self.group, is_public=False,
        )
        ide.types.add(self.ptype)
        ide.allowed_languages.set(Language.objects.exclude(key='ANSWER'))
        math_mcq = self._problem('math_det', 'Determinant of the identity', 'M', math, prof,
                                 'What is ~\\det(I_3)~?', points=5)
        ProblemMCQConfig.objects.create(problem=math_mcq, multiple_select=False)
        for i, text in enumerate(('0', '1', '3')):
            ProblemMCQOption.objects.create(problem=math_mcq, order=i, text=text, is_correct=(text == '1'))

        def assignment(key, name, fmt, start, end, problems, course=cs, **extra):
            contest = Contest.objects.create(
                key=key, name=name, format_name=fmt, start_time=start, end_time=end, is_visible=True,
                is_organization_private=True, scoreboard_visibility=Contest.SCOREBOARD_AFTER_CONTEST,
                description='Assignment %s.' % name, **extra)
            contest.authors.add(prof)
            contest.organizations.add(course)
            if course == cs:
                contest.classes.add(section)
            rows = [ContestProblem.objects.create(contest=contest, problem=p, points=p.points, order=i,
                                                  partial=True)
                    for i, p in enumerate(problems, start=1)]
            return contest, rows

        assignment('cs101hw1', 'Homework 1', 'default', now - timedelta(days=1), now + timedelta(days=6),
                   (code, mcq, blank, short, upload))
        exam, exam_rows = assignment('cs101exam1', 'Midterm exam', 'exam', now - timedelta(minutes=30),
                                     now + timedelta(hours=2), (exam_mcq, exam_blank))
        assignment('cs101quiz2', 'Quiz 2 (upcoming)', 'exam', now + timedelta(days=3),
                   now + timedelta(days=3, hours=1), (quiz2_mcq,))
        quiz, quiz_rows = assignment('cs101quiz1', 'Quiz 1 (graded)', 'exam', now - timedelta(days=10),
                                     now - timedelta(days=10) + timedelta(hours=1), (mcq, blank))
        assignment('math201hw1', 'Linear algebra homework', 'default', now - timedelta(days=2),
                   now + timedelta(days=5), (math_mcq,), course=math)

        # The exam is in progress: two students have answered, results hidden until finalize.
        for student, option in ((students[0], exam_options[1]), (students[1], exam_options[0])):
            part = ContestParticipation.objects.create(contest=exam, user=student,
                                                       real_start=now - timedelta(minutes=20))
            right = option.is_correct
            self._answer(student, exam_mcq, exam, exam_rows[0], part, {'selected': [option.id]},
                         'AC' if right else 'WA', 5 if right else 0)
        # Quiz 1 is over and finalized: every student has answers and a score.
        for i, student in enumerate(students):
            part = ContestParticipation.objects.create(contest=quiz, user=student,
                                                       real_start=quiz.start_time + timedelta(minutes=1))
            option = mcq_options[1] if i % 2 == 0 else mcq_options[2]
            right = option.is_correct
            self._answer(student, mcq, quiz, quiz_rows[0], part, {'selected': [option.id]},
                         'AC' if right else 'WA', 5 if right else 0)
            self._answer(student, blank, quiz, quiz_rows[1], part, {'blanks': ['print' if i < 3 else 'echo']},
                         'AC' if i < 3 else 'WA', 5 if i < 3 else 0)
        quiz.results_finalized = True
        quiz.save(update_fields=['results_finalized'])
        # The exam format counts hidden results only once finalized (as the finalize task does).
        for part in ContestParticipation.objects.filter(contest=quiz):
            part.recompute_results()

        create_about_page()
