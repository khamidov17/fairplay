"""Fixes from exercising every teacher/admin upload, import, export, save and delete action in a
real browser (docs/classroom/reports/agent-d-actions.md). Each test fails on the code before its fix."""
import io

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from judge.models import Class, Contest, ProblemImportDraft
from judge.models.tests.util import create_contest, create_organization, create_user
from judge.utils.import_export import process_user_csv
from judge.utils.simple_xlsx import build_xlsx_workbook


class RosterImportTestCase(TestCase):
    def test_blank_email_cell_falls_back_to_netid_duke_column(self):
        content = (
            'NetID,First name,Last Name,Email,netid@duke.edu\n'
            'oy002,Omar,Yang,,oy002@duke.edu\n'
        ).encode('utf-8')
        result = process_user_csv(io.BytesIO(content))
        self.assertEqual(result.created, 1)
        self.assertEqual(User.objects.get(username='oy002').email, 'oy002@duke.edu')

    def test_unchanged_existing_account_is_not_counted_as_updated(self):
        org = create_organization(name='Count Course')
        klass = Class.objects.create(organization=org, name='Count Class', slug='count-class')
        member = create_user(username='cnt_member', first_name='Ann', last_name='Lee', email='ann@example.com')
        org.members.add(member.profile)
        klass.members.add(member.profile)
        # Account with a password: an import never changes it, so it must not say "updated".
        content = 'username,first_name,last_name,email\ncnt_member,Other,Name,x@example.com\n'.encode()
        result = process_user_csv(io.BytesIO(content), organization=org, target_class=klass, update_existing=True)
        self.assertEqual(result.updated, 0)
        self.assertEqual(result.skipped, 1)


class CourseImportLinkTestCase(TestCase):
    def test_course_admin_sees_import_students_link(self):
        admin = create_user(username='imp_admin')
        org = create_organization(name='implink', slug='implink', admins=('imp_admin',))
        self.client.force_login(admin)
        response = self.client.get(reverse('organization_home', args=[org.id, org.slug]))
        self.assertContains(response, reverse('organization_import_users', args=[org.id, org.slug]))


