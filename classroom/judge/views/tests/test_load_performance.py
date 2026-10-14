"""Query-count guards for the pages a 300-student exam hits (P3 load test).

Each test pins a fix from docs/classroom/reports/load-test.md: the cost of these pages must not
grow with the number of submissions, students or questions, and per-request work that cannot
change during a request must not be repeated.
"""
import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from judge.models import (Class, ContestParticipation, ContestProblem, ContestSubmission, Language, Organization,
                          Problem, ProblemBlank, ProblemFillBlankConfig, ProblemMCQConfig, ProblemMCQOption,
                          ProblemShortAnswerConfig, Submission, SubmissionSource)
from judge.models.tests.util import create_contest, create_problem, create_user
from judge.utils.contest_analytics import get_contest_analytics


def count_matching(ctx, needle):
    return sum(1 for q in ctx.captured_queries if needle in q['sql'])


class ExamLoadBase(TestCase):
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.teacher = create_user(username='lp_teacher')
        cls.course = Organization.objects.create(name='LP course', slug='lp-course', short_name='LP',
                                                 is_open=False)
        cls.course.admins.add(cls.teacher.profile)
        cls.section = Class.objects.create(organization=cls.course, name='LP-01', slug='lp-01')
        cls.contest = create_contest(
            key='lpexam', name='LP exam', format_name='exam', is_visible=True, is_organization_private=True,
            start_time=now - timedelta(hours=1), end_time=now + timedelta(days=1),
            authors=('lp_teacher',), organizations=('LP course',),
        )
        cls.contest.classes.add(cls.section)
        cls.problems = []
        for i in range(4):
            cls.add_question(i)
        cls.students = [cls.add_student(i) for i in range(2)]

    @classmethod
    def add_question(cls, i):
        kind = 'MBSM'[i % 4]
        problem = create_problem('lp-q%d' % i, name='Question %d' % i, submission_type=kind, is_public=False,
                                 points=5, organizations=('LP course',))
        if kind == 'M':
            ProblemMCQConfig.objects.create(problem=problem)
            for order in range(3):
                ProblemMCQOption.objects.create(problem=problem, order=order, text='o%d' % order,
                                                is_correct=order == 1)
        elif kind == 'B':
            ProblemFillBlankConfig.objects.create(problem=problem)
            ProblemBlank.objects.create(problem=problem, order=0, label='', answers='for')
        else:
            ProblemShortAnswerConfig.objects.create(problem=problem, reference_answer='ref')
        ContestProblem.objects.create(contest=cls.contest, problem=problem, points=5, order=i + 1)
        cls.problems.append(problem)
        return problem

    @classmethod
    def add_student(cls, i):
        user = create_user(username='lp_student%d' % i)
        cls.course.members.add(user.profile)
        cls.section.members.add(user.profile)
        return user

    def answer_everything(self, user):
        participation, _ = ContestParticipation.objects.get_or_create(contest=self.contest, user=user.profile)
        language = Language.get_answer_language()
        for cp in self.contest.contest_problems.select_related('problem'):
            problem = cp.problem
            if problem.submission_type == 'M':
                source = {'selected': [problem.mcq_options.order_by('order')[1].id]}
            elif problem.submission_type == 'B':
                source = {'answers': ['for']}
            else:
                source = {'text': 'an answer'}
            sub = Submission.objects.create(user=user.profile, problem=problem, language=language, status='D',
                                            result='AC', points=5, case_points=5, case_total=5,
                                            contest_object=self.contest)
            SubmissionSource.objects.create(submission=sub, source=json.dumps(source))
            ContestSubmission.objects.create(submission=sub, problem=cp, participation=participation, points=5)
        return participation

    def join(self, user):
        participation, _ = ContestParticipation.objects.get_or_create(contest=self.contest, user=user.profile)
        user.profile.current_contest = participation
        user.profile.save()
        return participation

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client


