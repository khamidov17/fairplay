"""Tests for the pwned-password forced change that backs the CSV import's default
password: a login with a known-compromised password must be forced through a change."""
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from judge.models import Profile
from judge.utils.pwned import PwnedPasswordsValidator


class PwnedPasswordForcedChangeTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='imported', password='123456')
        Profile.objects.get_or_create(user=cls.user)

    def test_validator_falls_back_to_common_list_when_api_unavailable(self):
        with mock.patch('judge.utils.pwned._get_pwned', return_value=None):
            with self.assertRaises(ValidationError):
                PwnedPasswordsValidator().validate('123456')

    def test_login_with_pwned_password_redirects_to_change(self):
        with mock.patch('judge.utils.pwned.pwned_password', return_value=12345):
            response = self.client.post(reverse('auth_login'), {
                'username': 'imported', 'password': '123456',
            })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.client.session['password_pwned'])

        response = self.client.get('/problems/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(reverse('password_change')))

    def test_clean_password_not_redirected(self):
        with mock.patch('judge.utils.pwned.pwned_password', return_value=0):
            self.client.post(reverse('auth_login'), {
                'username': 'imported', 'password': '123456',
            })
        self.assertFalse(self.client.session['password_pwned'])
