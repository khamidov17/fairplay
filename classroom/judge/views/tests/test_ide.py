"""Tests for the in-browser IDE submit/status endpoints (judge.views.ide)."""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import ContestParticipation, Judge, Language, Submission, SubmissionTestCase
from judge.models.tests.util import create_contest, create_contest_participation, create_problem, create_user
from judge.utils.sandbox_output import should_hide_sandbox_output, submission_output_hidden_for


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


class IDESandboxOutputEveryPathTestCase(TestCase):
    """AI-2: "Hide IDE / Run Test output" holds on every path, and is recorded on the run."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.student = create_user(username='idepathstudent')
        cls.author = create_user(username='idepathauthor')
        cls.language = Language.objects.get(key='PY3')
        judge = Judge.objects.create(name='idepathjudge', auth_key='idepathkey', online=True)
        judge.runtimes.add(cls.language)
        cls.ide_problem = create_problem(code='idepractice')
        cls.contest = create_contest(
            key='idepathasn', name='Hide sandbox everywhere',
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now() + timedelta(hours=2),
            is_visible=True,
            hide_sandbox_output=True,
            authors=('idepathauthor',),
        )

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.client.force_login(self.student)

    def _join(self):
        participation = create_contest_participation(contest=self.contest, user=self.student.profile)
        self.student.profile.current_contest = participation
        self.student.profile.save()
        return participation

    def _leave(self):
        self.student.profile.current_contest = None
        self.student.profile.save()

    def _run_and_grade(self, output='secret-stdout-777'):
        with mock.patch.object(Submission, 'judge'):
            response = self.client.post(reverse('ide_submit'), {
                'source': 'print(1)', 'language': self.language.id, 'input': '',
            })
        self.assertEqual(response.status_code, 200, response.content)
        submission = Submission.objects.get(id=response.json()['submission_id'])
        Submission.objects.filter(id=submission.id).update(status='D', result='AC', time=0.01, memory=10)
        SubmissionTestCase.objects.create(
            submission=submission, case=1, status='AC', time=0.01, memory=10, output=output,
        )
        submission.refresh_from_db()
        return submission

    def _pages(self, submission):
        return {
            'ide_status': self.client.get(reverse('ide_submission_status', args=[submission.id])),
            'submission_status': self.client.get(reverse('submission_status', args=[submission.id])),
            'testcases_widget': self.client.get(reverse('submission_testcases_query'), {'id': submission.id}),
        }

    def test_run_records_the_hiding_contest(self):
        self._join()
        submission = self._run_and_grade()
        self.assertEqual(submission.sandbox_hidden_contest_id, self.contest.id)

    def test_run_outside_any_contest_records_nothing(self):
        submission = self._run_and_grade()
        self.assertIsNone(submission.sandbox_hidden_contest_id)
        pages = self._pages(submission)
        self.assertIn('secret-stdout-777', pages['ide_status'].json()['output'])
        self.assertContains(pages['submission_status'], 'secret-stdout-777')

    def test_output_hidden_on_submission_page_and_widget(self):
        self._join()
        submission = self._run_and_grade()
        pages = self._pages(submission)
        self.assertNotIn('output', pages['ide_status'].json())
        self.assertEqual(pages['submission_status'].status_code, 200)
        self.assertNotContains(pages['submission_status'], 'secret-stdout-777')
        self.assertEqual(pages['testcases_widget'].status_code, 200)
        self.assertNotContains(pages['testcases_widget'], 'secret-stdout-777')

    def test_leaving_the_contest_does_not_reveal_output(self):
        self._join()
        submission = self._run_and_grade()
        self._leave()
        pages = self._pages(submission)
        self.assertNotIn('output', pages['ide_status'].json())
        self.assertTrue(pages['ide_status'].json()['output_hidden'])
        self.assertNotContains(pages['submission_status'], 'secret-stdout-777')
        self.assertNotContains(pages['testcases_widget'], 'secret-stdout-777')

    def test_run_made_after_leaving_a_running_contest_is_hidden(self):
        # Leave, Run, rejoin must not be a way around the switch.
        self._join()
        self._leave()
        submission = self._run_and_grade()
        self.assertEqual(submission.sandbox_hidden_contest_id, self.contest.id)
        self.assertNotIn('output', self._pages(submission)['ide_status'].json())

    def test_output_shown_after_the_contest_ends(self):
        self._join()
        submission = self._run_and_grade()
        self._leave()
        self.contest.end_time = timezone.now() - timedelta(minutes=1)
        self.contest.save(update_fields=['end_time'])
        pages = self._pages(submission)
        self.assertIn('secret-stdout-777', pages['ide_status'].json()['output'])
        self.assertContains(pages['submission_status'], 'secret-stdout-777')

    def test_assignment_staff_are_not_hidden_from(self):
        self._join()
        submission = self._run_and_grade()
        self.assertTrue(submission_output_hidden_for(submission, self.student))
        self.assertFalse(submission_output_hidden_for(submission, self.author))


class IDERateLimitAtomicTestCase(TestCase):
    """AI-8: the IDE limit is reserved with add/incr, so a stale read cannot pass it."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user(username='iderateuser')
        cls.language = Language.objects.get(key='PY3')
        judge = Judge.objects.create(name='idereatejudge', auth_key='idereatekey', online=True)
        judge.runtimes.add(cls.language)
        create_problem(code='idepractice')

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.client.force_login(self.user)
        self.key = f'ide_run:{self.user.profile.id}'

    def _submit(self):
        with mock.patch.object(Submission, 'judge'):
            return self.client.post(reverse('ide_submit'), {
                'source': 'print(1)', 'language': self.language.id, 'input': '',
            })

    def test_stale_counter_read_cannot_pass_the_limit(self):
        # Five runs already counted, but this request read the counter before they
        # landed (a parallel burst): the reservation itself must refuse it.
        cache.set(self.key, 5, 60)
        real_get = cache.get

        def stale_get(key, *args, **kwargs):
            if key == self.key:
                return 0
            return real_get(key, *args, **kwargs)

        with mock.patch('judge.views.ide.cache.get', side_effect=stale_get):
            response = self._submit()
        self.assertEqual(response.status_code, 429)
        self.assertFalse(Submission.objects.filter(user=self.user.profile).exists())

    def test_counter_expiring_mid_request_does_not_500(self):
        # The fast-path read saw a live counter, which then expired before the
        # increment: this used to raise ValueError from cache.incr after queuing.
        real_get = cache.get

        def expired_get(key, *args, **kwargs):
            if key == self.key:
                return 1
            return real_get(key, *args, **kwargs)

        with mock.patch('judge.views.ide.cache.get', side_effect=expired_get):
            response = self._submit()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(cache.get(self.key), 1)

    def test_five_runs_then_limited(self):
        for _ in range(5):
            self.assertEqual(self._submit().status_code, 200)
            # Not queued any more, so DMOJ_SUBMISSION_LIMIT does not interfere.
            Submission.objects.filter(user=self.user.profile).update(status='D')
        self.assertEqual(self._submit().status_code, 429)
        self.assertEqual(Submission.objects.filter(user=self.user.profile).count(), 5)
