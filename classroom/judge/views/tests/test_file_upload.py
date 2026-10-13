import json
import tempfile
from unittest import mock

from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from judge.models import Language, Problem, ProblemFileConfig, Submission, SubmissionFile
from judge.models.tests.util import CommonDataMixin, create_problem, create_user
from judge.storage.resilient import ResilientStorage
from judge.utils.problem_create import default_problem_group


def _pdf(name='report.pdf', content=b'%PDF-1.4 hello'):
    return SimpleUploadedFile(name, content, content_type='application/pdf')


class FileUploadBase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.users.update({
            'creator': create_user(
                username='creator',
                user_permissions=('create_private_problem', 'edit_own_problem'),
            ),
            'other': create_user(username='other'),
        })

    def setUp(self):
        super().setUp()
        # Route the FileField at a throwaway temp dir so tests never touch real/object storage.
        self._tmp = tempfile.TemporaryDirectory()
        storage = ResilientStorage(primary=FileSystemStorage(location=self._tmp.name), backup=None)
        self._patch = mock.patch.object(SubmissionFile._meta.get_field('file'), 'storage', storage)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()
        super().tearDown()

    def make_problem(self, code='up1', **kwargs):
        opts = dict(submission_type='F', points=10, partial=True, is_public=True,
                    submission_source_visibility_mode='O', authors=('creator',))
        opts.update(kwargs)
        problem = create_problem(code=code, **opts)
        ProblemFileConfig.objects.create(
            problem=problem, allowed_extensions='pdf', max_file_size=1024)
        return problem


