"""Exam surface lock in ContestMiddleware: what a live participant may open during an exam."""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import ContestParticipation, Judge, Language, Submission, SubmissionSource
from judge.models.tests.util import create_contest, create_contest_problem, create_problem, create_user


@override_settings(COMPRESS_ENABLED=False)
class ExamSurfaceLockTestCase(TestCase):
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.student = create_user(username='lockstudent')
        cls.editor = create_user(username='lockeditor')
        cls.problem = create_problem(code='lockprob', is_public=True)
        cls.other_problem = create_problem(code='lockother', is_public=True)
        create_problem(code='idepractice')
        cls.exam = create_contest(
            key='lockexam', name='Locked exam', format_name='exam', is_visible=True,
            start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=2),
            authors=('lockeditor',),
        )
        create_contest_problem(contest=cls.exam, problem=cls.problem)
        cls.homework = create_contest(
            key='lockhw', name='Homework', format_name='default', is_visible=True,
            start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=2),
        )
        create_contest_problem(contest=cls.homework, problem=cls.problem)
        cls.other_contest = create_contest(key='lockelse', is_visible=True)
        py3 = Language.objects.get(key='PY3')
        judge = Judge.objects.create(name='lockjudge', auth_key='lockkey', online=True)
        judge.runtimes.add(py3)
        judge.problems.add(cls.problem)
        cls.problem.allowed_languages.add(py3)
        cls.in_exam = Submission.objects.create(
            user=cls.student.profile, problem=cls.problem, language=py3, status='D', result='WA',
            contest_object=cls.exam, time=0.1, memory=100,
        )
        SubmissionSource.objects.create(submission=cls.in_exam, source='print(1)')
        cls.practice = Submission.objects.create(
            user=cls.student.profile, problem=cls.problem, language=py3, status='D', result='WA',
            time=0.1, memory=100,
        )
        SubmissionSource.objects.create(submission=cls.practice, source='print(2)')

    def setUp(self):
        cache.clear()
        self.client = Client()

    def _enter(self, contest, user=None, virtual=ContestParticipation.LIVE):
        user = user or self.student
        participation = ContestParticipation.objects.create(contest=contest, user=user.profile, virtual=virtual)
        user.profile.current_contest = participation
        user.profile.save()
        self.client.force_login(user)
        return participation

    def assertBlocked(self, response):
        self.assertIn(response.status_code, (302, 403), getattr(response, 'url', response.content[:200]))
        if response.status_code == 302:
            self.assertEqual(response.url, reverse('contest_view', args=('lockexam',)))

    def assertAllowed(self, response):
        if response.status_code == 302:
            self.assertNotEqual(response.url, reverse('contest_view', args=('lockexam',)))
        else:
            self.assertNotEqual(response.status_code, 403, response.content[:300])

    def test_blocked_pages(self):
        self._enter(self.exam)
        for label, url in (
            ('home / blog', reverse('home')),
            ('ide page', reverse('ide')),
            ('other problem', reverse('problem_detail', args=['lockother'])),
            ('problem list', reverse('problem_list')),
            ('tickets', reverse('problem_ticket_list', args=['lockprob'])),
            ('editorial', reverse('problem_editorial', args=['lockprob'])),
            ('problem submissions', reverse('chronological_submissions', args=['lockprob'])),
            ('user page', reverse('user_page', args=['lockeditor'])),
            ('all submissions', reverse('all_submissions')),
            ('other contest', reverse('contest_view', args=['lockelse'])),
            ('practice submission', reverse('submission_status', args=[self.practice.id])),
            ('practice submission widget', reverse('submission_testcases_query') + '?id=%d' % self.practice.id),
            ('api', '/api/v2/problems'),
        ):
            with self.subTest(label):
                self.assertBlocked(self.client.get(url))

    def test_blocked_posts(self):
        self._enter(self.exam)
        # Comments are posted to the problem page; blocked during the exam.
        self.assertEqual(self.client.post(reverse('problem_detail', args=['lockprob']),
                                          {'body': 'hi', 'parent': ''}).status_code, 403)
        self.assertEqual(self.client.post(reverse('ide_submit'), {'source': 'print(1)'}).status_code, 403)
        self.assertEqual(self.client.post(reverse('new_problem_ticket', args=['lockprob']),
                                          {'title': 'x', 'body': 'y'}).status_code, 403)
        # AI hints are off in this exam.
        self.assertEqual(self.client.post(reverse('submission_ai_hint', args=[self.in_exam.id])).status_code, 403)

    def test_allowed_pages(self):
        self._enter(self.exam)
        for label, url in (
            ('contest page', reverse('contest_view', args=['lockexam'])),
            ('contest problem', reverse('problem_detail', args=['lockprob'])),
            ('workspace submissions', reverse('problem_workspace_submissions', args=['lockprob']) + '?scope=my'),
            ('workspace submission', reverse('problem_workspace_submission_status',
                                             args=['lockprob', self.in_exam.id])),
            ('own exam submission', reverse('submission_status', args=[self.in_exam.id])),
            ('own exam submission widget', reverse('submission_testcases_query') + '?id=%d' % self.in_exam.id),
            ('own submissions in contest', reverse('contest_user_submissions',
                                                   args=['lockexam', 'lockstudent', 'lockprob'])),
            ('seb quit', reverse('seb_quit')),
        ):
            with self.subTest(label):
                self.assertAllowed(self.client.get(url))
        self.assertAllowed(self.client.get(reverse('auth_logout')))

    def test_workspace_run_and_its_status_allowed(self):
        self._enter(self.exam)
        with mock.patch.object(Submission, 'judge'):
            response = self.client.post(reverse('problem_workspace_run', args=['lockprob']), {
                'source': 'print(1)', 'language': Language.objects.get(key='PY3').id, 'input': '',
            })
        self.assertEqual(response.status_code, 200, response.content)
        status = self.client.get(response.json()['submission_url'])
        self.assertEqual(status.status_code, 200)

    def test_ai_hint_route_allowed_when_contest_allows_hints(self):
        self.exam.allow_ai_hints = True
        self.exam.save(update_fields=['allow_ai_hints'])
        self._enter(self.exam)
        response = self.client.post(reverse('submission_ai_hint', args=[self.in_exam.id]))
        # Past the lock; the view itself then decides (no LLM configured here).
        self.assertNotEqual(response.json().get('error'), 'This page is not available during the exam.')

    def test_not_applied_to_ordinary_homework(self):
        self._enter(self.homework)
        self.assertEqual(self.client.get(reverse('ide')).status_code, 200)

    def test_flag_turns_lock_on_for_homework_and_off_for_exam(self):
        self.homework.lock_site_during_participation = True
        self.homework.save(update_fields=['lock_site_during_participation'])
        self._enter(self.homework)
        response = self.client.get(reverse('ide'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('contest_view', args=('lockhw',)))

        self.exam.lock_site_during_participation = False
        self.exam.save(update_fields=['lock_site_during_participation'])
        self.client = Client()
        other = create_user(username='lockother_student')
        self._enter(self.exam, user=other)
        self.assertEqual(self.client.get(reverse('ide')).status_code, 200)

    def test_seb_required_contest_locks_by_default(self):
        self.assertFalse(self.homework.locks_site_during_participation)
        self.homework.seb_required = True
        self.assertTrue(self.homework.locks_site_during_participation)
        self.assertTrue(self.exam.locks_site_during_participation)

    def test_editor_not_locked(self):
        self._enter(self.exam, user=self.editor)
        self.assertEqual(self.client.get(reverse('ide')).status_code, 200)

    def test_spectator_not_locked(self):
        self._enter(self.exam, virtual=ContestParticipation.SPECTATE)
        self.assertEqual(self.client.get(reverse('ide')).status_code, 200)

    def test_json_clients_get_403_not_redirect(self):
        self._enter(self.exam)
        response = self.client.get(reverse('problem_list'), HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 403)
