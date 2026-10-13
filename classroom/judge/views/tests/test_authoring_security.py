"""Regression tests for the problem-authoring / import / export review findings (AUTH-01 ... AUTH-12)
and for the exam-integrity rule: no statement or hidden test material of an assignment reaches a
non-editor before (or, for hidden test material, during) the assignment."""
import importlib
import io
import json
import os
import tempfile
import zipfile
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.management import call_command, get_commands
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import clear_url_caches, resolve, reverse
from django.utils import timezone

from judge.models import (ContestParticipation, ContestSubmission, Language, Problem, ProblemHarness,
                          ProblemImportDraft, ProblemImportItem, ProblemTestCase, ProblemTranslation, ProblemType,
                          Rating, Submission, SubmissionSource, SubmissionTestCase, problem_data_storage)
from judge.models.tests.util import (CommonDataMixin, create_contest, create_contest_problem, create_organization,
                                     create_problem, create_user)
from judge.storage.resilient import ResilientStorage
from judge.utils.problem_create import default_problem_group
from judge.views.problem import get_workspace_answer_display


class TempProblemDataMixin:
    """Point problem_data_storage at a temporary directory for the test."""

    def setUp(self):
        super().setUp()
        self._data_dir = tempfile.TemporaryDirectory()
        self._old_location = problem_data_storage.location
        problem_data_storage.location = self._data_dir.name

    def tearDown(self):
        problem_data_storage.location = self._old_location
        self._data_dir.cleanup()
        super().tearDown()

    def write_case(self, problem, order=1):
        os.makedirs(os.path.join(self._data_dir.name, problem.code), exist_ok=True)
        for name, body in (('%d.in' % order, 'secret input'), ('%d.out' % order, 'secret output')):
            with open(os.path.join(self._data_dir.name, problem.code, name), 'w') as f:
                f.write(body)
        return ProblemTestCase.objects.create(dataset=problem, order=order, input_file='%d.in' % order,
                                              output_file='%d.out' % order, points=1, is_pretest=False)


# --------------------------------------------------------------------------- AUTH-01