class GradeDeskQueryCountTestCase(ExamLoadBase):
    """The grade desk inlines every submission; its query count must not depend on how many."""

    def grade_queries(self):
        client = self.client_for(self.teacher)
        client.get(reverse('contest_grade', args=[self.contest.key]))  # warm per-process caches
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(reverse('contest_grade', args=[self.contest.key]))
        self.assertEqual(response.status_code, 200)
        return len(ctx), response

    def test_query_count_is_independent_of_submission_count(self):
        self.answer_everything(self.students[0])
        few, _response = self.grade_queries()
        for i in range(2, 6):
            self.answer_everything(self.add_student(i))
        self.answer_everything(self.students[1])
        many, response = self.grade_queries()
        # 2 -> 24 submissions. Before the fix every row cost a source, a contest-problem and an
        # MCQ-option / blank query (~3 per submission).
        self.assertEqual(few, many)
        rows = response.context['grade_submissions_json']
        self.assertEqual(len(rows), 24)
        mcq_rows = [row for row in rows if row['problem_type'] == 'M']
        self.assertTrue(all(row['answer_display'][0]['value'] == 'B. o1' for row in mcq_rows))
        blank_rows = [row for row in rows if row['problem_type'] == 'B']
        self.assertTrue(all(row['answer_display'][0]['value'] == 'for' for row in blank_rows))
        self.assertTrue(all(row['total'] == 5 for row in rows))


class StudentPageQueryCountTestCase(ExamLoadBase):
    """Workspace and assignment pages list every question: one name query, not one per question."""

    def test_workspace_question_names_are_one_query(self):
        student = self.students[0]
        self.join(student)
        client = self.client_for(student)
        url = reverse('problem_detail', args=[self.problems[0].code])
        client.get(url)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(url)
        self.assertEqual(response.status_code, 200)
        before = count_matching(ctx, 'judge_problemtranslation')
        for i in range(4, 10):
            self.add_question(i)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(count_matching(ctx, 'judge_problemtranslation'), before)
        # The question list, the current question's translation and the next/previous links.
        self.assertLessEqual(before, 4)
        self.assertContains(response, 'Question 9')

    def test_assignment_page_cost_is_independent_of_question_count(self):
        student = self.students[0]
        self.join(student)
        client = self.client_for(student)
        url = reverse('contest_view', args=[self.contest.key])
        client.get(url)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(client.get(url).status_code, 200)
        few = len(ctx)
        for i in range(4, 10):
            self.add_question(i)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Question 9')
        # Before: one translated-name query and one scoreboard-permission query per question.
        self.assertEqual(len(ctx), few)


class AccessCheckOncePerRequestTestCase(ExamLoadBase):
    """The contest middleware and the draft view ask the same course-role questions."""

    def test_draft_save_checks_course_roles_once(self):
        student = self.students[0]
        participation = self.join(student)
        client = self.client_for(student)
        url = reverse('assignment_draft', args=[self.contest.key, self.problems[2].code])
        with CaptureQueriesContext(connection) as ctx:
            response = client.post(url, {'content': json.dumps({'text': 'draft'})})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(participation.drafts.filter(problem=self.problems[2]).exists())
        self.assertEqual(count_matching(ctx, 'judge_organization_instructors'), 1)
        self.assertEqual(count_matching(ctx, 'judge_class_admins'), 1)

    def test_memo_is_per_user(self):
        contest = type(self.contest).objects.get(pk=self.contest.pk)
        self.assertTrue(contest.is_viewable_by_course_teacher(self.teacher))
        self.assertFalse(contest.is_viewable_by_course_teacher(self.students[0]))
        self.assertTrue(contest.is_viewable_by_org_admin(self.teacher))
        self.assertFalse(contest.is_viewable_by_org_admin(self.students[0]))
        with self.assertNumQueries(0):
            contest.is_viewable_by_course_teacher(self.teacher)
            contest.is_viewable_by_org_admin(self.students[0])

    def test_fresh_instance_sees_role_changes(self):
        student = self.students[1]
        self.assertFalse(type(self.contest).objects.get(pk=self.contest.pk).is_viewable_by_course_teacher(student))
        self.course.instructors.add(student.profile)
        self.assertTrue(type(self.contest).objects.get(pk=self.contest.pk).is_viewable_by_course_teacher(student))


