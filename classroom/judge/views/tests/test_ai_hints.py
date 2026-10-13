"""Tests for the AI-hint endpoint (judge.views.submission.SubmissionAIHint)."""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge import ai_hints, llm
from judge.models import Language, Problem, Submission, SubmissionSource
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

    def test_hints_enabled_for_or_logic(self):
        problem = create_problem(code='hintlogic', ai_hints_enabled=False)
        contest = create_contest(key='hintlogicc', allow_ai_hints=True)
        self.assertFalse(ai_hints.hints_enabled_for(problem, None))
        self.assertTrue(ai_hints.hints_enabled_for(problem, contest))
        contest.allow_ai_hints = False
        self.assertFalse(ai_hints.hints_enabled_for(problem, contest))
        problem.ai_hints_enabled = True
        self.assertTrue(ai_hints.hints_enabled_for(problem, contest))

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