class ContestDocumentExportAccessTest(CommonDataMixin, TestCase):
    """AUTH-01: only editors of the assignment may export its statements."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher = create_user(username='export_teacher', user_permissions=('edit_own_contest',))
        cls.student = create_user(username='export_student')
        now = timezone.now()
        cls.contest = create_contest(
            key='upcomingexam', is_visible=True, authors=('export_teacher',),
            start_time=now + timedelta(days=2), end_time=now + timedelta(days=3),
        )
        cls.first = create_problem(code='exportfirst', description='ALPHA statement', is_public=False)
        cls.second = create_problem(code='exportsecond', description='', is_public=False)
        create_contest_problem(contest=cls.contest, problem=cls.first, order=1)
        create_contest_problem(contest=cls.contest, problem=cls.second, order=2)

    def _post(self, name):
        return self.client.post(reverse(name, args=[self.contest.key]))

    def test_student_cannot_export_upcoming_assignment(self):
        self.assertTrue(self.contest.is_accessible_by(self.student))
        self.client.force_login(self.student)
        with mock.patch('judge.utils.contest_export.convert_contest_html', return_value=b'%PDF-1.4'):
            for name in ('contest_export_pdf', 'contest_export_word'):
                with self.subTest(name=name):
                    response = self._post(name)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn(b'ALPHA', response.content)

    def test_anonymous_cannot_export_public_assignment(self):
        with mock.patch('judge.utils.contest_export.convert_contest_html', return_value=b'%PDF-1.4'):
            for name in ('contest_export_pdf', 'contest_export_word'):
                with self.subTest(name=name):
                    self.assertEqual(self._post(name).status_code, 404)

    def test_editor_can_export_and_empty_description_is_not_reused(self):
        self.client.force_login(self.teacher)
        with mock.patch('judge.utils.contest_export.convert_contest_html', return_value=b'%PDF-1.4') as convert:
            response = self._post('contest_export_pdf')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'application/pdf')
            html, output_format = convert.call_args[0]
            self.assertEqual(output_format, 'pdf')
            # The second problem has no description: it must not repeat the first one's.
            self.assertEqual(html.count('ALPHA statement'), 1)

            response = self._post('contest_export_word')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(convert.call_args[0][1], 'docx')

    def test_first_problem_without_description_exports(self):
        self.contest.contest_problems.filter(problem=self.first).update(order=3)
        self.client.force_login(self.teacher)
        with mock.patch('judge.utils.contest_export.convert_contest_html', return_value=b'%PDF-1.4'):
            self.assertEqual(self._post('contest_export_pdf').status_code, 200)

    def test_titles_are_escaped(self):
        from judge.utils.contest_export import build_contest_export_html
        self.contest.name = '<script type="math/tex">\\input{/etc/passwd}</script>'
        self.contest.description = '<img src="http://169.254.169.254/x">'
        html = build_contest_export_html(self.contest, self.contest.contest_problems.select_related('problem'))
        self.assertNotIn('<script type="math/tex">', html)
        self.assertNotIn('<img src="http://169.254.169.254', html)


# --------------------------------------------------------------------------- AUTH-02 / AUTH-07


class HiddenTestMaterialDownloadTest(TempProblemDataMixin, CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.author = create_user(username='tc_author', user_permissions=('edit_own_problem',))
        cls.student = create_user(username='tc_student')
        cls.problem = create_problem(code='tcprob', is_public=True, view_test_cases=True, view_tester=True,
                                     authors=('tc_author',))
        cls.language = Language.objects.get(key='PY3')
        ProblemHarness.objects.create(problem=cls.problem, language=cls.language, harness_code='HIDDEN HARNESS')

    def setUp(self):
        super().setUp()
        self.write_case(self.problem)

    def _submission(self, user, contest=None):
        submission = Submission.objects.create(user=user.profile, problem=self.problem, language=self.language,
                                               result='WA', status='D', points=0, case_points=0, case_total=1)
        SubmissionTestCase.objects.create(submission=submission, case=1, status='WA', points=0, total=1)
        return submission

    def _download(self, submission, kind='in'):
        return self.client.get(reverse('submission_test_case_download', args=[submission.id, 1, kind]))

    def _tester(self, problem=None):
        return self.client.get(reverse('problem_download_tester', args=[(problem or self.problem).code]))

    def test_practice_download_allowed_without_assignment(self):
        submission = self._submission(self.student)
        self.client.force_login(self.student)
        response = self._download(submission)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response), b'secret input')
        self.assertEqual(self._tester().status_code, 200)

    def test_no_download_while_problem_is_in_running_assignment(self):
        create_contest_problem(contest=create_contest(key='tcrunning'), problem=self.problem)
        submission = self._submission(self.student)
        self.client.force_login(self.student)
        self.assertEqual(self._download(submission).status_code, 404)
        self.assertEqual(self._download(submission, 'out').status_code, 404)
        self.assertEqual(self._tester().status_code, 404)

    def test_no_download_while_problem_is_in_upcoming_assignment(self):
        now = timezone.now()
        contest = create_contest(key='tcupcoming', start_time=now + timedelta(days=1),
                                 end_time=now + timedelta(days=2))
        create_contest_problem(contest=contest, problem=self.problem)
        submission = self._submission(self.student)
        self.client.force_login(self.student)
        self.assertEqual(self._download(submission).status_code, 404)
        self.assertEqual(self._tester().status_code, 404)

    def test_no_download_when_exam_results_are_hidden(self):
        now = timezone.now()
        contest = create_contest(key='tcexam', format_name='exam', start_time=now - timedelta(days=2),
                                 end_time=now - timedelta(days=1))
        contest_problem = create_contest_problem(contest=contest, problem=self.problem, hide_results=True)
        participation = ContestParticipation.objects.create(contest=contest, user=self.student.profile)
        submission = self._submission(self.student)
        submission.contest_object = contest
        submission.save()
        ContestSubmission.objects.create(submission=submission, problem=contest_problem,
                                         participation=participation)
        submission = Submission.objects.get(id=submission.id)
        self.assertFalse(submission.results_visible_to(self.student))
        self.client.force_login(self.student)
        self.assertEqual(self._download(submission).status_code, 404)

    def test_editor_keeps_access_during_assignment(self):
        create_contest_problem(contest=create_contest(key='tceditor'), problem=self.problem)
        submission = self._submission(self.student)
        self.client.force_login(self.author)
        self.assertEqual(self._download(submission).status_code, 200)
        self.assertEqual(self._tester().status_code, 200)

    def test_tester_needs_problem_access(self):
        """AUTH-07: the tester of a private / other-course problem is not served."""
        private = create_problem(code='tcprivate', is_public=False, view_tester=True, authors=('tc_author',))
        ProblemHarness.objects.create(problem=private, language=self.language, harness_code='PRIVATE HARNESS')
        course = create_organization(name='tc other course')
        org_private = create_problem(code='tcorgonly', is_public=True, is_organization_private=True,
                                     view_tester=True, organizations=('tc other course',))
        ProblemHarness.objects.create(problem=org_private, language=self.language, harness_code='COURSE HARNESS')
        self.assertTrue(course.pk)
        self.client.force_login(self.student)
        for problem in (private, org_private):
            with self.subTest(problem=problem.code):
                response = self._tester(problem)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(b'HARNESS', response.content)
        self.client.force_login(self.author)
        self.assertEqual(self._tester(private).status_code, 200)


# --------------------------------------------------------------------------- exam integrity


class UpcomingAssignmentStatementTest(TempProblemDataMixin, CommonDataMixin, TestCase):
    """A private problem of an upcoming assignment is not served to an enrolled student by any path."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher = create_user(username='ua_teacher', user_permissions=('edit_own_problem', 'edit_own_contest'))
        cls.student = create_user(username='ua_student')
        cls.course = create_organization(name='ua course', admins=('ua_teacher',))
        cls.student.profile.organizations.add(cls.course)
        now = timezone.now()
        cls.contest = create_contest(
            key='uaexam', is_visible=True, is_organization_private=True, organizations=('ua course',),
            authors=('ua_teacher',), start_time=now + timedelta(days=1), end_time=now + timedelta(days=2),
        )
        cls.problem = create_problem(code='uaprob', is_public=False, description='EXAM STATEMENT',
                                     view_tester=True, view_test_cases=True, authors=('ua_teacher',))
        ProblemHarness.objects.create(problem=cls.problem, language=Language.objects.get(key='PY3'),
                                      harness_code='EXAM HARNESS')
        create_contest_problem(contest=cls.contest, problem=cls.problem)

    def test_no_statement_before_start(self):
        self.assertTrue(self.contest.is_accessible_by(self.student))
        self.client.force_login(self.student)
        responses = {
            'problem page': self.client.get(reverse('problem_detail', args=[self.problem.code])),
            'legacy problem page': self.client.get(reverse('problem_detail', args=[self.problem.code]),
                                                   {'legacy': '1'}),
            'tester': self.client.get(reverse('problem_download_tester', args=[self.problem.code])),
            'pdf export': self.client.post(reverse('contest_export_pdf', args=[self.contest.key])),
            'word export': self.client.post(reverse('contest_export_word', args=[self.contest.key])),
            'assignment preview': self.client.get(reverse('problem_detail', args=[self.problem.code]),
                                                  {'assignment_preview': '1', 'preview_codes': self.problem.code}),
        }
        for label, response in responses.items():
            with self.subTest(path=label):
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(b'EXAM STATEMENT', response.content)
                self.assertNotIn(b'EXAM HARNESS', response.content)

    def test_teacher_still_sees_statement(self):
        self.client.force_login(self.teacher)
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'EXAM STATEMENT')


