from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import ContestSubmission, Language, ProblemMCQConfig, Submission
from judge.models.tests.util import (
    CommonDataMixin,
    create_contest,
    create_contest_participation,
    create_contest_problem,
    create_problem,
    create_user,
)


class ProblemListFormatFilterTest(CommonDataMixin, TestCase):
    """The redesigned problem list filters by answer format via Problem.submission_type."""

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.code_problem = create_problem(
            code='codeprob', name='Code Problem', is_public=True, submission_type='C')
        self.mcq_problem = create_problem(
            code='mcqprob', name='Quiz Problem', is_public=True, submission_type='M')

    def _content(self, query=''):
        self.client.force_login(self.users['normal'])
        return self.client.get(reverse('problem_list') + query).content

    def test_all_formats_shown_by_default(self):
        content = self._content()
        self.assertIn(b'codeprob', content)
        self.assertIn(b'mcqprob', content)

    def test_filter_by_mcq_format(self):
        content = self._content('?format=M')
        self.assertIn(b'mcqprob', content)
        self.assertNotIn(b'codeprob', content)

    def test_filter_by_code_format(self):
        content = self._content('?format=C')
        self.assertIn(b'codeprob', content)
        self.assertNotIn(b'mcqprob', content)

    def test_filter_by_multiple_formats(self):
        content = self._content('?format=C&format=M')
        self.assertIn(b'codeprob', content)
        self.assertIn(b'mcqprob', content)

    def test_unknown_format_is_ignored(self):
        # 'Z' is not a registered question type, so the filter is dropped (show everything).
        content = self._content('?format=Z')
        self.assertIn(b'codeprob', content)
        self.assertIn(b'mcqprob', content)


class ProblemListMCQSubtypeFilterTest(CommonDataMixin, TestCase):
    """MCQ subtypes share submission_type M; the list filter is one "Choice question" option."""

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.single = create_problem(
            code='mcq-single', name='Single MCQ', is_public=True, submission_type='M')
        self.multiple = create_problem(
            code='mcq-multi', name='Multi MCQ', is_public=True, submission_type='M')
        self.truefalse = create_problem(
            code='mcq-tf', name='TF MCQ', is_public=True, submission_type='M')
        # A legacy MCQ with no mcq_config row: must still match the choice-question filter.
        self.legacy = create_problem(
            code='mcq-legacy', name='Legacy MCQ', is_public=True, submission_type='M')
        ProblemMCQConfig.objects.create(
            problem=self.single, multiple_select=False, is_true_false=False)
        ProblemMCQConfig.objects.create(
            problem=self.multiple, multiple_select=True, is_true_false=False)
        ProblemMCQConfig.objects.create(
            problem=self.truefalse, multiple_select=False, is_true_false=True)

    def _content(self, query=''):
        self.client.force_login(self.users['normal'])
        return self.client.get(reverse('problem_list') + query).content

    def test_choice_question_filter_covers_all_mcq_subtypes(self):
        content = self._content('?format=M')
        for code in (b'mcq-single', b'mcq-multi', b'mcq-tf', b'mcq-legacy'):
            self.assertIn(code, content)

    def test_legacy_subtype_query_params_are_ignored(self):
        # single / multiple / truefalse are no longer filter codes; unknown values are dropped.
        content = self._content('?format=single&format=truefalse')
        for code in (b'mcq-single', b'mcq-multi', b'mcq-tf', b'mcq-legacy'):
            self.assertIn(code, content)