class AnalyticsCacheTestCase(ExamLoadBase):
    """Analytics are recomputed at most every CLASSROOM_ANALYTICS_CACHE_SECONDS."""

    def setUp(self):
        cache.delete('contest_analytics:%d' % self.contest.id)
        self.addCleanup(cache.delete, 'contest_analytics:%d' % self.contest.id)
        self.url = reverse('contest_analytics', args=[self.contest.key])

    def test_reloads_reuse_the_computation(self):
        self.answer_everything(self.students[0])
        client = self.client_for(self.teacher)
        with mock.patch('judge.views.contest_analytics.get_contest_analytics',
                        wraps=get_contest_analytics) as compute:
            self.assertEqual(client.get(self.url).status_code, 200)
            response = client.get(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(compute.call_count, 1)
            self.assertEqual(response.context['analytics_summary']['total_submissions'], 4)
            self.assertContains(response, '?refresh=1')
            # "Recompute now" sees new work at once.
            self.answer_everything(self.students[1])
            response = client.get(self.url + '?refresh=1')
            self.assertEqual(compute.call_count, 2)
            self.assertEqual(response.context['analytics_summary']['total_submissions'], 8)

    def test_cached_analytics_are_still_editor_only(self):
        self.answer_everything(self.students[0])
        self.assertEqual(self.client_for(self.teacher).get(self.url).status_code, 200)
        # ContestMixin answers a non-editor with its "No such contest" page.
        response = self.client_for(self.students[0]).get(self.url)
        self.assertContains(response, 'No such contest')
        self.assertNotContains(response, 'Contest Summary')

    def test_zero_disables_the_cache(self):
        client = self.client_for(self.teacher)
        with self.settings(CLASSROOM_ANALYTICS_CACHE_SECONDS=0), \
                mock.patch('judge.views.contest_analytics.get_contest_analytics',
                           wraps=get_contest_analytics) as compute:
            client.get(self.url)
            client.get(self.url)
        self.assertEqual(compute.call_count, 2)


class ReservedTypeLabelsCacheTestCase(TestCase):
    def setUp(self):
        Problem._reserved_auto_type_labels_cache.clear()
        self.addCleanup(Problem._reserved_auto_type_labels_cache.clear)

    def test_computed_once_per_language(self):
        first = Problem.reserved_auto_type_labels()
        self.assertEqual(first, Problem._compute_reserved_auto_type_labels())
        self.assertIn('short answer', first)
        with mock.patch.object(Problem, '_compute_reserved_auto_type_labels') as compute:
            self.assertEqual(Problem.reserved_auto_type_labels(), first)
        compute.assert_not_called()

    def test_language_and_settings_are_part_of_the_key(self):
        Problem.reserved_auto_type_labels()
        with translation.override('de'):
            with mock.patch.object(Problem, '_compute_reserved_auto_type_labels', return_value=frozenset()) as c:
                Problem.reserved_auto_type_labels()
        c.assert_called_once()
        with self.settings(LANGUAGES=[('en', 'English')]):
            with mock.patch.object(Problem, '_compute_reserved_auto_type_labels', return_value=frozenset()) as c:
                Problem.reserved_auto_type_labels()
        c.assert_called_once()


class StudentAccessFastPathTestCase(ExamLoadBase):
    """``Contest.access_check`` grants class members first; the answers must not change."""

    def fresh(self, **changes):
        contest = type(self.contest).objects.get(pk=self.contest.pk)
        for field, value in changes.items():
            setattr(contest, field, value)
        return contest

    def test_class_member_is_checked_in_few_queries(self):
        student = type(self.students[0]).objects.get(pk=self.students[0].pk)
        # As in a request: permissions and the profile are already loaded on the user object.
        student.has_perm('judge.see_private_contest')
        student.profile
        contest = self.fresh()
        # Only the class membership query; before: editors, testers, spectators, course roles...
        with self.assertNumQueries(1):
            self.assertTrue(contest.is_accessible_by(student))

    def test_answers_are_unchanged(self):
        member = self.students[0]
        outsider = create_user(username='lp_outsider')
        self.course.members.add(outsider.profile)  # in the course but not in the class
        self.assertTrue(self.fresh().is_accessible_by(member))
        self.assertFalse(self.fresh().is_accessible_by(outsider))
        self.assertTrue(self.fresh().is_accessible_by(self.teacher))
        # Hidden or invite-only assignments are not opened by class membership alone.
        self.assertFalse(self.fresh(is_visible=False).is_accessible_by(member))
        self.assertFalse(self.fresh(is_private=True).is_accessible_by(member))
        private = self.fresh(is_private=True)
        private.save()
        private.private_contestants.add(member.profile)
        self.assertTrue(self.fresh().is_accessible_by(member))