# --------------------------------------------------------------------------- AUTH-03


class ImportTestDataStorageTest(TempProblemDataMixin, CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.creator = create_user(username='zip_creator', is_staff=True,
                                  user_permissions=('add_problem', 'create_private_problem'))
        default_problem_group()
        ProblemType.objects.get_or_create(name='zip-type', defaults={'full_name': 'zip-type'})

    def setUp(self):
        super().setUp()
        self._zip_dir = tempfile.TemporaryDirectory()
        self.storage = ResilientStorage(primary=FileSystemStorage(location=self._zip_dir.name), backup=None)
        field = ProblemImportItem._meta.get_field('test_data_zip')
        self.original_storage = field.storage
        patcher = mock.patch.object(field, 'storage', self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._zip_dir.cleanup()
        super().tearDown()

    @staticmethod
    def _zip_bytes():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('1.in', '1 2\n')
            archive.writestr('1.out', '3\n')
        return buffer.getvalue()

    def _item(self):
        draft = ProblemImportDraft.objects.create(owner=self.creator.profile)
        return ProblemImportItem.objects.create(
            draft=draft, row_number=2, name='Zipped sum', submission_type='C', description='Add two numbers.',
            allowed_languages='PY3', tags='zip-type', test_data_zip=ContentFile(self._zip_bytes(), name='tests.zip'),
        )

    def test_zip_is_private_and_randomly_named(self):
        self.assertIsNot(self.original_storage, default_storage)
        self.assertNotIsInstance(self.original_storage, FileSystemStorage)
        item = self._item()
        name = item.test_data_zip.name
        self.assertNotIn('tests', name)
        self.assertFalse(name.startswith('problem_import_test_data/'))
        self.assertTrue(self.storage.exists(name))
        self.assertFalse(default_storage.exists(name))

    def test_zip_deleted_after_problem_is_created(self):
        item = self._item()
        name = item.test_data_zip.name
        self.client.force_login(self.creator)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('problem_import_item_action', args=(item.draft_id, item.pk, 'create')),
                {'name': item.name, 'question_type': 'C', 'description': item.description,
                 'allowed_languages': 'PY3', 'tags': 'zip-type'},
                HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            )
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertIsNotNone(item.created_problem_id)
        self.assertEqual(ProblemTestCase.objects.filter(dataset=item.created_problem).count(), 1)
        self.assertFalse(item.test_data_zip)
        self.assertFalse(self.storage.exists(name))

    def test_zip_deleted_when_draft_is_discarded(self):
        item = self._item()
        name = item.test_data_zip.name
        item.draft.delete()
        self.assertFalse(self.storage.exists(name))

    def test_debug_media_does_not_serve_legacy_import_zips(self):
        import dmoj.urls
        # Patch the attributes directly: override_settings(DEBUG=...) would reset the template engines
        # and turn off template-rendered signals (response.context) for every later test.
        try:
            with mock.patch.object(settings, 'DEBUG', True), mock.patch.object(settings, 'MEDIA_URL', '/media/'):
                urls = importlib.reload(dmoj.urls)
                clear_url_caches()
                match = resolve('/media/problem_import_test_data/tests.zip', urlconf=urls)
                self.assertIs(match.func, urls._media_not_found)
                # Ordinary uploads are still served in DEBUG.
                self.assertIsNot(resolve('/media/martor/x.png', urlconf=urls).func, urls._media_not_found)
        finally:
            importlib.reload(dmoj.urls)
            clear_url_caches()


# --------------------------------------------------------------------------- AUTH-04


@override_settings(LANGUAGES=[('en', 'English')])
class InlineScriptJsonTest(CommonDataMixin, TestCase):
    payload = '</script><script>window.pwned=1</script><!--'

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.creator = create_user(username='xss_creator', user_permissions=('create_private_problem',
                                                                            'edit_own_problem'))
        ProblemType.objects.create(name='xss-type', full_name=cls.payload)
        cls.problem = create_problem(code='xssprob', description=cls.payload, name='xss', authors=('xss_creator',))

    def assertNoBreakout(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'<script>window.pwned=1', response.content)

    def test_create_page_escapes_problem_types(self):
        self.client.force_login(self.creator)
        self.assertNoBreakout(self.client.get(reverse('problem_create_v2')))

    def test_edit_page_escapes_edit_data(self):
        self.client.force_login(self.creator)
        self.assertNoBreakout(self.client.get(reverse('problem_edit_v2', args=[self.problem.code])))

    def test_user_page_escapes_contest_names_in_rating_data(self):
        contest = create_contest(key='xsscontest', name=self.payload)
        participation = ContestParticipation.objects.create(contest=contest, user=self.creator.profile)
        Rating.objects.create(user=self.creator.profile, contest=contest, participation=participation, rank=1,
                              rating=1500, mean=1500, performance=1500, last_rated=timezone.now())
        self.creator.profile.rating = 1500
        self.creator.profile.save(update_fields=['rating'])
        self.assertNoBreakout(self.client.get(reverse('user_page', args=[self.creator.username])))

    def test_script_json_escapes(self):
        from judge.utils.safe_json import script_json
        value = script_json({'a': '</script>&  '}, ensure_ascii=False)
        for raw in ('<', '>', '&', ' ', ' '):
            self.assertNotIn(raw, value)
        self.assertEqual(json.loads(value), {'a': '</script>&  '})


# --------------------------------------------------------------------------- AUTH-05 / 09 / 10


class ProblemCreateV2PermissionTest(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.group = default_problem_group()
        cls.creator = create_user(username='perm_creator', user_permissions=('create_private_problem',
                                                                             'edit_own_problem'))
        cls.full_markup_editor = create_user(username='perm_full', user_permissions=(
            'create_private_problem', 'edit_own_problem', 'problem_full_markup'))
        cls.taught = create_organization(name='perm taught')
        cls.taught.admins.add(cls.creator.profile)
        cls.other = create_organization(name='perm other')

    def _post(self, payload, code=None):
        url = reverse('problem_edit_v2', args=[code]) if code else reverse('problem_create_v2')
        return self.client.post(url, data=json.dumps(payload), content_type='application/json')

    def _payload(self, **extra):
        payload = {
            'problemTypes': ['Graph Theory'], 'type': 'short', 'name': 'Perm problem',
            'description': 'Explain it.', 'points': 1, 'group_id': self.group.id, 'shortAnswer': 'ref',
        }
        payload.update(extra)
        return payload

    def _full_markup_problem(self):
        problem = create_problem(code='permfull', description='<b>admin html</b>', is_full_markup=True,
                                 authors=('perm_creator', 'perm_full'))
        ProblemTranslation.objects.create(problem=problem, language='en', name='T', description='<i>tr</i>')
        return problem

    # AUTH-05
    def test_full_markup_description_is_read_only_without_permission(self):
        problem = self._full_markup_problem()
        self.client.force_login(self.creator)
        response = self._post(self._payload(edit_code=problem.code, is_full_markup=True,
                                            description='<script>alert(1)</script>'), problem.code)
        self.assertEqual(response.status_code, 403)
        problem.refresh_from_db()
        self.assertEqual(problem.description, '<b>admin html</b>')

    @override_settings(LANGUAGES=[('en', 'English')])
    def test_full_markup_translations_are_read_only_without_permission(self):
        problem = self._full_markup_problem()
        self.client.force_login(self.creator)
        response = self._post(self._payload(
            edit_code=problem.code, is_full_markup=True, description=problem.description,
            translations=[{'language': 'en', 'name': 'T', 'description': '<script>x</script>'}],
        ), problem.code)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(problem.translations.get().description, '<i>tr</i>')

    @override_settings(LANGUAGES=[('en', 'English')])
    def test_full_markup_problem_other_fields_still_editable(self):
        problem = self._full_markup_problem()
        self.client.force_login(self.creator)
        response = self._post(self._payload(
            edit_code=problem.code, is_full_markup=True, description=problem.description, name='Renamed',
            translations=[{'language': 'en', 'name': 'T', 'description': '<i>tr</i>'}],
        ), problem.code)
        self.assertEqual(response.status_code, 200, response.content)
        problem.refresh_from_db()
        self.assertEqual(problem.name, 'Renamed')

    def test_full_markup_permission_allows_description_change(self):
        problem = self._full_markup_problem()
        self.client.force_login(self.full_markup_editor)
        response = self._post(self._payload(edit_code=problem.code, is_full_markup=True,
                                            description='<b>new</b>'), problem.code)
        self.assertEqual(response.status_code, 200, response.content)
        problem.refresh_from_db()
        self.assertEqual(problem.description, '<b>new</b>')

    # AUTH-09
    def test_unknown_course_id_does_not_make_problem_public(self):
        self.client.force_login(self.creator)
        response = self._post(self._payload(is_public=True, organizations=[987654]))
        self.assertIn(response.status_code, (400, 403))
        self.assertFalse(Problem.objects.filter(name='Perm problem', is_public=True,
                                                is_organization_private=False).exists())

    # AUTH-10
    def test_cannot_attach_course_not_taught(self):
        self.creator.profile.organizations.add(self.other)  # a member, not a teacher
        self.client.force_login(self.creator)
        response = self._post(self._payload(organizations=[self.other.id]))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Problem.objects.filter(name='Perm problem').exists())

    def test_can_attach_taught_course(self):
        self.client.force_login(self.creator)
        response = self._post(self._payload(organizations=[self.taught.id], is_public=True))
        self.assertEqual(response.status_code, 200, response.content)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertEqual(list(problem.organizations.all()), [self.taught])
        self.assertTrue(problem.is_organization_private)

    def test_existing_foreign_course_is_kept_on_edit(self):
        problem = create_problem(code='permkeep', authors=('perm_creator',), organizations=('perm other',),
                                 is_organization_private=True)
        self.client.force_login(self.creator)
        response = self._post(self._payload(edit_code=problem.code, organizations=[self.other.id, self.taught.id]),
                              problem.code)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertSetEqual(set(problem.organizations.values_list('id', flat=True)), {self.other.id, self.taught.id})


# --------------------------------------------------------------------------- AUTH-08


class StudentAnswerRenderingTest(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.student = create_user(username='answer_student')

    def _display(self, problem, text):
        submission = Submission.objects.create(user=self.student.profile, problem=problem,
                                               language=Language.get_answer_language(), status='D', result='AC')
        SubmissionSource.objects.create(submission=submission, source=json.dumps({'text': text}))
        return get_workspace_answer_display(Submission.objects.get(id=submission.id))

    def test_full_markup_problem_does_not_render_student_html(self):
        problem = create_problem(code='answerfull', submission_type='S', is_full_markup=True)
        rows = self._display(problem, 'Answer <img src=x onerror=alert(1)> <script>alert(2)</script>')
        html = rows[0]['html']
        self.assertNotIn('<img src="x" onerror', html)
        self.assertNotIn('<img src=x onerror', html)
        self.assertNotIn('<script>', html)
        self.assertIn('Answer', html)

    def test_normal_problem_does_not_allow_student_style(self):
        problem = create_problem(code='answerstaff', submission_type='S')
        html = self._display(problem, '<style>body{display:none}</style> **bold**')[0]['html']
        self.assertNotIn('<style>', html)
        self.assertIn('<strong>bold</strong>', html)


# --------------------------------------------------------------------------- AUTH-12


class LegacyCommandsRemovedTest(TestCase):
    def test_legacy_commands_are_gone(self):
        """AUTH-12: the old CSV import (default password 123456) and its sibling are not installed."""
        commands = get_commands()
        for name in ('import_users_csv_old', 'export_contest_submissions_old'):
            with self.subTest(command=name):
                self.assertNotIn(name, commands)
                with self.assertRaisesRegex(CommandError, 'Unknown command'):
                    call_command(name)