class ProblemCreatorTestCase(TestCase):
    def test_new_choice_question_has_no_demo_options(self):
        # A new single/multiple choice question started with the options "HTTP" and "TCP" filled in,
        # which were saved as real options when the teacher only edited part of the list.
        admin = create_user(username='pc_admin', is_superuser=True, is_staff=True)
        self.client.force_login(admin)
        response = self.client.get(reverse('problem_create_v2'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "addOption('HTTP')")
        self.assertContains(response, 'addOption(); addOption();')


class ExcelImportTestCase(TestCase):
    def setUp(self):
        # A course instructor: may create private problems, but is not staff.
        self.teacher = create_user(username='xl_teacher',
                                   user_permissions=('create_private_problem', 'edit_own_problem'))
        self.client.force_login(self.teacher)

    def _upload(self, rows, **extra):
        workbook = build_xlsx_workbook([('Problems', rows)])
        return self.client.post(reverse('problem_import_upload'), {
            'excel_file': SimpleUploadedFile('q.xlsx', workbook),
        }, **extra)

    def test_item_editor_can_be_framed_by_the_preview_page(self):
        # The preview page shows the editor in an iframe; DENY left it blank and "Create all" timed out.
        response = self._upload([['name', 'question_type', 'description', 'answer'],
                                 ['Q1', 'multiple_choice', 'Pick\nA. x\nB. y', 'A']],
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        draft = ProblemImportDraft.objects.get(owner=self.teacher.profile)
        item = draft.items.get()
        editor = self.client.get(reverse('problem_import_item_editor', args=[draft.pk, item.pk]))
        self.assertEqual(editor.status_code, 200)
        self.assertEqual(editor['X-Frame-Options'], 'SAMEORIGIN')

    def test_instructor_downloads_the_template(self):
        list_page = self.client.get(reverse('problem_list'))
        self.assertContains(list_page, reverse('problem_import_template'))
        response = self.client.get(reverse('problem_import_template'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content[:2], b'PK')

    def test_form_upload_error_returns_to_the_list_with_a_message(self):
        response = self._upload([['name', 'description'], ['Q1', 'text']], follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse('problem_list'))
        self.assertContains(response, 'does not contain a recognizable problem sheet')


class ContestCloneScopeTestCase(TestCase):
    def test_clone_keeps_classes_and_join_organizations(self):
        org = create_organization(name='clonescope', slug='clonescope')
        klass = Class.objects.create(organization=org, name='Clone Class', slug='clone-class')
        contest = create_contest(key='clonesrc', is_organization_private=True, limit_join_organizations=True)
        contest.organizations.add(org)
        contest.classes.add(klass)
        contest.join_organizations.add(org)
        admin = create_user(username='clone_admin', is_superuser=True, is_staff=True)
        self.client.force_login(admin)
        response = self.client.post(reverse('contest_clone', args=['clonesrc']), {'key': 'clonedst'})
        self.assertRedirects(response, reverse('contest_edit', args=['clonedst']))
        cloned = Contest.objects.get(key='clonedst')
        self.assertEqual(list(cloned.classes.all()), [klass])
        self.assertEqual(list(cloned.join_organizations.all()), [org])


class PaperExportTestCase(TestCase):
    def test_choices_assignment_points_and_readable_times(self):
        from judge.models import ProblemMCQConfig, ProblemMCQOption
        from judge.models.tests.util import create_contest_problem, create_problem
        from judge.utils.contest_export import build_contest_export_html
        problem = create_problem(code='paper_mcq', points=5, submission_type='M',
                                 description='How many times does the loop run?')
        ProblemMCQConfig.objects.create(problem=problem, multiple_select=False)
        for order, (text, correct) in enumerate((('two', False), ('three', True), ('four', False)), 1):
            ProblemMCQOption.objects.create(problem=problem, order=order, text=text, is_correct=correct)
        contest = create_contest(key='paperexp')
        cp = create_contest_problem(contest=contest, problem=problem, points=8)
        html = build_contest_export_html(contest, [cp])
        # The choices are on the paper (without saying which is correct)...
        self.assertIn('<ol type="A">', html)
        for text in ('two', 'three', 'four'):
            self.assertIn('<p>%s</p>' % text, html)
        self.assertNotIn('correct', html.lower())
        # ...the points are the assignment's, and times have no microseconds.
        self.assertIn('Total Score: 8', html)
        self.assertIn('8 - points', html)
        self.assertNotRegex(html, r'Start Time: </span>[^<]*\.\d{6}')


class JoinedScoresTestCase(TestCase):
    def test_rows_without_participant_keep_existing_values(self):
        import csv
        from judge.models.tests.util import create_contest_participation
        from judge.utils.import_export import export_contest_scores_joined_csv
        contest = create_contest(key='joinkeep', name='Quiz 1')
        alice = create_user(username='jk_alice', first_name='Alice', last_name='Qiu')
        create_contest_participation(contest=contest, user=alice.profile, score=7)
        gradebook = ('Student,ID,SIS Login ID,Quiz 1\n'
                     '    Points Possible,,,10\n'
                     '"Qiu, Alice",1,jk_alice,\n'
                     '"Else, Other",2,other,6\n').encode()
        result = export_contest_scores_joined_csv(contest, io.BytesIO(gradebook))
        rows = list(csv.DictReader(io.StringIO(result.csv_content.lstrip('﻿'))))
        self.assertEqual([row['Quiz 1'] for row in rows], ['10', '7.0', '6'])


class ScoreImportFileNameTestCase(TestCase):
    def test_file_name_handler_is_valid_inside_the_attribute(self):
        # tojson emits double quotes; inside onchange="..." they ended the attribute, so choosing a
        # file threw "Unexpected end of input" and the file name never showed.
        admin = create_user(username='sf_admin', is_superuser=True, is_staff=True)
        contest = create_contest(key='sfname', authors=('sf_admin',))
        self.client.force_login(admin)
        html = self.client.get(reverse('contest_view', args=[contest.key])).content.decode()
        start = html.index('id="contest-grade-csv"')
        tag = html[start:html.index('>', html.index('onchange=', start))]
        self.assertIn("onchange='", tag)
        self.assertIn('"No file selected"', tag)


class SeedDataTestCase(TestCase):
    fixtures = ['language_small']

    def test_finalized_quiz_has_scores(self):
        from django.core.management import call_command
        from django.test import override_settings
        from judge.models import ContestParticipation
        with override_settings(DEBUG=True):
            call_command('seed_dev_data', stdout=io.StringIO())
        scores = sorted(ContestParticipation.objects.filter(contest__key='cs101_quiz1')
                        .values_list('score', flat=True))
        self.assertTrue(any(scores), scores)


class FileUploadLimitMessageTestCase(TestCase):
    def test_too_large_is_stated_in_megabytes(self):
        from judge.forms import FileSubmitForm
        from judge.models import ProblemFileConfig
        from judge.models.tests.util import create_problem
        problem = create_problem(code='fsize', submission_type='F')
        ProblemFileConfig.objects.create(problem=problem, allowed_extensions='pdf', max_file_size=1024 * 1024)
        upload = SimpleUploadedFile('big.pdf', b'%PDF' + b'0' * (1024 * 1024 + 600 * 1024))
        form = FileSubmitForm(data={}, files={'file': upload}, problem=problem)
        self.assertFalse(form.is_valid())
        message = form.errors['file'][0]
        self.assertIn('1.6\xa0MB', message)
        self.assertIn('1.0\xa0MB', message)
        self.assertNotIn('bytes', message)
