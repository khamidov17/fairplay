"""Tests for the AI-hint endpoint (judge.views.submission.SubmissionAIHint)."""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge import ai_hints, llm
from judge.models import (
    Contest, ContestParticipation, ContestSubmission, Language, Problem, Submission, SubmissionSource,
)
from judge.models.ai_hints import AIHintLog
from judge.models.tests.util import create_contest, create_contest_problem, create_problem, create_user

AZURE_SETTINGS = {
    'LLM_PROVIDER': 'azure',
    'AZURE_OPENAI_ENDPOINT': 'https://example.openai.azure.com/',
    'AZURE_OPENAI_API_KEY': 'test-key',
    'AZURE_OPENAI_DEPLOYMENT': 'gpt-test',
}


class AIHintViewTestCase(TestCase):
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user(username='hintowner')
        cls.other = create_user(username='hintother')
        cls.problem = create_problem(code='hintprob', ai_hints_enabled=True)
        cls.submission = Submission.objects.create(
            user=cls.user.profile, problem=cls.problem,
            language=Language.objects.get(key='PY3'), result='WA', status='D')
        SubmissionSource.objects.create(submission=cls.submission, source='print(1)')

    def setUp(self):
        llm._backend = None
        self.addCleanup(setattr, llm, '_backend', None)
        cache.clear()
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse('submission_ai_hint', args=[self.submission.id])

    def test_requires_login(self):
        response = Client().post(self.url)
        self.assertEqual(response.status_code, 302)

    @override_settings(**AZURE_SETTINGS)
    def test_disabled_problem_rejected(self):
        Problem.objects.filter(id=self.problem.id).update(ai_hints_enabled=False)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 403)

    def test_unconfigured_backend_rejected(self):
        # Explicitly simulate "no LLM configured" rather than relying on the host's settings —
        # a host with a configured-but-unreachable LLM would otherwise make a real call.
        with mock.patch.object(ai_hints, 'is_configured', return_value=False):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503)

    @override_settings(**AZURE_SETTINGS)
    def test_other_users_submission_rejected(self):
        self.client.force_login(self.other)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 403)

    @override_settings(**AZURE_SETTINGS)
    def test_quota_exhausted_rejected(self):
        with mock.patch.object(ai_hints, 'try_reserve', return_value=False):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 429)

    @override_settings(**AZURE_SETTINGS)
    def test_successful_hint_logged_and_counted(self):
        result = ai_hints.HintResult(hint_text='Check your loop bounds.', total_tokens=42)
        with mock.patch.object(ai_hints, 'get_hint', return_value=result):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['hint'], 'Check your loop bounds.')
        self.assertEqual(data['remaining'], 4)
        log = AIHintLog.objects.get(user=self.user.profile)
        self.assertTrue(log.success)
        self.assertEqual(log.hint_text, 'Check your loop bounds.')

    @override_settings(**AZURE_SETTINGS)
    def test_failed_hint_refunds_quota(self):
        with mock.patch.object(ai_hints, 'get_hint', side_effect=RuntimeError('API down')):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503)
        log = AIHintLog.objects.get(user=self.user.profile)
        self.assertFalse(log.success)
        self.assertIn('API down', log.error_message)
        self.assertEqual(ai_hints.get_remaining(self.user.profile.id), 5)

    @override_settings(**AZURE_SETTINGS)
    def test_failed_hint_does_not_leak_internal_error(self):
        """The student-facing message must be stable and friendly — never expose the internal
        exception text (endpoint names, exception types) that could aid probing."""
        with mock.patch.object(ai_hints, 'get_hint', side_effect=RuntimeError('APITimeoutError at https://10.0.0.1')):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503)
        body = response.json()['error']
        self.assertNotIn('APITimeoutError', body)
        self.assertNotIn('10.0.0.1', body)
        self.assertIn('Could not reach the AI service', body)


