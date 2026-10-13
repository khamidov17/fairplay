import hashlib
import shutil
import tempfile
import time
from datetime import timedelta
from unittest import mock

from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse, set_script_prefix
from django.utils import timezone

from judge.middleware import _seb_block_exempt, _seb_launch_prefix
from judge.models import Contest, ContestParticipation, SEBConfigPreset
from judge.models.tests.util import CommonDataMixin, create_contest, create_contest_participation, create_user
from judge.utils.seb import sign_seb_launch_token

BEK = hashlib.sha256(b'browser exam key').hexdigest()
CFG = hashlib.sha256(b'config key').hexdigest()

SEB_CONFIG_BYTES = b'<plist>fake seb config</plist>'

PRESET_BEK = hashlib.sha256(b'preset browser exam key').hexdigest()
PRESET_CFG = hashlib.sha256(b'preset config key').hexdigest()

PRESET_CONFIG_BYTES = b'<plist>fake preset seb config</plist>'


def bek_header(path, key=BEK):
    return {'HTTP_X_SAFEEXAMBROWSER_REQUESTHASH':
            hashlib.sha256(('http://testserver%s' % path + key).encode('utf-8')).hexdigest()}


def cfg_header(path, key=CFG):
    return {'HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH':
            hashlib.sha256(('http://testserver%s' % path + key).encode('utf-8')).hexdigest()}


