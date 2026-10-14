"""Fixes found while running code questions end to end against a real DMOJ judge
(docs/classroom/reports/agent-b-judge.md)."""
import io
import os
import socket
import tempfile
import threading
import zipfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from judge.bridge import daemon as bridge_daemon
from judge.bridge.server import Server, ThreadingTCPListener
from judge.models import (Judge, Language, Problem, ProblemData, ProblemTestCase, Submission, SubmissionSource,
                          problem_data_storage)
from judge.models.tests.util import CommonDataMixin, create_problem, create_user

CASES = [('1 2', '3'), ('-5 5', '0'), ('7 8', '15')]


def make_zip(cases=CASES, name='data.zip'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for i, (given, expected) in enumerate(cases, 1):
            z.writestr('%d.in' % i, given + '\n')
            z.writestr('%d.out' % i, expected + '\n')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='application/zip')


def data_form(rows=(), zip_file=None, initial=0):
    """The POST the test-data page sends. ``rows`` are (id, order, input, output, points) tuples;
    like the page, a blank numbered row follows them."""
    rows = list(rows) + [('', len(rows) + 1, '', '', '')]
    post = {
        'problem-data-checker': '', 'problem-data-checker_args': '', 'problem-data-output_limit': '',
        'problem-data-output_prefix': '',
        'cases-TOTAL_FORMS': str(len(rows)), 'cases-INITIAL_FORMS': str(initial),
        'cases-MIN_NUM_FORMS': '0', 'cases-MAX_NUM_FORMS': '1000',
        'harnesses-TOTAL_FORMS': '0', 'harnesses-INITIAL_FORMS': '0',
        'harnesses-MIN_NUM_FORMS': '0', 'harnesses-MAX_NUM_FORMS': '1',
    }
    for i, (pk, order, inp, out, points) in enumerate(rows):
        prefix = 'cases-%d-' % i
        post.update({prefix + 'id': pk, prefix + 'order': str(order), prefix + 'type': 'C',
                     prefix + 'input_file': inp, prefix + 'output_file': out, prefix + 'points': str(points),
                     prefix + 'output_prefix': '', prefix + 'output_limit': '', prefix + 'checker': '',
                     prefix + 'checker_args': '', prefix + 'generator_args': '', prefix + 'batch_dependencies': ''})
    if zip_file is not None:
        post['problem-data-zipfile'] = zip_file
    return post


class TestDataEditorTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher = create_user(username='jd_teacher', user_permissions=('edit_own_problem',))
        cls.problem = create_problem(code='jdsum', authors=('jd_teacher',), points=6)

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_location = problem_data_storage.location
        problem_data_storage.location = self._tmpdir.name
        self.client.force_login(self.teacher)
        self.url = reverse('problem_data', args=[self.problem.code])

    def tearDown(self):
        problem_data_storage.location = self._old_location
        self._tmpdir.cleanup()

    def _dir(self):
        return Path(self._tmpdir.name) / self.problem.code

    def test_uploading_only_a_zip_does_not_save_the_blank_row(self):
        # The page numbers its blank row (order=1); that alone used to save an empty case and
        # fail with "Points must be defined for non-batch case #1."
        response = self.client.post(self.url, data_form(zip_file=make_zip()))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ProblemTestCase.objects.filter(dataset=self.problem).exists())
        data = ProblemData.objects.get(problem=self.problem)
        self.assertEqual(data.feedback, '')
        self.assertTrue(data.zipfile)
        self.assertTrue((self._dir() / 'data.zip').exists())

    def test_zip_then_cases_generates_init_yml(self):
        self.client.post(self.url, data_form(zip_file=make_zip()))
        rows = [('', i, '%d.in' % i, '%d.out' % i, 2) for i in range(1, 4)]
        response = self.client.post(self.url, data_form(rows))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProblemData.objects.get(problem=self.problem).feedback, '')
        self.assertEqual(ProblemTestCase.objects.filter(dataset=self.problem).count(), 3)
        init = (self._dir() / 'init.yml').read_text()
        self.assertIn('archive: data.zip', init)
        self.assertEqual(init.count('points: 2'), 3)

    def test_uploading_a_new_zip_replaces_the_old_one(self):
        # Re-uploading test data used to leave the problem with no zip at all: deleting the old
        # file also cleared the field that held the new upload.
        self.client.post(self.url, data_form(zip_file=make_zip()))
        rows = [('', i, '%d.in' % i, '%d.out' % i, 2) for i in range(1, 4)]
        self.client.post(self.url, data_form(rows))
        saved = [(c.pk, c.order, c.input_file, c.output_file, c.points)
                 for c in ProblemTestCase.objects.filter(dataset=self.problem).order_by('order')]

        new_cases = [('2 2', '4'), ('3 3', '6'), ('4 4', '8')]
        response = self.client.post(self.url, data_form(saved, zip_file=make_zip(new_cases), initial=len(saved)))
        self.assertEqual(response.status_code, 302)
        data = ProblemData.objects.get(problem=self.problem)
        self.assertEqual(data.feedback, '')
        self.assertEqual(data.zipfile.name, '%s/data.zip' % self.problem.code)
        with zipfile.ZipFile(self._dir() / 'data.zip') as z:
            self.assertEqual(z.read('1.in'), b'2 2\n')
        self.assertIn('archive: data.zip', (self._dir() / 'init.yml').read_text())

    def test_new_zip_with_another_name_removes_the_old_file(self):
        self.client.post(self.url, data_form(zip_file=make_zip()))
        self.client.post(self.url, data_form(zip_file=make_zip(name='v2.zip')))
        data = ProblemData.objects.get(problem=self.problem)
        self.assertEqual(data.zipfile.name, '%s/v2.zip' % self.problem.code)
        self.assertFalse((self._dir() / 'data.zip').exists())
        self.assertTrue((self._dir() / 'v2.zip').exists())

    def test_clearing_the_zip_removes_it(self):
        self.client.post(self.url, data_form(zip_file=make_zip()))
        post = data_form()
        post['problem-data-zipfile-clear'] = 'on'
        self.client.post(self.url, post)
        self.assertFalse(ProblemData.objects.get(problem=self.problem).zipfile)
        self.assertFalse((self._dir() / 'data.zip').exists())


