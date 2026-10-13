from django.test import TestCase
from django.utils import timezone

from judge.models import ContestSubmission, Language, Submission
from judge.models.tests.util import (
    create_contest,
    create_contest_participation,
    create_contest_problem,
    create_problem,
    create_user,
)
from judge.utils.problems import participation_submit_progress


class ParticipationSubmitProgressTestCase(TestCase):
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user(username='leave-progress-user')
        cls.language = Language.objects.get(key='PY3')
        cls.contest = create_contest(
            key='leaveprog',
            start_time=timezone.now() - timezone.timedelta(hours=1),
            end_time=timezone.now() + timezone.timedelta(hours=2),
            is_visible=True,
        )
        cls.problem_a = create_problem(code='leave-a', is_public=True)
        cls.problem_b = create_problem(code='leave-b', is_public=True)
        cls.cp_a = create_contest_problem(contest=cls.contest, problem=cls.problem_a, points=100, order=1)
        cls.cp_b = create_contest_problem(contest=cls.contest, problem=cls.problem_b, points=100, order=2)
        cls.participation = create_contest_participation(contest=cls.contest, user=cls.user.profile)

    def test_none_participation_is_empty(self):
        self.assertEqual(
            participation_submit_progress(None),
            {
                'total': 0, 'submitted': 0, 'unsubmitted': 0,
                'submitted_names': [], 'unsubmitted_names': [],
            },
        )

    def test_counts_submitted_and_remaining_problems(self):
        self.assertEqual(
            participation_submit_progress(self.participation),
            {
                'total': 2, 'submitted': 0, 'unsubmitted': 2,
                'submitted_names': [],
                'unsubmitted_names': ['1. leave-a', '2. leave-b'],
            },
        )

        submission = Submission.objects.create(
            user=self.user.profile, problem=self.problem_a, language=self.language,
            result='WA', status='D', points=0, contest_object=self.contest,
            time=0, memory=0,
        )
        ContestSubmission.objects.create(
            submission=submission, problem=self.cp_a, participation=self.participation,
        )
        self.assertEqual(
            participation_submit_progress(self.participation),
            {
                'total': 2, 'submitted': 1, 'unsubmitted': 1,
                'submitted_names': ['1. leave-a'],
                'unsubmitted_names': ['2. leave-b'],
            },
        )