class FileUploadAuthoringTestCase(FileUploadBase):
    """Authoring happens on the unified create_v2 page, not a per-type page."""

    def test_create_page_offers_file_type(self):
        self.client.force_login(self.users['creator'])
        response = self.client.get(reverse('problem_create_v2'))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'value="file"', response.content)          # the type pill
        self.assertIn(b'data-for="file"', response.content)       # its author-config panel
        self.assertIn(b'f-file-ext', response.content)

        # Not available to a user without the permission.
        self.client.force_login(self.users['normal'])
        self.assertEqual(self.client.get(reverse('problem_create_v2')).status_code, 404)

    def test_create_flow(self):
        self.client.force_login(self.users['creator'])
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps({
            'type': 'file', 'name': 'Upload PDF', 'description': 'Upload your report',
            'points': 10, 'group_id': default_problem_group().id,
            'problemTypes': ['Graph Theory'],
            'fileExtensions': '.PDF, zip', 'fileMaxSizeMb': 5, 'fileInstructions': 'PDF only',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertEqual(problem.submission_type, 'F')
        # Manual grading awards an arbitrary score, so partial credit must be forced on.
        self.assertTrue(problem.partial)
        # Extensions are normalised to the lowercase, dot-less form the config helper expects.
        self.assertEqual(problem.file_config.allowed_extensions, 'pdf,zip')
        self.assertEqual(problem.file_config.allowed_extension_list(), ['pdf', 'zip'])
        self.assertEqual(problem.file_config.max_file_size, 5 * 1024 * 1024)
        self.assertEqual(problem.file_config.instructions, 'PDF only')

    def test_create_rejects_zero_max_size(self):
        self.client.force_login(self.users['creator'])
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps({
            'type': 'file', 'name': 'No cap', 'description': 'x',
            'points': 10, 'group_id': default_problem_group().id,
            'problemTypes': ['Graph Theory'], 'fileMaxSizeMb': 0,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Problem.objects.filter(name='No cap').exists())

    def test_create_requires_permission(self):
        self.client.force_login(self.users['normal'])
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps({
            'type': 'file', 'name': 'Nope', 'description': 'x',
            'points': 10, 'group_id': default_problem_group().id, 'fileMaxSizeMb': 5,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_edit_round_trip(self):
        problem = self.make_problem(code='editf')
        url = reverse('problem_edit_v2', args=['editf'])

        self.client.force_login(self.users['normal'])
        self.assertEqual(self.client.get(url).status_code, 404)

        self.client.force_login(self.users['creator'])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # The existing config is serialized back into the page as MB, not bytes.
        self.assertIn(b'"fileMaxSizeMb"', response.content)

        response = self.client.post(url, data=json.dumps({
            'type': 'file', 'edit_code': 'editf', 'name': 'Renamed', 'description': 'new prompt',
            'points': 10, 'group_id': default_problem_group().id,
            'fileExtensions': 'docx', 'fileMaxSizeMb': 2, 'fileInstructions': 'Word only',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        problem.refresh_from_db()
        self.assertEqual(problem.name, 'Renamed')
        # _clear_type_children drops the old config; exactly one must remain.
        self.assertEqual(ProblemFileConfig.objects.filter(problem=problem).count(), 1)
        self.assertEqual(problem.file_config.allowed_extensions, 'docx')
        self.assertEqual(problem.file_config.max_file_size, 2 * 1024 * 1024)

    def test_type_switch_keeps_student_uploads(self):
        """Switching a problem's type clears its upload config but never student data."""
        problem = self.make_problem(code='switchf')
        submission = Submission.objects.create(
            user=self.users['normal'].profile, problem=problem, language=Language.get_python3())
        SubmissionFile.objects.create(
            submission=submission, file=_pdf(), original_name='report.pdf', size=14)

        self.client.force_login(self.users['creator'])
        response = self.client.post(reverse('problem_edit_v2', args=['switchf']), data=json.dumps({
            'type': 'short', 'edit_code': 'switchf', 'name': 'Now short', 'description': 'x',
            'points': 10, 'group_id': default_problem_group().id, 'shortAnswer': 'answer',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        problem.refresh_from_db()
        self.assertEqual(problem.submission_type, 'S')
        self.assertFalse(ProblemFileConfig.objects.filter(problem=problem).exists())
        self.assertTrue(SubmissionFile.objects.filter(submission=submission).exists())


class FileUploadSubmitTestCase(FileUploadBase):
    def setUp(self):
        super().setUp()
        self.problem = self.make_problem(code='sub1')

    def test_submit_stores_file_and_stays_queued(self):
        self.client.force_login(self.users['normal'])
        response = self.client.post(reverse('problem_submit', args=['sub1']), {'file': _pdf()})
        self.assertEqual(response.status_code, 302)

        sub = Submission.objects.get(problem=self.problem, user=self.users['normal'].profile)
        self.assertEqual(sub.status, 'QU')          # manual: waits for a human, never auto-graded
        self.assertIsNone(sub.result)
        self.assertEqual(sub.language.key, 'ANSWER')
        self.assertEqual(sub.upload.original_name, 'report.pdf')
        self.assertEqual(sub.upload.size, len(b'%PDF-1.4 hello'))
        self.assertTrue(sub.upload.file.storage.exists(sub.upload.file.name))

    def test_submit_rejects_bad_extension(self):
        self.client.force_login(self.users['normal'])
        response = self.client.post(reverse('problem_submit', args=['sub1']),
                                    {'file': _pdf(name='evil.exe')})
        self.assertEqual(response.status_code, 200)  # re-render with error, no redirect
        self.assertContains(response, 'not allowed')
        self.assertFalse(Submission.objects.filter(problem=self.problem).exists())

    def test_submit_rejects_oversize(self):
        self.client.force_login(self.users['normal'])
        big = _pdf(content=b'x' * 4096)  # config cap is 1024 bytes
        response = self.client.post(reverse('problem_submit', args=['sub1']), {'file': big})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'too large')
        self.assertFalse(Submission.objects.filter(problem=self.problem).exists())

    def test_detail_and_submit_pages_render_file_form(self):
        self.client.force_login(self.users['normal'])
        for url in (reverse('problem_detail', args=['sub1']), reverse('problem_submit', args=['sub1'])):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'multipart/form-data')
            self.assertContains(response, 'type="file"')
            self.assertNotContains(response, 'id_source')  # no Ace code editor on a file problem


class FileUploadGradeDownloadTestCase(FileUploadBase):
    def setUp(self):
        super().setUp()
        self.problem = self.make_problem(code='gd1')
        self.sub = Submission.objects.create(
            user=self.users['normal'].profile, problem=self.problem,
            language=Language.get_answer_language(),
        )
        SubmissionFile.objects.create(
            submission=self.sub, file=_pdf(), original_name='report.pdf', size=14)

    def test_legacy_grade_file_endpoint_removed(self):
        # G-9: the old per-submission grade endpoint was authorized by problem
        # ownership (not assignment editing rights) and wrote the score straight
        # into the ranking, bypassing Release All. Nothing in the UI used it, so
        # it is gone; grading goes through the Grade desk.
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse('submission_file_grade', args=[self.sub.id])
        self.client.force_login(self.users['creator'])
        response = self.client.post('/submission/%d/grade-file' % self.sub.id, {'points': '7'})
        self.assertEqual(response.status_code, 404)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'QU')
        self.assertIsNone(self.sub.points)

    def test_status_page_view_does_not_abort_old_upload(self):
        # G-8: a file upload waits at QU for a teacher by design. Opening its
        # status page long after submission must not mark it Aborted / 0 points.
        from datetime import timedelta
        from django.utils import timezone
        Submission.objects.filter(id=self.sub.id).update(
            status='QU', date=timezone.now() - timedelta(hours=2))
        for user in ('normal', 'creator'):
            self.client.force_login(self.users[user])
            response = self.client.get(reverse('submission_status', args=[self.sub.id]))
            self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'QU')
        self.assertIsNone(self.sub.result)
        self.assertIsNone(self.sub.points)

    def test_download_allowed_for_owner_denied_for_other(self):
        self.client.force_login(self.users['normal'])            # the submitter
        response = self.client.get(reverse('submission_file_download', args=[self.sub.id]))
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertIn('report.pdf', response['Content-Disposition'])

        self.client.force_login(self.users['other'])             # unrelated user (ONLY_OWN visibility)
        self.assertEqual(
            self.client.get(reverse('submission_file_download', args=[self.sub.id])).status_code, 403)

    def test_rejudge_is_noop(self):
        with mock.patch('judge.models.submission.judge_submission') as bridge:
            self.sub.judge(rejudge=True, force_judge=True)
        bridge.assert_not_called()
        self.sub.refresh_from_db()
        self.assertNotEqual(self.sub.result, 'IE')

    def test_status_page_shows_download_not_grade_form(self):
        # Result page: download + awaiting grading. The old per-submission
        # file-grade form is not here; teachers open the grading desk instead.
        self.client.force_login(self.users['creator'])
        response = self.client.get(reverse('submission_status', args=[self.sub.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('submission_file_download', args=[self.sub.id]))
        self.assertContains(response, 'Awaiting grading')
        self.assertContains(response, reverse('practice_grade'))
        self.assertNotContains(response, 'waiting for a suitable judge')
        self.assertNotContains(response, 'Abort')

        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('submission_status', args=[self.sub.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('submission_file_download', args=[self.sub.id]))
        self.assertNotContains(response, 'Grade this submission')


class FileUploadWorkspaceTestCase(FileUploadBase):
    def setUp(self):
        super().setUp()
        self.problem = self.make_problem(code='wsfile')
        config = self.problem.file_config
        config.instructions = 'Please upload a PDF report.'
        config.max_file_size = 5 * 1024 * 1024
        config.save()

    def test_workspace_shows_upload_hints(self):
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('problem_detail', args=['wsfile']))
        self.assertContains(response, 'id="workspace"')
        self.assertContains(response, 'Please upload a PDF report.')
        self.assertContains(response, 'Allowed types:')
        self.assertContains(response, 'pdf')
        self.assertContains(response, 'Max size:')
        self.assertContains(response, '5 MB')
        self.assertContains(response, 'type="file"')
        self.assertContains(response, 'multipart/form-data')
        self.assertContains(response, 'data-is-file="1"')

    def test_workspace_result_has_download_and_awaiting_grading(self):
        self.client.force_login(self.users['normal'])
        sub = Submission.objects.create(
            user=self.users['normal'].profile, problem=self.problem,
            language=Language.get_answer_language(), status='QU',
        )
        SubmissionFile.objects.create(
            submission=sub, file=_pdf(), original_name='report.pdf', size=14,
        )
        response = self.client.get(
            reverse('problem_detail', args=['wsfile']) + '?submission=%d' % sub.id,
        )
        self.assertContains(response, reverse('submission_file_download', args=[sub.id]))
        self.assertContains(response, 'Download uploaded file')
        self.assertContains(response, 'report.pdf')
        self.assertContains(response, 'Awaiting grading')
        self.assertContains(response, 'File submitted')
        self.assertNotContains(response, 'waiting for a suitable judge')
        self.assertNotContains(response, 'Grade this submission')
        self.assertNotContains(response, 'id="test-cases"')

    def test_workspace_submit_json_includes_file_download(self):
        self.client.force_login(self.users['normal'])
        response = self.client.post(
            reverse('problem_submit', args=['wsfile']),
            {'workspace': '1', 'file': _pdf()},
            HTTP_X_WORKSPACE_SUBMIT='1',
        )
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        download = reverse('submission_file_download', args=[payload['submission_id']])
        self.assertEqual(payload['file_upload']['name'], 'report.pdf')
        self.assertIn(download, payload['file_upload']['url'])
        self.assertFalse(payload['is_graded'])
        status = self.client.get(payload['status_url'])
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertEqual(body['file_upload']['name'], 'report.pdf')
        self.assertIn(download, body['file_upload']['url'])

    def test_workspace_submit_oversize_keeps_size_error(self):
        self.problem.file_config.max_file_size = 1024
        self.problem.file_config.save()
        self.client.force_login(self.users['normal'])
        response = self.client.post(
            reverse('problem_submit', args=['wsfile']),
            {'workspace': '1', 'file': _pdf(content=b'x' * 4096)},
            HTTP_X_WORKSPACE_SUBMIT='1',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('too large', response.json()['error'].lower())