class AssignmentProblemListTestCase(TestCase):
    """Join-an-assignment problem list: title, columns, and assignment-scoped stats."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.student = create_user(username='assign-student')
        cls.classmate = create_user(username='assign-classmate')
        cls.outsider = create_user(username='assign-outsider')
        cls.virtual_user = create_user(username='assign-virtual')
        cls.language = Language.objects.get(key='PY3')
        cls.problem = create_problem(
            code='assign-p1', name='Assignment P1', is_public=True, ac_rate=99.0, user_count=50,
        )
        cls.other = create_problem(
            code='assign-p2', name='Not In Assignment', is_public=True, ac_rate=10.0,
        )
        cls.contest = create_contest(
            key='assignlist', name='Homework Live Stats',
            start_time=timezone.now() - timezone.timedelta(hours=1),
            end_time=timezone.now() + timezone.timedelta(hours=2),
            is_visible=True,
        )
        cls.contest_problem = create_contest_problem(
            contest=cls.contest, problem=cls.problem, points=80, order=1,
        )

        student_part = create_contest_participation(contest=cls.contest, user=cls.student.profile)
        classmate_part = create_contest_participation(contest=cls.contest, user=cls.classmate.profile)
        cls.student.profile.current_contest = student_part
        cls.student.profile.save()

        ac = Submission.objects.create(
            user=cls.student.profile, problem=cls.problem, language=cls.language,
            result='AC', status='D', points=80, contest_object=cls.contest,
        )
        ContestSubmission.objects.create(
            submission=ac, problem=cls.contest_problem, participation=student_part, points=80,
        )
        wa = Submission.objects.create(
            user=cls.classmate.profile, problem=cls.problem, language=cls.language,
            result='WA', status='D', points=0, contest_object=cls.contest,
        )
        ContestSubmission.objects.create(
            submission=wa, problem=cls.contest_problem, participation=classmate_part, points=0,
        )
        # Practice / global attempt must not affect assignment Users or AC rate.
        Submission.objects.create(
            user=cls.outsider.profile, problem=cls.problem, language=cls.language,
            result='AC', status='D', points=100, contest_object=None,
        )
        virtual_part = create_contest_participation(
            contest=cls.contest, user=cls.virtual_user.profile, virtual=1,
        )
        virtual_ac = Submission.objects.create(
            user=cls.virtual_user.profile, problem=cls.problem, language=cls.language,
            result='AC', status='D', points=100, contest_object=cls.contest,
        )
        ContestSubmission.objects.create(
            submission=virtual_ac, problem=cls.contest_problem, participation=virtual_part, points=100,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.student)

    def test_heading_uses_assignment_name_before_problems(self):
        response = self.client.get(reverse('problem_list'))
        self.assertContains(response, 'Homework Live Stats')
        self.assertContains(response, 'Problems')
        self.assertContains(response, reverse('contest_view', args=[self.contest.key]))
        self.assertEqual(response.context['content_title'], 'Homework Live Stats')
        self.assertEqual(response.context['title_crumb'], 'Problems')

    def test_columns_are_score_problem_points_users_ac_rate(self):
        response = self.client.get(reverse('problem_list'))
        self.assertContains(response, '>Score<')
        self.assertContains(response, '>Problem<')
        self.assertContains(response, '>Points<')
        self.assertContains(response, '>Users<')
        self.assertContains(response, '>AC Rate<')
        self.assertNotContains(response, '>Category<')
        self.assertContains(response, 'Assignment P1')
        self.assertNotContains(response, 'Not In Assignment')
        self.assertContains(response, '80')

    def test_users_and_ac_rate_use_live_assignment_submissions(self):
        response = self.client.get(reverse('problem_list'))
        problems = list(response.context['problems'])
        self.assertEqual(len(problems), 1)
        row = problems[0]
        self.assertEqual(row['code'], 'assign-p1')
        # Two live participants submitted; practice and virtual AC are ignored.
        self.assertEqual(row['user_count'], 2)
        # 1 AC + 1 WA in this assignment → 50%, not the stored global 99%.
        self.assertEqual(row['ac_rate'], 50.0)
        self.assertContains(response, '50.0%')
        self.assertNotContains(response, '99.0%')
        ranked = reverse('contest_ranked_submissions', args=[self.contest.key, self.problem.code])
        self.assertContains(response, ranked)
        self.assertNotContains(response, reverse('ranked_submissions', args=[self.problem.code]))

    def test_list_row_shows_leave_button(self):
        response = self.client.get(reverse('problem_list'))
        leave = reverse('contest_leave', args=[self.contest.key])
        self.assertContains(response, 'fa-list')
        self.assertContains(response, leave)
        self.assertContains(response, 'assignment-leave-form')
        self.assertContains(response, 'Leave')
        self.assertContains(response, 'data-total=')
        self.assertContains(
            response,
            'You must press Submit on every problem. Unsubmitted answers will not count toward your grade.',
        )
        self.assertNotContains(response, 'Only answers you have already submitted will be kept.')
        self.assertNotContains(
            response,
            'When you leave, answers you have not submitted yet will be submitted automatically.',
        )
        self.assertNotContains(response, 'Submit &amp; Exit SEB')