class BridgeStartupTestCase(CommonDataMixin, TestCase):
    def test_second_bridge_that_cannot_bind_leaves_judging_alone(self):
        # A bridge started while another one holds the ports used to mark every judge offline
        # and every submission being graded as IE before failing to bind.
        judge = Judge.objects.create(name='jd-judge', auth_key='k' * 20, online=True)
        problem = create_problem(code='jdbridge')
        sub = Submission.objects.create(user=self.users['normal'].profile, problem=problem,
                                        language=Language.get_default_language(), status='G')
        with mock.patch.object(bridge_daemon, 'Server', side_effect=OSError(98, 'Address already in use')):
            with self.assertRaises(OSError):
                bridge_daemon.judge_daemon()
        judge.refresh_from_db()
        sub.refresh_from_db()
        self.assertTrue(judge.online)
        self.assertEqual(sub.status, 'G')

    def test_second_port_failing_closes_the_first(self):
        first = mock.Mock()
        with mock.patch.object(bridge_daemon, 'Server', side_effect=[first, OSError(98, 'in use')]):
            with self.assertRaises(OSError):
                bridge_daemon.judge_daemon()
        first.close.assert_called_once_with()


class BridgeServerShutdownTestCase(TestCase):
    def test_connection_threads_do_not_keep_the_process_alive(self):
        self.assertTrue(ThreadingTCPListener.daemon_threads)

    def test_shutdown_releases_the_port(self):
        server = Server([('127.0.0.1', 0)], mock.Mock())
        port = server.servers[0].server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        server.shutdown()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        # Before, the listening socket stayed open after shutdown.
        self.assertEqual(server.servers[0].socket.fileno(), -1)
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', port))


class StatusPageWithoutEventDaemonTestCase(CommonDataMixin, TestCase):
    """Without the event daemon the status page stayed on "RESULT —" until reloaded."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem = create_problem(code='jdstatus', is_public=True)

    def _submission(self, **kwargs):
        sub = Submission.objects.create(user=self.users['normal'].profile, problem=self.problem,
                                        language=Language.get_default_language(), **kwargs)
        SubmissionSource.objects.create(submission=sub, source='print(1)')
        return sub

    @override_settings(EVENT_DAEMON_USE=False)
    def test_pending_submission_page_polls(self):
        sub = self._submission(status='QU')
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('submission_status', args=[sub.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No event daemon (EVENT_DAEMON_USE off): poll")
        self.assertContains(response, 'data-graded="0"')

    @override_settings(EVENT_DAEMON_USE=False)
    def test_graded_submission_page_does_not_poll(self):
        sub = self._submission(status='D', result='AC', points=1, case_points=1, case_total=1)
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('submission_status', args=[sub.id]))
        self.assertNotContains(response, "No event daemon (EVENT_DAEMON_USE off): poll")
        fragment = self.client.get(reverse('submission_testcases_query'), {'id': sub.id})
        self.assertContains(fragment, 'data-graded="1"')


class GradePageTypeLabelTestCase(TestCase):
    def test_meta_line_shows_the_question_type_name(self):
        # The grading page showed the raw code, e.g. "A. Sum · C · C" for a C program.
        source = Path(settings.BASE_DIR, 'templates', 'contest', 'grade.html').read_text()
        self.assertIn('esc(problemTypeLabel(s.problem_type))', source)
        self.assertNotIn("esc(s.problem_type || '—')", source)


class SeedDevDataTestCase(TestCase):
    fixtures = ['language_small.json']

    def test_seed_creates_the_hidden_ide_problem(self):
        # /ide/ and the workspace "Run test" answer 500 "IDE feature not configured" without it.
        # Not override_settings(DEBUG=True): it leaves later tests without response.context.
        from unittest import mock
        from judge.management.commands import seed_dev_data
        with mock.patch.object(seed_dev_data, 'settings', mock.Mock(DEBUG=True)):
            call_command('seed_dev_data', stdout=io.StringIO())
        ide = Problem.objects.get(code='idepractice')
        self.assertFalse(ide.is_public)
        self.assertTrue(ide.allowed_languages.filter(key='PY3').exists())
        self.assertTrue(User.objects.filter(username='ab101').exists())


class DevJudgeFilesTestCase(TestCase):
    def test_ide_judge_data_is_shipped(self):
        root = Path(settings.BASE_DIR, 'scripts', 'dev-judge', 'idepractice')
        for name in ('init.yml', 'ide_grader.py', 'ide.in', 'ide.out'):
            self.assertTrue(os.path.exists(root / name), name)
        self.assertIn('custom_judge: ide_grader.py', (root / 'init.yml').read_text())
