"""Fixes from exercising every teacher/admin upload, import, export, save and delete action in a
real browser (docs/classroom/reports/agent-d-actions.md). Each test fails on the code before its fix."""
import io

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from judge.models import Class, ProblemImportDraft
from judge.models.tests.util import create_organization, create_user
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
