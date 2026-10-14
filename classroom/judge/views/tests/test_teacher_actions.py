"""Fixes from exercising every teacher/admin upload, import, export, save and delete action in a
real browser (docs/classroom/reports/agent-d-actions.md). Each test fails on the code before its fix."""
import io

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from judge.models import Class
from judge.models.tests.util import create_organization, create_user
from judge.utils.import_export import process_user_csv


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