@override_settings(COMPRESS_ENABLED=False, MEDIA_ROOT=tempfile.mkdtemp())
class ContestSEBEnforcementTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.users.update({
            'seb_participant': create_user(username='seb_participant'),
            'seb_joiner': create_user(username='seb_joiner'),
            'seb_editor': create_user(username='seb_editor'),
            'seb_tester': create_user(username='seb_tester'),
            'seb_spectator': create_user(username='seb_spectator'),
            'seb_edit_all': create_user(
                username='seb_edit_all',
                user_permissions=('edit_all_contest',),
            ),
        })

        self.seb_contest = create_contest(
            key='sebexam',
            is_visible=True,
            seb_required=True,
            seb_browser_exam_keys='%s  # Windows 3.7' % BEK,
            seb_config_keys=CFG,
            authors=('seb_editor',),
            testers=('seb_tester',),
            spectators=('seb_spectator',),
        )
        self.seb_contest.seb_config_file.save('conf.seb', ContentFile(SEB_CONFIG_BYTES), save=True)

        self.seb_contest_no_file = create_contest(
            key='sebnofile',
            is_visible=True,
            seb_required=True,
            seb_browser_exam_keys=BEK,
            seb_config_keys=CFG,
        )

        self.plain_contest = create_contest(
            key='plain',
            is_visible=True,
        )

        self.seb_contest_private = create_contest(
            key='sebprivate',
            is_visible=True,
            is_private=True,
            seb_required=True,
            seb_browser_exam_keys=BEK,
            seb_config_keys=CFG,
            private_contestants=('seb_joiner',),
        )
        self.seb_contest_private.seb_config_file.save('priv.seb', ContentFile(SEB_CONFIG_BYTES), save=True)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls._overridden_settings['MEDIA_ROOT'], ignore_errors=True)

    def setUp(self):
        self.client = Client()

    def enter_contest(self, username, contest, **kwargs):
        profile = self.users[username].profile
        participation = create_contest_participation(contest=contest, user=profile, **kwargs)
        profile.current_contest = participation
        profile.save()
        self.client.force_login(self.users[username])
        return participation

    # ------------------------------------------------------------------ #
    # Middleware lockdown                                                 #
    # ------------------------------------------------------------------ #

    def test_blocked_without_seb(self):
        self.enter_contest('seb_participant', self.seb_contest)
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'Safe Exam Browser required', status_code=403)

    def test_allowed_with_valid_bek_header(self):
        self.enter_contest('seb_participant', self.seb_contest)
        # A page inside the exam surface: other pages are locked during an SEB exam.
        path = reverse('contest_view', args=('sebexam',))
        response = self.client.get(path, **bek_header(path))
        self.assertEqual(response.status_code, 200)

    def test_allowed_with_valid_config_key_header(self):
        self.enter_contest('seb_participant', self.seb_contest)
        # A page inside the exam surface: other pages are locked during an SEB exam.
        path = reverse('contest_view', args=('sebexam',))
        response = self.client.get(path, **cfg_header(path))
        self.assertEqual(response.status_code, 200)

    def test_blocked_with_wrong_hash(self):
        self.enter_contest('seb_participant', self.seb_contest)
        path = reverse('home')
        response = self.client.get(path, **bek_header(path, key='f' * 64))
        self.assertEqual(response.status_code, 403)

    def test_hash_covers_query_string(self):
        self.enter_contest('seb_participant', self.seb_contest)
        # A page inside the exam surface: other pages are locked during an SEB exam.
        path = reverse('contest_view', args=('sebexam',))
        # Hash over URL including the query string is accepted...
        response = self.client.get(path + '?foo=bar', **bek_header(path + '?foo=bar'))
        self.assertEqual(response.status_code, 200)
        # ...hash over the bare path is not.
        response = self.client.get(path + '?foo=bar', **bek_header(path))
        self.assertEqual(response.status_code, 403)

    def test_exempt_paths_reachable_while_blocked(self):
        self.enter_contest('seb_participant', self.seb_contest)
        self.assertEqual(self.client.get(reverse('seb_quit')).status_code, 200)
        self.assertNotEqual(self.client.get(reverse('auth_logout')).status_code, 403)
        # The block page links to the .seb download; it must not itself be blocked.
        response = self.client.get(reverse('contest_seb_config', args=('sebexam',)))
        self.assertEqual(response.status_code, 200)

    def test_bypass_users_not_blocked(self):
        for username in ('seb_editor', 'seb_tester', 'seb_spectator', 'seb_edit_all'):
            with self.subTest(username=username):
                self.enter_contest(username, self.seb_contest)
                response = self.client.get(reverse('home'))
                self.assertEqual(response.status_code, 200)

    def test_plain_contest_not_blocked(self):
        self.enter_contest('seb_participant', self.plain_contest)
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)

    # ------------------------------------------------------------------ #
    # Joining                                                             #
    # ------------------------------------------------------------------ #

    def test_join_blocked_without_seb(self):
        self.client.force_login(self.users['seb_joiner'])
        path = reverse('contest_join', args=('sebexam',))
        for response in (self.client.get(path), self.client.post(path)):
            self.assertContains(response, 'Safe Exam Browser required', status_code=403)
        self.assertFalse(ContestParticipation.objects.filter(
            contest=self.seb_contest, user=self.users['seb_joiner'].profile).exists())

    def test_join_with_valid_header(self):
        self.client.force_login(self.users['seb_joiner'])
        path = reverse('contest_join', args=('sebexam',))
        response = self.client.post(path, **bek_header(path))
        self.assertEqual(response.status_code, 302)
        profile = self.users['seb_joiner'].profile
        profile.refresh_from_db()
        self.assertIsNotNone(profile.current_contest)
        self.assertEqual(profile.current_contest.contest, self.seb_contest)

    def test_bypass_user_joins_without_seb(self):
        self.client.force_login(self.users['seb_tester'])
        path = reverse('contest_join', args=('sebexam',))
        response = self.client.post(path)
        self.assertEqual(response.status_code, 302)

    def test_join_plain_contest_without_seb(self):
        self.client.force_login(self.users['seb_joiner'])
        response = self.client.post(reverse('contest_join', args=('plain',)))
        self.assertEqual(response.status_code, 302)

    # ------------------------------------------------------------------ #
    # Leaving                                                             #
    # ------------------------------------------------------------------ #

    def test_leave_redirects_to_quit_page(self):
        self.enter_contest('seb_participant', self.seb_contest)
        path = reverse('contest_leave', args=('sebexam',))
        response = self.client.post(path, **bek_header(path))
        self.assertRedirects(response, reverse('seb_quit'))
        # A running SEB exam locks the site, so Leave (hand in) keeps the student in it until it
        # ends; otherwise leaving and rejoining would switch the lock off and on at will.
        profile = self.users['seb_participant'].profile
        profile.refresh_from_db()
        self.assertIsNotNone(profile.current_contest)

    def test_leave_redirects_to_custom_quit_url(self):
        self.seb_contest.seb_quit_url = 'https://example.com/done'
        self.seb_contest.save()
        self.enter_contest('seb_participant', self.seb_contest)
        path = reverse('contest_leave', args=('sebexam',))
        response = self.client.post(path, **bek_header(path))
        self.assertRedirects(response, 'https://example.com/done', fetch_redirect_response=False)

    def test_spectator_leave_skips_quit_page(self):
        self.enter_contest('seb_spectator', self.seb_contest, virtual=ContestParticipation.SPECTATE)
        response = self.client.post(reverse('contest_leave', args=('sebexam',)))
        self.assertRedirects(response, reverse('contest_view', args=('sebexam',)),
                             fetch_redirect_response=False)

    def test_leave_plain_contest_redirects_to_contest(self):
        self.enter_contest('seb_participant', self.plain_contest)
        response = self.client.post(reverse('contest_leave', args=('plain',)))
        self.assertRedirects(response, reverse('contest_view', args=('plain',)),
                             fetch_redirect_response=False)

    def test_seb_leave_button_says_exit_seb_not_submit(self):
        self.enter_contest('seb_participant', self.seb_contest)
        path = reverse('contest_view', args=('sebexam',))
        response = self.client.get(path, **bek_header(path))
        self.assertContains(response, 'Exit SEB')
        self.assertContains(response, 'data-seb-exit="1"')
        self.assertContains(
            response,
            'You must press Submit on every problem. Unsubmitted answers will not count toward your grade.',
        )
        self.assertNotContains(response, 'Only answers you have already submitted will be kept.')
        self.assertNotContains(
            response,
            'When you leave, answers you have not submitted yet will be submitted automatically.',
        )
        self.assertNotContains(response, 'Submit &amp; Exit SEB')
        self.assertNotContains(response, 'Submit your work and exit Safe Exam Browser')

    # ------------------------------------------------------------------ #
    # Natural contest end                                                  #
    # ------------------------------------------------------------------ #

    def end_contest(self, contest):
        contest.end_time = timezone.now() - timezone.timedelta(minutes=1)
        contest.save()

    def test_natural_end_redirects_to_quit_page(self):
        self.enter_contest('seb_participant', self.seb_contest)
        self.end_contest(self.seb_contest)
        response = self.client.get(reverse('home'))
        self.assertRedirects(response, reverse('seb_quit'))
        profile = self.users['seb_participant'].profile
        profile.refresh_from_db()
        self.assertIsNone(profile.current_contest)

    def test_natural_end_redirects_to_custom_quit_url(self):
        self.seb_contest.seb_quit_url = 'https://example.com/done'
        self.seb_contest.save()
        self.enter_contest('seb_participant', self.seb_contest)
        self.end_contest(self.seb_contest)
        response = self.client.get(reverse('home'))
        self.assertRedirects(response, 'https://example.com/done', fetch_redirect_response=False)

    def test_natural_end_redirect_fires_only_once(self):
        self.enter_contest('seb_participant', self.seb_contest)
        self.end_contest(self.seb_contest)
        self.assertEqual(self.client.get(reverse('home')).status_code, 302)
        self.assertEqual(self.client.get(reverse('home')).status_code, 200)

    def test_natural_end_no_redirect_on_exempt_path(self):
        self.enter_contest('seb_participant', self.seb_contest)
        self.end_contest(self.seb_contest)
        response = self.client.get(reverse('seb_quit'))
        self.assertContains(response, 'Exam finished')

    def test_natural_end_spectator_not_redirected(self):
        self.enter_contest('seb_spectator', self.seb_contest, virtual=ContestParticipation.SPECTATE)
        self.end_contest(self.seb_contest)
        self.assertEqual(self.client.get(reverse('home')).status_code, 200)

    def test_natural_end_bypass_user_not_redirected(self):
        self.enter_contest('seb_tester', self.seb_contest)
        self.end_contest(self.seb_contest)
        self.assertEqual(self.client.get(reverse('home')).status_code, 200)

    def test_natural_end_plain_contest_not_redirected(self):
        self.enter_contest('seb_participant', self.plain_contest)
        self.end_contest(self.plain_contest)
        self.assertEqual(self.client.get(reverse('home')).status_code, 200)

    # ------------------------------------------------------------------ #
    # .seb config download                                                #
    # ------------------------------------------------------------------ #

    def test_config_download(self):
        self.client.force_login(self.users['seb_joiner'])
        response = self.client.get(reverse('contest_seb_config', args=('sebexam',)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/seb')
        self.assertIn('sebexam.seb', response['Content-Disposition'])
        self.assertEqual(b''.join(response.streaming_content), SEB_CONFIG_BYTES)

    def test_config_download_requires_login(self):
        response = self.client.get(reverse('contest_seb_config', args=('sebexam',)))
        self.assertEqual(response.status_code, 302)

    def test_config_download_missing_file(self):
        # ContestMixin.dispatch converts Http404 into a generic "No such contest" page.
        self.client.force_login(self.users['seb_joiner'])
        response = self.client.get(reverse('contest_seb_config', args=('sebnofile',)))
        self.assertContains(response, 'No such contest')

    def test_config_download_when_seb_not_required(self):
        self.client.force_login(self.users['seb_joiner'])
        response = self.client.get(reverse('contest_seb_config', args=('plain',)))
        self.assertContains(response, 'No such contest')

    # ------------------------------------------------------------------ #
    # Token-authorized launch (what the SEB client itself fetches)         #
    # ------------------------------------------------------------------ #

    def launch_url(self, contest):
        return reverse('seb_launch_config',
                       args=(sign_seb_launch_token(contest, self.users['seb_joiner'].profile),))

    def test_launch_config_served_without_authentication(self):
        # The whole point: SEB fetches this with no session, and must get bytes
        # rather than a redirect to the login page.
        response = self.client.get(self.launch_url(self.seb_contest))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/seb')
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertIn('sebexam.seb', response['Content-Disposition'])
        self.assertEqual(b''.join(response.streaming_content), SEB_CONFIG_BYTES)

    def test_launch_config_never_redirects(self):
        # A redirect is what puts SEB into its unconfigured fallback window, so
        # no failure mode of this view may 302.
        for label, url in (
            ('valid', self.launch_url(self.seb_contest)),
            ('tampered', reverse('seb_launch_config', args=('bogus-token',))),
        ):
            with self.subTest(label):
                self.assertNotEqual(self.client.get(url).status_code, 302)

    def test_launch_config_private_contest_served(self):
        # ContestMixin.access_check would 403 an anonymous visitor here; the
        # token is the authorization instead.
        response = self.client.get(self.launch_url(self.seb_contest_private))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/seb')

    def test_launch_config_tampered_token(self):
        response = self.client.get(reverse('seb_launch_config', args=('not-a-real-token',)))
        self.assertContains(response, 'Launch link expired', status_code=400)

    @override_settings(DMOJ_SEB_LAUNCH_TOKEN_EXPIRY_MINUTES=30)
    def test_launch_config_expired_token(self):
        with mock.patch('django.core.signing.time.time', return_value=time.time() - 31 * 60):
            url = self.launch_url(self.seb_contest)
        self.assertContains(self.client.get(url), 'Launch link expired', status_code=400)

    def test_launch_config_missing_file(self):
        response = self.client.get(self.launch_url(self.seb_contest_no_file))
        self.assertEqual(response.status_code, 404)

    def test_launch_config_when_seb_not_required(self):
        response = self.client.get(self.launch_url(self.plain_contest))
        self.assertEqual(response.status_code, 404)

    def test_launch_config_reachable_while_blocked(self):
        # Exempted by prefix, so a student already parked on the block page can
        # still hand SEB its configuration.
        contest = self.seb_contest
        url = self.launch_url(contest)
        self.enter_contest('seb_participant', contest)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_launch_token_endpoint_mints_a_working_url(self):
        self.client.force_login(self.users['seb_joiner'])
        response = self.client.get(reverse('seb_launch_token', args=('sebexam',)))
        self.assertEqual(response.status_code, 200)
        url = response.json()['url']
        self.assertTrue(url.startswith('seb://testserver/seb/launch/'))
        # Follow the freshly minted link the way SEB would.
        self.client.logout()
        fetched = self.client.get(url.replace('seb://testserver', '', 1))
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched['Content-Type'], 'application/seb')

    def test_launch_token_endpoint_requires_login(self):
        self.assertEqual(self.client.get(reverse('seb_launch_token', args=('sebexam',))).status_code, 302)

    def test_exempt_prefix_does_not_widen_under_a_mounted_script_prefix(self):
        """The launch route's prefix must stay the launch route's prefix.

        Deriving it by slicing the first path segment off a SEB route yields the
        mount root under FORCE_SCRIPT_NAME / a reverse-proxy subpath, which
        prefix-matches every URL and turns the lockdown off entirely.
        """
        set_script_prefix('/judge/')
        try:
            self.assertEqual(_seb_launch_prefix(), '/judge/seb/launch/')
            for path in ('/judge/contest/sebexam/join', '/judge/problem/x', '/judge/', '/judge/user/y'):
                with self.subTest(path=path):
                    self.assertFalse(_seb_block_exempt(path, self.seb_contest))
            self.assertTrue(_seb_block_exempt('/judge/seb/launch/sometoken/', self.seb_contest))
        finally:
            set_script_prefix('/')

    def test_exempt_prefix_covers_only_the_launch_route(self):
        self.assertEqual(_seb_launch_prefix(), '/seb/launch/')
        self.assertTrue(_seb_block_exempt('/seb/launch/sometoken/', self.seb_contest))
        # Sibling /seb/ routes are staff-only and were never prefix-exempt.
        self.assertFalse(_seb_block_exempt('/seb/presets/1/config/', self.seb_contest))
        self.assertFalse(_seb_block_exempt('/contest/sebexam/join', self.seb_contest))

    # ------------------------------------------------------------------ #
    # Quit landing page                                                   #
    # ------------------------------------------------------------------ #

    def test_quit_page_anonymous(self):
        response = self.client.get(reverse('seb_quit'))
        self.assertContains(response, 'Exam finished')


@override_settings(COMPRESS_ENABLED=False, MEDIA_ROOT=tempfile.mkdtemp())
class ContestSEBPresetTestCase(CommonDataMixin, TestCase):
    """A contest carrying no SEB config of its own, inheriting a site preset."""

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.users['preset_student'] = create_user(username='preset_student')
        self.users['preset_staff'] = create_user(
            username='preset_staff', user_permissions=('contest_seb_requirement',))

        self.preset = SEBConfigPreset(
            name='Site default',
            browser_exam_keys=PRESET_BEK,
            config_keys=PRESET_CFG,
            quit_url='https://example.com/quit',
            is_default=True,
        )
        self.preset.config_file.save('default.seb', ContentFile(PRESET_CONFIG_BYTES), save=False)
        self.preset.save()

        self.preset_contest = create_contest(
            key='presetexam',
            is_visible=True,
            seb_required=True,
            seb_preset=self.preset,
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls._overridden_settings['MEDIA_ROOT'], ignore_errors=True)

    def setUp(self):
        self.client = Client()

    def enter_contest(self, username, contest):
        profile = self.users[username].profile
        profile.current_contest = create_contest_participation(contest=contest, user=profile)
        profile.save()
        self.client.force_login(self.users[username])

    def test_blocked_without_seb(self):
        self.enter_contest('preset_student', self.preset_contest)
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'Safe Exam Browser required', status_code=403)

    def test_allowed_with_preset_bek_header(self):
        self.enter_contest('preset_student', self.preset_contest)
        path = reverse('contest_view', args=('presetexam',))
        response = self.client.get(path, **bek_header(path, PRESET_BEK))
        self.assertEqual(response.status_code, 200)

    def test_allowed_with_preset_config_key_header(self):
        self.enter_contest('preset_student', self.preset_contest)
        path = reverse('contest_view', args=('presetexam',))
        response = self.client.get(path, **cfg_header(path, PRESET_CFG))
        self.assertEqual(response.status_code, 200)

    def test_config_download_serves_preset_file(self):
        self.client.force_login(self.users['preset_student'])
        response = self.client.get(reverse('contest_seb_config', args=('presetexam',)))
        self.assertEqual(response.status_code, 200)
        # Served under the contest's own name even though the bytes are the preset's.
        self.assertIn('presetexam.seb', response['Content-Disposition'])
        self.assertEqual(b''.join(response.streaming_content), PRESET_CONFIG_BYTES)

    def test_preset_file_stored_under_generated_key(self):
        # Presets live in the object store on an opaque key, like submissions.
        self.assertRegex(self.preset.config_file.name,
                         r'^seb_configs/\d{4}/\d{2}/presets/[0-9a-f]{32}\.seb$')

    # ------------------------------------------------------------------ #
    # Preset preview download (editor-facing)                             #
    # ------------------------------------------------------------------ #

    def test_preset_preview_requires_login(self):
        response = self.client.get(reverse('seb_preset_config', args=(self.preset.id,)))
        self.assertEqual(response.status_code, 302)

    def test_preset_preview_denied_without_permission(self):
        self.client.force_login(self.users['preset_student'])
        response = self.client.get(reverse('seb_preset_config', args=(self.preset.id,)))
        self.assertEqual(response.status_code, 403)

    def test_preset_preview_denied_with_permission_but_not_editing_a_contest_using_it(self):
        # SEB-1: the permission alone does not hand out every preset's .seb.
        self.client.force_login(self.users['preset_staff'])
        response = self.client.get(reverse('seb_preset_config', args=(self.preset.id,)))
        self.assertEqual(response.status_code, 403)

    def test_preset_preview_denied_for_editor_without_permission(self):
        self.preset_contest.authors.add(self.users['preset_student'].profile)
        self.client.force_login(self.users['preset_student'])
        response = self.client.get(reverse('seb_preset_config', args=(self.preset.id,)) + '?contest=presetexam')
        self.assertEqual(response.status_code, 403)

    def test_preset_preview_denied_for_other_contest_key(self):
        other = create_contest(key='presetother', is_visible=True)
        other.authors.add(self.users['preset_staff'].profile)
        self.client.force_login(self.users['preset_staff'])
        response = self.client.get(reverse('seb_preset_config', args=(self.preset.id,)) + '?contest=presetother')
        self.assertEqual(response.status_code, 403)

    def test_preset_preview_allowed_with_permission(self):
        # Permission AND editing a contest that uses this preset, which no other exam shares.
        own = SEBConfigPreset(name='Own exam', config_keys=PRESET_CFG, is_default=False)
        own.config_file.save('own.seb', ContentFile(PRESET_CONFIG_BYTES), save=False)
        own.save()
        create_contest(key='ownpresetexam', is_visible=True, seb_required=True, seb_preset=own,
                       authors=('preset_staff',))
        self.client.force_login(self.users['preset_staff'])
        response = self.client.get(reverse('seb_preset_config', args=(own.id,)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), PRESET_CONFIG_BYTES)

    def test_site_default_preview_denied_to_editor(self):
        # SEB-1: the site default is shared by every exam that uses it, so editing one of them
        # (any instructor can tick "use default" on a draft) does not hand out its file.
        self.preset_contest.authors.add(self.users['preset_staff'].profile)
        self.client.force_login(self.users['preset_staff'])
        response = self.client.get(reverse('seb_preset_config', args=(self.preset.id,)))
        self.assertEqual(response.status_code, 403)
        # Nor through the contest's own .seb download outside the student rule (here: weeks
        # before the start, which is what a fresh draft looks like).
        Contest.objects.filter(id=self.preset_contest.id).update(
            start_time=timezone.now() + timedelta(days=30), end_time=timezone.now() + timedelta(days=31))
        response = self.client.get(reverse('contest_seb_config', args=(self.preset_contest.key,)))
        self.assertEqual(response.status_code, 403)
        # Nor as "anyone on the roster" of their own running public draft (review follow-up).
        Contest.objects.filter(id=self.preset_contest.id).update(
            start_time=timezone.now() - timedelta(minutes=5), end_time=timezone.now() + timedelta(days=1))
        response = self.client.get(reverse('contest_seb_config', args=(self.preset_contest.key,)))
        self.assertEqual(response.status_code, 403)


