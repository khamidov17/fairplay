from django.test import TestCase
from django.urls import reverse

from judge.models import Class
from judge.models.tests.util import CommonDataMixin, create_organization, create_user
from judge.utils.user_search import format_user_label


class OrganizationSelect2ScopeTest(CommonDataMixin, TestCase):
    """The org select2 (admin problem form) only offers orgs the user administers."""

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        # 'open' is admined by staff_organization_admin (from CommonDataMixin); 'other' by nobody.
        self.organizations['other'] = create_organization(name='other', admins=())

    def _ids(self, user):
        if user == 'anonymous':
            self.client.logout()
        else:
            self.client.force_login(self.users[user])
        results = self.client.get(reverse('organization_select2')).json()['results']
        return {r['id'] for r in results}

    def test_superuser_sees_all_orgs(self):
        ids = self._ids('superuser')
        self.assertIn(self.organizations['open'].id, ids)
        self.assertIn(self.organizations['other'].id, ids)

    def test_org_admin_sees_only_administered_org(self):
        self.assertEqual(self._ids('staff_organization_admin'), {self.organizations['open'].id})

    def test_normal_user_sees_no_orgs(self):
        self.assertEqual(self._ids('normal'), set())

    def test_anonymous_sees_no_orgs(self):
        self.assertEqual(self._ids('anonymous'), set())


class ClassSelect2ScopeTest(CommonDataMixin, TestCase):
    """The class select2 is org-first: it returns the chosen org's classes, empty otherwise."""

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.open_class = Class.objects.create(
            organization=self.organizations['open'], name='Open Class', slug='open-class')

    def _ids(self, user, query=''):
        self.client.force_login(self.users[user])
        results = self.client.get(reverse('class_select2') + query).json()['results']
        return {r['id'] for r in results}

    def test_classes_returned_for_selected_org(self):
        org_id = self.organizations['open'].id
        self.assertEqual(self._ids('staff_organization_admin', '?organizations=%d' % org_id),
                         {self.open_class.id})
        self.assertEqual(self._ids('superuser', '?organizations=%d' % org_id), {self.open_class.id})

    def test_empty_without_org(self):
        # Org-first by design: with no organization chosen the class list is empty.
        self.assertEqual(self._ids('superuser'), set())

    def test_org_admin_does_not_see_other_orgs_classes(self):
        other = create_organization(name='lonely', admins=())
        self.assertEqual(self._ids('staff_organization_admin', '?organizations=%d' % other.id), set())


class UserSelect2NameSearchTest(CommonDataMixin, TestCase):
    """profile_select2 matches username or real/display name and labels both."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.named = create_user(username='alice', first_name='张三')
        cls.override = create_user(username='bobnet')
        cls.override.profile.username_display_override = 'Bobby Tables'
        cls.override.profile.save(update_fields=['username_display_override'])
        cls.unlisted = create_user(username='ghost', first_name='Hidden')
        cls.unlisted.profile.is_unlisted = True
        cls.unlisted.profile.save(update_fields=['is_unlisted'])

    def _results(self, url_name, term, **kwargs):
        url = reverse(url_name, kwargs=kwargs) if kwargs else reverse(url_name)
        return self.client.get(url, {'term': term}).json()['results']

    def test_profile_select2_search_by_username_and_name(self):
        by_username = self._results('profile_select2', 'alice')
        self.assertTrue(any(row['id'] == self.named.profile.id for row in by_username))
        self.assertEqual(
            next(row['text'] for row in by_username if row['id'] == self.named.profile.id),
            format_user_label('张三', 'alice'),
        )

        by_name = self._results('profile_select2', '张三')
        self.assertTrue(any(row['id'] == self.named.profile.id for row in by_name))
        self.assertEqual(
            next(row['text'] for row in by_name if row['id'] == self.named.profile.id),
            '张三 (alice)',
        )

        by_override = self._results('profile_select2', 'Bobby')
        self.assertTrue(any(row['id'] == self.override.profile.id for row in by_override))
        self.assertEqual(
            next(row['text'] for row in by_override if row['id'] == self.override.profile.id),
            'Bobby Tables (bobnet)',
        )

    def test_user_search_select2_hides_unlisted_but_still_dual_search(self):
        listed = self._results('user_search_select2_ajax', '张三')
        self.assertTrue(any(row['id'] == 'alice' for row in listed))
        self.assertEqual(
            next(row['text'] for row in listed if row['id'] == 'alice'),
            '张三 (alice)',
        )
        hidden = self._results('user_search_select2_ajax', 'Hidden')
        self.assertFalse(any(row['id'] == 'ghost' for row in hidden))

    def test_profile_select2_still_returns_unlisted_users(self):
        """Teacher form pickers keep the original profile_select2 scope."""
        rows = self._results('profile_select2', 'Hidden')
        self.assertTrue(any(row['id'] == self.unlisted.profile.id for row in rows))


class AuthorFilterSelect2Test(CommonDataMixin, TestCase):
    """Students cannot enumerate users via the problem-author filter endpoint."""

    def _ids(self, user, term=''):
        if user == 'anonymous':
            self.client.logout()
        else:
            self.client.force_login(self.users[user])
        results = self.client.get(reverse('author_filter_select2'), {'term': term}).json()['results']
        return {row['id'] for row in results}

    def test_student_gets_empty_results(self):
        self.assertEqual(self._ids('normal', 'superuser'), set())

    def test_anonymous_gets_empty_results(self):
        self.assertEqual(self._ids('anonymous', 'superuser'), set())

    def test_instructor_can_search_profiles(self):
        ids = self._ids('staff_problem_edit_own', 'superuser')
        self.assertIn(self.users['superuser'].profile.id, ids)