class AssignmentAIHintOverrideTestCase(TestCase):
    """Assignment-level Allow AI Hint ORs with the problem flag; it does not mutate it."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user(username='asghintowner')
        cls.problem = create_problem(code='asghintprob', ai_hints_enabled=False)
        cls.contest = create_contest(
            key='asghinthw', name='Hint Homework',
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now() + timedelta(days=1),
            allow_ai_hints=True,
            is_visible=True,
        )
        create_contest_problem(contest=cls.contest, problem=cls.problem)
        cls.submission = Submission.objects.create(
            user=cls.user.profile, problem=cls.problem,
            language=Language.objects.get(key='PY3'), result='WA', status='D',
            contest_object=cls.contest,
        )
        SubmissionSource.objects.create(submission=cls.submission, source='print(1)')
        cls.outside = Submission.objects.create(
            user=cls.user.profile, problem=cls.problem,
            language=Language.objects.get(key='PY3'), result='WA', status='D',
        )
        SubmissionSource.objects.create(submission=cls.outside, source='print(2)')

    def setUp(self):
        llm._backend = None
        self.addCleanup(setattr, llm, '_backend', None)
        cache.clear()
        self.client = Client()
        self.client.force_login(self.user)

    def test_hints_enabled_for_contest_decides(self):
        # AI-3: inside a contest the contest flag decides; outside, the problem flag.
        problem = create_problem(code='hintlogic', ai_hints_enabled=False)
        contest = create_contest(key='hintlogicc', allow_ai_hints=True)
        self.assertFalse(ai_hints.hints_enabled_for(problem, None))
        self.assertTrue(ai_hints.hints_enabled_for(problem, contest))
        contest.allow_ai_hints = False
        self.assertFalse(ai_hints.hints_enabled_for(problem, contest))
        problem.ai_hints_enabled = True
        self.assertTrue(ai_hints.hints_enabled_for(problem, None))
        # The contest can switch hints off even when the problem has them on.
        self.assertFalse(ai_hints.hints_enabled_for(problem, contest))

    @override_settings(**AZURE_SETTINGS)
    def test_assignment_override_allows_hint_when_problem_disabled(self):
        result = ai_hints.HintResult(hint_text='Check your loop bounds.')
        with mock.patch.object(ai_hints, 'get_hint', return_value=result):
            response = self.client.post(reverse('submission_ai_hint', args=[self.submission.id]))
        self.assertEqual(response.status_code, 200, response.content)
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.ai_hints_enabled)

    @override_settings(**AZURE_SETTINGS)
    def test_assignment_override_does_not_apply_outside_assignment(self):
        response = self.client.post(reverse('submission_ai_hint', args=[self.outside.id]))
        self.assertEqual(response.status_code, 403)


class ContestControlsAIHintsTestCase(TestCase):
    """AI-3: inside a contest the contest flag decides, even over the problem flag."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user(username='exhintowner')
        cls.problem = create_problem(code='exhintprob', is_public=True, ai_hints_enabled=True)
        cls.contest = create_contest(
            key='exhintexam', name='Hints off exam',
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now() + timedelta(days=1),
            allow_ai_hints=False,
            is_visible=True,
        )
        create_contest_problem(contest=cls.contest, problem=cls.problem)
        py3 = Language.objects.get(key='PY3')
        cls.in_contest = Submission.objects.create(
            user=cls.user.profile, problem=cls.problem, language=py3, result='WA', status='D',
            contest_object=cls.contest,
        )
        SubmissionSource.objects.create(submission=cls.in_contest, source='print(1)')
        cls.practice = Submission.objects.create(
            user=cls.user.profile, problem=cls.problem, language=py3, result='WA', status='D',
        )
        SubmissionSource.objects.create(submission=cls.practice, source='print(2)')

    def setUp(self):
        llm._backend = None
        self.addCleanup(setattr, llm, '_backend', None)
        cache.clear()
        self.client = Client()
        self.client.force_login(self.user)

    def _join(self):
        participation = ContestParticipation.objects.create(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()

    @override_settings(**AZURE_SETTINGS)
    def test_contest_off_refuses_hint_on_contest_submission(self):
        with mock.patch.object(ai_hints, 'get_hint') as get_hint:
            response = self.client.post(reverse('submission_ai_hint', args=[self.in_contest.id]))
        self.assertEqual(response.status_code, 403)
        get_hint.assert_not_called()

    @override_settings(**AZURE_SETTINGS)
    def test_contest_off_refuses_hint_on_practice_submission_while_in_contest(self):
        self._join()
        with mock.patch.object(ai_hints, 'get_hint') as get_hint:
            response = self.client.post(reverse('submission_ai_hint', args=[self.practice.id]))
        self.assertEqual(response.status_code, 403)
        get_hint.assert_not_called()

    @override_settings(**AZURE_SETTINGS)
    def test_practice_submission_outside_contest_uses_problem_flag(self):
        result = ai_hints.HintResult(hint_text='Look at the loop.')
        with mock.patch.object(ai_hints, 'get_hint', return_value=result):
            response = self.client.post(reverse('submission_ai_hint', args=[self.practice.id]))
        self.assertEqual(response.status_code, 200, response.content)

    def test_workspace_hides_hints_in_contest_with_hints_off(self):
        self._join()
        with mock.patch('judge.ai_hints.is_configured', return_value=True):
            response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertFalse(response.context['ai_hints_possible'])

    def test_status_page_hides_hints_for_contest_submission(self):
        with mock.patch('judge.ai_hints.is_configured', return_value=True):
            response = self.client.get(reverse('submission_status', args=[self.in_contest.id]))
        self.assertFalse(response.context['ai_hints_possible'])

    def test_create_v2_ai_hints_default_off(self):
        import os

        from django.conf import settings
        with open(os.path.join(settings.BASE_DIR, 'templates', 'problem', 'create_v2.html'), encoding='utf-8') as f:
            source = f.read()
        self.assertIn('id="f-ai-hints">', source)
        self.assertNotIn('id="f-ai-hints" checked', source)

    def test_quota_message_is_not_daily(self):
        # AI-8: the limit is a sliding window, not a daily allowance.
        message = ai_hints.quota_exhausted_message()
        self.assertNotIn('today', message)
        self.assertNotIn('tomorrow', message)
        self.assertIn('5-minute window', message)


class HiddenVerdictAIHintTestCase(TestCase):
    """AI-4: no hint (offered or answered) while the verdict is hidden from the user."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user(username='hidhintowner')
        cls.problem = create_problem(code='hidhintprob', is_public=True, ai_hints_enabled=False)
        cls.contest = create_contest(
            key='hidhintexam', name='Hidden results exam',
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now() + timedelta(days=1),
            allow_ai_hints=True,
            format_name='exam',
            is_visible=True,
        )
        cls.contest_problem = create_contest_problem(contest=cls.contest, problem=cls.problem, hide_results=True)
        cls.participation = ContestParticipation.objects.create(contest=cls.contest, user=cls.user.profile)
        cls.submission = Submission.objects.create(
            user=cls.user.profile, problem=cls.problem, language=Language.objects.get(key='PY3'),
            result='WA', status='D', contest_object=cls.contest,
        )
        SubmissionSource.objects.create(submission=cls.submission, source='print(1)')
        ContestSubmission.objects.create(
            submission=cls.submission, problem=cls.contest_problem, participation=cls.participation, points=0,
        )

    def setUp(self):
        llm._backend = None
        self.addCleanup(setattr, llm, '_backend', None)
        cache.clear()
        self.client = Client()
        self.client.force_login(self.user)
        self.user.profile.current_contest = self.participation
        self.user.profile.save()

    def test_verdict_is_hidden(self):
        self.assertFalse(self.submission.results_visible_to(self.user))

    @override_settings(**AZURE_SETTINGS)
    def test_hint_post_refused(self):
        with mock.patch.object(ai_hints, 'get_hint') as get_hint:
            response = self.client.post(reverse('submission_ai_hint', args=[self.submission.id]))
        self.assertEqual(response.status_code, 403)
        get_hint.assert_not_called()
        self.assertFalse(AIHintLog.objects.exists())

    def test_status_page_does_not_offer_hint(self):
        with mock.patch('judge.ai_hints.is_configured', return_value=True):
            response = self.client.get(reverse('submission_status', args=[self.submission.id]))
        self.assertFalse(response.context['ai_hints_possible'])
        self.assertFalse(response.context['ai_hints_enabled'])
        self.assertNotContains(response, 'ai-hint-section')

    def test_workspace_does_not_offer_hint(self):
        with mock.patch('judge.ai_hints.is_configured', return_value=True):
            response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertFalse(response.context['ai_hints_possible'])
        self.assertNotContains(response, 'data-ai-hints-possible')

    def test_hint_offered_once_results_are_finalized(self):
        Contest.objects.filter(id=self.contest.id).update(results_finalized=True)
        with mock.patch('judge.ai_hints.is_configured', return_value=True):
            response = self.client.get(reverse('submission_status', args=[self.submission.id]))
        self.assertTrue(response.context['ai_hints_possible'])
