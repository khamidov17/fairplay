"""Tests for the in-browser IDE submit/status endpoints (judge.views.ide)."""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import ContestParticipation, Judge, Language, Submission, SubmissionTestCase
from judge.models.tests.util import create_contest, create_contest_participation, create_problem, create_user
from judge.utils.sandbox_output import should_hide_sandbox_output


class IDEViewTestCase(TestCase):
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user(username='ideuser')
        cls.other = create_user(username='ideother')
        cls.language = Language.objects.get(key='PY3')
        judge = Judge.objects.create(name='testjudge', auth_key='testkey', online=True)
        judge.runtimes.add(cls.language)

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.client.force_login(self.user)

    def _submit(self, **overrides):
        data = {'source': 'print(input())', 'language': self.language.id, 'input': 'hello'}
        data.update(overrides)
        return self.client.post(reverse('ide_submit'), data)

    def test_page_uses_local_monaco(self):
        response = self.client.get(reverse('ide'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="code-editor"')
        self.assertContains(response, 'libs/monaco/vs')
        self.assertNotContains(response, 'cdn.jsdelivr.net')

    def test_requires_login(self):
        anon = Client()
        self.assertEqual(anon.get(reverse('ide')).status_code, 302)
        self.assertEqual(anon.post(reverse('ide_submit'), {}).status_code, 302)

    def test_rate_limit(self):
        cache.set(f'ide_run:{self.user.profile.id}', 5, 60)
        self.assertEqual(self._submit().status_code, 429)

    def test_source_size_validated(self):
        self.assertEqual(self._submit(source='').status_code, 400)
        self.assertEqual(self._submit(source='a' * 65537).status_code, 400)

    def test_invalid_language_rejected(self):
        self.assertEqual(self._submit(language=999999).status_code, 400)

    def test_missing_ide_problem_reported(self):
        self.assertEqual(self._submit().status_code, 500)

    def test_successful_submission_judged_with_cached_input(self):
        create_problem(code='idepractice')
        with mock.patch.object(Submission, 'judge') as mock_judge:
            response = self._submit()
        self.assertEqual(response.status_code, 200)
        submission_id = response.json()['submission_id']
        mock_judge.assert_called_once()
        self.assertEqual(cache.get(f'ide_input:{submission_id}'), 'hello')
        submission = Submission.objects.get(id=submission_id)
        self.assertEqual(submission.problem.code, 'idepractice')

    def test_status_denies_other_users(self):
        problem = create_problem(code='idepractice')
        submission = Submission.objects.create(
            user=self.other.profile, problem=problem, language=self.language)
        response = self.client.get(reverse('ide_submission_status', args=[submission.id]))
        self.assertEqual(response.status_code, 403)

    def test_page_loads_shared_run_result_script(self):
        response = self.client.get(reverse('ide'))
        self.assertContains(response, 'ide_run_result.js')
        self.assertContains(response, 'formatIdeRunResult')


class IDESandboxOutputHideTestCase(TestCase):
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.student = create_user(username='idestudent')
        cls.author = create_user(username='ideauthor')
        cls.language = Language.objects.get(key='PY3')
        judge = Judge.objects.create(name='idehidejudge', auth_key='idehidekey', online=True)
        judge.runtimes.add(cls.language)
        cls.ide_problem = create_problem(code='idepractice')
        cls.contest = create_contest(
            key='idehideasn', name='Hide sandbox',
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now() + timedelta(hours=2),
            is_visible=True,
            hide_sandbox_output=True,
            authors=('ideauthor',),
        )

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.contest.refresh_from_db()

    def _graded_run(self, user, *, result='AC', status='D', output='printed-42',
                    error='', feedback='', extended_feedback=''):
        submission = Submission.objects.create(
            user=user.profile, problem=self.ide_problem, language=self.language,
            result=result, status=status, time=0.012, memory=2048, error=error,
        )
        SubmissionTestCase.objects.create(
            submission=submission, case=1, status=result, time=0.012, memory=2048,
            output=output, feedback=feedback, extended_feedback=extended_feedback,
        )
        return submission

    def _join(self, user, contest=None, virtual=ContestParticipation.LIVE):
        participation = create_contest_participation(
            contest=contest or self.contest, user=user.profile, virtual=virtual,
        )
        user.profile.current_contest = participation
        user.profile.save()
        return participation

    def _status(self, user, submission):
        self.client.force_login(user)
        return self.client.get(reverse('ide_submission_status', args=[submission.id]))

    def test_not_joined_shows_stdout(self):
        submission = self._graded_run(self.student)
        self.assertFalse(should_hide_sandbox_output(self.student))
        body = self._status(self.student, submission).json()
        self.assertIn('printed-42', body['output'])
        self.assertNotIn('output_hidden', body)

    def test_switch_off_shows_stdout(self):
        self.contest.hide_sandbox_output = False
        self.contest.save(update_fields=['hide_sandbox_output'])
        self._join(self.student)
        self.student.profile.refresh_from_db()
        self.assertFalse(should_hide_sandbox_output(self.student))
        submission = self._graded_run(self.student)
        body = self._status(self.student, submission).json()
        self.assertIn('printed-42', body['output'])
        self.assertNotIn('output_hidden', body)

    def test_student_in_restricted_assignment_omits_stdout(self):
        self._join(self.student)
        self.student.profile.refresh_from_db()
        self.assertTrue(should_hide_sandbox_output(self.student))
        submission = self._graded_run(self.student)
        body = self._status(self.student, submission).json()
        self.assertNotIn('output', body)
        self.assertTrue(body['output_hidden'])
        self.assertEqual(body['result'], 'AC')
        self.assertEqual(body['status'], 'D')
        self.assertEqual(body['time'], 0.012)
        self.assertEqual(body['memory'], 2048)

    def test_author_bypass_sees_stdout(self):
        self._join(self.author)
        self.author.profile.refresh_from_db()
        self.assertFalse(should_hide_sandbox_output(self.author))
        submission = self._graded_run(self.author)
        body = self._status(self.author, submission).json()
        self.assertIn('printed-42', body['output'])
        self.assertNotIn('output_hidden', body)

    def test_compile_error_keeps_compiler_log(self):
        self._join(self.student)
        submission = self._graded_run(
            self.student, result='CE', status='CE', output='should-hide',
            error='SyntaxError: invalid syntax',
        )
        body = self._status(self.student, submission).json()
        self.assertNotIn('output', body)
        self.assertTrue(body['output_hidden'])
        self.assertIn('SyntaxError', body['error'])

    def test_runtime_error_keeps_traceback(self):
        self._join(self.student)
        submission = self._graded_run(
            self.student, result='IR', status='D', output='should-hide',
            feedback='ZeroDivisionError',
            extended_feedback='Traceback (most recent call last):\nZeroDivisionError',
        )
        body = self._status(self.student, submission).json()
        self.assertNotIn('output', body)
        self.assertTrue(body['output_hidden'])
        self.assertEqual(body['result'], 'IR')
        self.assertIn('ZeroDivisionError', body['feedback'])
        self.assertIn('Traceback', body['extended_feedback'])
