from django.test import TestCase
from django.urls import reverse

from judge.models.tests.util import CommonDataMixin, create_user


class MartorSearchUserTest(CommonDataMixin, TestCase):
    """Rich-editor mention search matches name or username and returns both."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.named = create_user(username='alice', first_name='张三', is_active=True)
        cls.inactive = create_user(username='oldalice', first_name='张三旧', is_active=False)

    def test_requires_login(self):
        response = self.client.get(reverse('martor_search_user'), {'username': 'alice'})
        self.assertEqual(response.status_code, 302)

    def test_search_by_username_returns_name_and_username(self):
        self.client.force_login(self.users['normal'])
        data = self.client.get(reverse('martor_search_user'), {'username': 'alice'}).json()
        self.assertEqual(data['status'], 200)
        row = next(item for item in data['data'] if item['username'] == 'alice')
        self.assertEqual(row['name'], '张三')
        self.assertEqual(row['text'], '张三 (alice)')

    def test_search_by_display_name(self):
        self.client.force_login(self.users['normal'])
        data = self.client.get(reverse('martor_search_user'), {'username': '张三'}).json()
        self.assertEqual(data['status'], 200)
        usernames = [item['username'] for item in data['data']]
        self.assertIn('alice', usernames)
        self.assertNotIn('oldalice', usernames)

    def test_search_allows_spaces_in_name(self):
        spaced = create_user(username='carolnet', first_name='Carol', last_name='Chen')
        self.client.force_login(self.users['normal'])
        data = self.client.get(reverse('martor_search_user'), {'username': 'Carol Chen'}).json()
        self.assertEqual(data['status'], 200)
        self.assertTrue(any(item['username'] == spaced.username for item in data['data']))
        row = next(item for item in data['data'] if item['username'] == spaced.username)
        self.assertEqual(row['text'], 'Carol Chen (carolnet)')
