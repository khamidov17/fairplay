from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse
from impersonate.helpers import check_allow_impersonate, users_impersonable

from judge.models import Class
from judge.models.tests.util import CommonDataMixin, create_organization, create_user


class ImpersonateURLTestCase(SimpleTestCase):
    """The impersonate include must not be shadowed by a later catch-all pattern.

    dmoj/urls.py appends the DEBUG media fallback via django.conf.urls.static.static(),
    which compiles to ^<prefix>(?P<path>.*)$. With a root-ish MEDIA_URL that becomes a
    catch-all, and anything registered after it resolves to django.views.static.serve.
    """

    def test_impersonate_start_reverses_with_trailing_slash(self):
        self.assertEqual(reverse('impersonate-start', args=(10,)), '/impersonate/10/')

    def test_impersonate_urls_resolve_to_the_impersonate_app(self):
        # The views are wrapped by allowed_user_required, which does not use
        # functools.wraps, so match on url_name rather than the function itself.
        for path, url_name in (
            ('/impersonate/10/', 'impersonate-start'),
            ('/impersonate/stop/', 'impersonate-stop'),
            ('/impersonate/list/', 'impersonate-list'),
        ):
            with self.subTest(path=path):
                match = resolve(path)
                self.assertEqual(match.url_name, url_name)
                self.assertTrue(match.func.__module__.startswith('impersonate.'))


@override_settings(COMPRESS_ENABLED=False)
class ImpersonatePermissionTestCase(CommonDataMixin, TestCase):
    """django-impersonate reads CUSTOM_ALLOW/CUSTOM_USER_QUERYSET from the IMPERSONATE
    dict only. If they are not wired, it silently falls back to letting any staff user
    impersonate any non-superuser, and judge/impersonation.py never runs.
    """

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.users.update({
            'imp_class_admin': create_user(
                username='imp_class_admin',
                user_permissions=('impersonate_class',),
            ),
            'imp_student': create_user(username='imp_student'),
            'imp_outsider': create_user(username='imp_outsider'),
            'imp_staff_student': create_user(username='imp_staff_student', is_staff=True),
            'imp_plain_staff': create_user(username='imp_plain_staff', is_staff=True),
        })

        self.organizations['imp_org'] = create_organization(name='imp_org')
        self.imp_class = Class.objects.create(
            organization=self.organizations['imp_org'],
            name='imp_class',
            slug='imp-class',
        )
        self.imp_class.admins.set([self.users['imp_class_admin'].profile])
        self.imp_class.members.set([
            self.users['imp_student'].profile,
            self.users['imp_staff_student'].profile,
        ])

    def setUp(self):
        self.client = Client()
        self.factory = RequestFactory()

    def request_for(self, username):
        request = self.factory.get('/')
        request.user = self.users[username]
        return request

    def test_custom_allow_is_wired(self):
        for username, expected in (
            ('superuser', True),
            ('imp_class_admin', True),
            # Staff with none of the judge.impersonate_* permissions must be refused.
            # This is what django-impersonate's default allows, and what the custom
            # CUSTOM_ALLOW callback exists to prevent.
            ('imp_plain_staff', False),
            ('normal', False),
        ):
            with self.subTest(username=username):
                self.assertIs(check_allow_impersonate(self.request_for(username)), expected)

    def test_custom_user_queryset_is_scoped_to_administered_class(self):
        impersonable = users_impersonable(self.request_for('imp_class_admin'))
        self.assertEqual(
            set(impersonable.values_list('username', flat=True)),
            {'imp_student'},
            msg='Class admins should only reach plain members of classes they administer.',
        )

    def test_view_refuses_staff_without_impersonate_permission(self):
        self.client.force_login(self.users['imp_plain_staff'])
        response = self.client.get(reverse('impersonate-start', args=(self.users['imp_student'].id,)))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('_impersonate', self.client.session)

    def test_view_refuses_target_outside_administered_class(self):
        self.client.force_login(self.users['imp_class_admin'])
        response = self.client.get(reverse('impersonate-start', args=(self.users['imp_outsider'].id,)))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('_impersonate', self.client.session)

    def test_view_allows_class_admin_to_impersonate_member(self):
        self.client.force_login(self.users['imp_class_admin'])
        response = self.client.get(reverse('impersonate-start', args=(self.users['imp_student'].id,)))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session['_impersonate'], self.users['imp_student'].id)

    def test_impersonation_start_and_stop_are_logged(self):
        """CA-5: who impersonated whom and when is recorded."""
        from django.conf import settings
        from impersonate.models import ImpersonationLog
        self.assertFalse(settings.IMPERSONATE.get('DISABLE_LOGGING'))
        self.client.force_login(self.users['imp_class_admin'])
        self.client.get(reverse('impersonate-start', args=(self.users['imp_student'].id,)))
        log = ImpersonationLog.objects.get()
        self.assertEqual(log.impersonator, self.users['imp_class_admin'])
        self.assertEqual(log.impersonating, self.users['imp_student'])
        self.assertIsNotNone(log.session_started_at)
        self.assertIsNone(log.session_ended_at)
        self.client.get(reverse('impersonate-stop'))
        log.refresh_from_db()
        self.assertIsNotNone(log.session_ended_at)
