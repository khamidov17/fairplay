"""CA-7: NetID SSO login and local-registration squatting."""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from judge.models.tests.util import CommonDataMixin, create_user
from judge.views.register import CustomRegistrationForm


def _sso_response(uid, eppn):
    response = mock.Mock(status_code=200)
    response.json.return_value = {'success': True, 'user_info': {
        'uid': uid, 'eppn': eppn, 'givenName': 'Given', 'sn': 'Sur',
    }}
    return response


@override_settings(AUTH_HOSTS={'Duke-netid': 'https://sso.example.test'})
class NetidLoginTest(CommonDataMixin, TestCase):
    def _login(self, uid, eppn, token='tok&en=1'):
        with mock.patch('requests.get', return_value=_sso_response(uid, eppn)) as get:
            self.client.get(reverse('login_netid'), {'token': token})
        return get

    def _logged_in_as(self):
        user_id = self.client.session.get('_auth_user_id')
        return User.objects.get(id=user_id).username if user_id else None

    def test_token_check_verifies_tls_and_encodes_token(self):
        get = self._login('ab123', 'ab123@example.edu')
        args, kwargs = get.call_args
        self.assertTrue(kwargs['verify'])
        self.assertEqual(kwargs['params'], {'token': 'tok&en=1'})
        self.assertNotIn('tok&en', args[0])

    def test_first_login_creates_sso_account(self):
        self._login('cd456', 'cd456@example.edu')
        self.assertEqual(self._logged_in_as(), 'cd456')
        self.assertFalse(User.objects.get(username='cd456').has_usable_password())

    def test_squatted_local_account_is_not_bound(self):
        squatter = create_user(username='ef789', email='squatter@evil.example')
        squatter.set_password('known-password')
        squatter.save()
        self._login('ef789', 'ef789@example.edu')
        self.assertIsNone(self._logged_in_as())

    def test_imported_account_without_password_is_bound(self):
        imported = create_user(username='gh012', email='')
        imported.set_unusable_password()
        imported.save()
        self._login('gh012', 'gh012@example.edu')
        self.assertEqual(self._logged_in_as(), 'gh012')

    def test_local_account_with_verified_matching_email_is_bound(self):
        local = create_user(username='ij345', email='IJ345@example.edu')
        local.set_password('pw-for-local')
        local.save()
        self._login('ij345', 'ij345@example.edu')
        self.assertEqual(self._logged_in_as(), 'ij345')

    def test_disabled_account_cannot_sso_login(self):
        banned = create_user(username='kl678', is_active=False)
        banned.set_unusable_password()
        banned.save()
        self._login('kl678', 'kl678@example.edu')
        self.assertIsNone(self._logged_in_as())


class LocalRegistrationTest(CommonDataMixin, TestCase):
    def _form(self, username):
        return CustomRegistrationForm(data={
            'username': username, 'email': '%s@mail.example' % username,
            'password1': 'Very-long-pass-123', 'password2': 'Very-long-pass-123',
            'timezone': 'UTC', 'language': 1,
        })

    def test_netid_shaped_username_is_reserved(self):
        form = self._form('ab123')
        self.assertFalse(form.is_valid())
        self.assertIn('username', form.errors)

    def test_ordinary_username_is_allowed(self):
        form = self._form('alice_wonder')
        form.is_valid()
        self.assertNotIn('username', form.errors)

    @override_settings(REGISTRATION_OPEN=False)
    def test_registration_can_be_switched_off(self):
        response = self.client.get(reverse('registration_register'))
        self.assertRedirects(response, reverse('registration_disallowed'), fetch_redirect_response=False)