@override_settings(COMPRESS_ENABLED=False, MEDIA_ROOT=tempfile.mkdtemp(), DMOJ_SEB_CONFIG_DOWNLOAD_WINDOW_MINUTES=30)
class SEBConfigDownloadGateTestCase(CommonDataMixin, TestCase):
    """SEB-1: the .seb goes only to the roster, and only shortly before or during the exam."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.users.update({
            'gate_student': create_user(username='gate_student'),
            'gate_outsider': create_user(username='gate_outsider'),
            'gate_editor': create_user(username='gate_editor'),
        })
        now = timezone.now()
        cls.contest = create_contest(
            key='sebgate', is_visible=True, is_private=True,
            start_time=now + timezone.timedelta(hours=3),
            end_time=now + timezone.timedelta(hours=5),
            seb_required=True, seb_browser_exam_keys=BEK, seb_config_keys=CFG,
            private_contestants=('gate_student',),
            # Can see the contest (scoreboard viewer) but is not on the roster.
            view_contest_scoreboard=('gate_outsider',),
            authors=('gate_editor',),
        )
        cls.contest.seb_config_file.save('gate.seb', ContentFile(SEB_CONFIG_BYTES), save=True)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls._overridden_settings['MEDIA_ROOT'], ignore_errors=True)

    def _move_start(self, delta):
        now = timezone.now()
        Contest.objects.filter(id=self.contest.id).update(start_time=now + delta, end_time=now + delta +
                                                          timezone.timedelta(hours=2))

    def _download(self, username):
        self.client.force_login(self.users[username])
        return self.client.get(reverse('contest_seb_config', args=('sebgate',)))

    def _launch_fetch(self, username):
        self.client.logout()
        url = reverse('seb_launch_config', args=(sign_seb_launch_token(self.contest, self.users[username].profile),))
        return self.client.get(url)

    def test_student_cannot_download_hours_before_start(self):
        self.assertEqual(self._download('gate_student').status_code, 403)
        self.assertEqual(self._launch_fetch('gate_student').status_code, 400)
        self.client.force_login(self.users['gate_student'])
        self.assertEqual(self.client.get(reverse('seb_launch_token', args=('sebgate',))).status_code, 403)

    def test_student_can_download_within_window_before_start(self):
        self._move_start(timezone.timedelta(minutes=20))
        response = self._download('gate_student')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), SEB_CONFIG_BYTES)
        self.assertEqual(self._launch_fetch('gate_student').status_code, 200)

    def test_student_can_download_while_running(self):
        self._move_start(-timezone.timedelta(minutes=5))
        self.assertEqual(self._download('gate_student').status_code, 200)

    def test_nobody_but_editors_after_the_end(self):
        self._move_start(-timezone.timedelta(hours=3))
        self.assertEqual(self._download('gate_student').status_code, 403)
        self.assertEqual(self._download('gate_editor').status_code, 200)

    def test_viewer_not_on_roster_denied_even_during_exam(self):
        self._move_start(-timezone.timedelta(minutes=5))
        self.assertEqual(self._download('gate_outsider').status_code, 403)
        self.assertEqual(self._launch_fetch('gate_outsider').status_code, 400)

    def test_editor_always_allowed(self):
        self.assertEqual(self._download('gate_editor').status_code, 200)

    @override_settings(DMOJ_SEB_CONFIG_DOWNLOAD_WINDOW_MINUTES=240)
    def test_window_is_a_setting(self):
        self.assertEqual(self._download('gate_student').status_code, 200)

    def test_contest_page_hides_links_before_window(self):
        self.client.force_login(self.users['gate_student'])
        response = self.client.get(reverse('contest_view', args=('sebgate',)))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['seb_launch']['download_url'])
        self.assertIsNone(response.context['seb_launch']['sebs_url'])


@override_settings(COMPRESS_ENABLED=False)
class SEBRequireBrowserExamKeyViewTestCase(CommonDataMixin, TestCase):
    """SEB-1: the per-contest "require Browser Exam Key header" option."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.users['bek_student'] = create_user(username='bek_student')
        cls.contest = create_contest(
            key='sebbekonly', is_visible=True, seb_required=True,
            seb_browser_exam_keys=BEK, seb_config_keys=CFG, seb_require_browser_exam_key=True,
        )

    def setUp(self):
        profile = self.users['bek_student'].profile
        profile.current_contest = create_contest_participation(contest=self.contest, user=profile)
        profile.save()
        self.client.force_login(self.users['bek_student'])

    def test_config_key_alone_blocked(self):
        path = reverse('contest_view', args=('sebbekonly',))
        response = self.client.get(path, **cfg_header(path))
        self.assertContains(response, 'Safe Exam Browser required', status_code=403)

    def test_browser_exam_key_allowed(self):
        path = reverse('contest_view', args=('sebbekonly',))
        self.assertEqual(self.client.get(path, **bek_header(path)).status_code, 200)
