import hashlib
import time
from types import SimpleNamespace
from unittest import mock

from django.core import signing
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse

from judge.models import Contest
from judge.utils.seb import BEK_HEADER, SEB_INSTALL_URL, read_seb_launch_token, request_matches_seb, \
    seb_launch_context, sign_seb_launch_token

BEK = hashlib.sha256(b'browser exam key').hexdigest()
CFG = hashlib.sha256(b'config key').hexdigest()


def seb_hash(url, key):
    return hashlib.sha256((url + key).encode('utf-8')).hexdigest()


class RequestMatchesSEBTestCase(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_no_headers(self):
        request = self.factory.get('/page')
        self.assertFalse(request_matches_seb(request, [BEK], [CFG]))

    def test_no_allowed_keys(self):
        request = self.factory.get(
            '/page', HTTP_X_SAFEEXAMBROWSER_REQUESTHASH=seb_hash('http://testserver/page', BEK),
        )
        self.assertFalse(request_matches_seb(request, [], []))

    def test_valid_bek_header(self):
        request = self.factory.get(
            '/page', HTTP_X_SAFEEXAMBROWSER_REQUESTHASH=seb_hash('http://testserver/page', BEK),
        )
        self.assertTrue(request_matches_seb(request, [BEK], [CFG]))

    def test_valid_config_key_header(self):
        request = self.factory.get(
            '/page', HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=seb_hash('http://testserver/page', CFG),
        )
        self.assertTrue(request_matches_seb(request, [BEK], [CFG]))

    def test_wrong_hash(self):
        request = self.factory.get(
            '/page', HTTP_X_SAFEEXAMBROWSER_REQUESTHASH=seb_hash('http://testserver/other', BEK),
        )
        self.assertFalse(request_matches_seb(request, [BEK], [CFG]))

    def test_header_checked_against_matching_key_list_only(self):
        # A hash made with a BEK is not accepted in the Config Key header slot.
        request = self.factory.get(
            '/page', HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=seb_hash('http://testserver/page', BEK),
        )
        self.assertFalse(request_matches_seb(request, [BEK], [CFG]))

    def test_any_key_in_list_matches(self):
        request = self.factory.get(
            '/page', HTTP_X_SAFEEXAMBROWSER_REQUESTHASH=seb_hash('http://testserver/page', BEK),
        )
        self.assertTrue(request_matches_seb(request, ['f' * 64, BEK], []))

    def test_hash_includes_query_string(self):
        # SEB hashes the absolute URL including the query string (only the
        # fragment is stripped).
        request = self.factory.get(
            '/page', {'x': '1'},
            HTTP_X_SAFEEXAMBROWSER_REQUESTHASH=seb_hash('http://testserver/page?x=1', BEK),
        )
        self.assertTrue(request_matches_seb(request, [BEK], []))

        request = self.factory.get(
            '/page', {'x': '1'},
            HTTP_X_SAFEEXAMBROWSER_REQUESTHASH=seb_hash('http://testserver/page', BEK),
        )
        self.assertFalse(request_matches_seb(request, [BEK], []))

    def test_received_hash_case_and_whitespace_insensitive(self):
        request = self.factory.get(
            '/page',
            HTTP_X_SAFEEXAMBROWSER_REQUESTHASH=' %s ' % seb_hash('http://testserver/page', BEK).upper(),
        )
        self.assertTrue(request_matches_seb(request, [BEK], []))

    def test_form_and_query_params_ignored(self):
        # Only the SEB HTTP headers are trusted; request parameters are not.
        valid = seb_hash('http://testserver/page', BEK)
        request = self.factory.post('/page', {'_seb_request_hash': valid})
        self.assertFalse(request_matches_seb(request, [BEK], []))


class SEBMismatchDiagnosticsTestCase(SimpleTestCase):
    """The block page looks identical for every cause; the log must not."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_proxy_scheme_mismatch_is_named_as_such(self):
        # TLS terminated at a proxy: SEB hashed https://, Django rebuilt http://.
        request = self.factory.get('/contest/x')
        seb_url = 'https://testserver/contest/x'
        request.META[BEK_HEADER] = seb_hash(seb_url, BEK)
        with self.assertLogs('judge.seb', level='WARNING') as logs:
            self.assertFalse(request_matches_seb(request, [BEK], []))
        self.assertIn('the keys are CORRECT', logs.output[0])
        self.assertIn('SECURE_PROXY_SSL_HEADER', logs.output[0])
        self.assertIn(seb_url, logs.output[0])

    def test_forwarded_host_mismatch_is_named_as_such(self):
        request = self.factory.get('/contest/x', HTTP_X_FORWARDED_HOST='oj.example.edu')
        seb_url = 'http://oj.example.edu/contest/x'
        request.META[BEK_HEADER] = seb_hash(seb_url, BEK)
        with self.assertLogs('judge.seb', level='WARNING') as logs:
            self.assertFalse(request_matches_seb(request, [BEK], []))
        self.assertIn('the keys are CORRECT', logs.output[0])
        self.assertIn('USE_X_FORWARDED_HOST', logs.output[0])

    def test_stale_keys_are_named_as_such(self):
        # Right URL, wrong key: no URL variant can rescue this one.
        request = self.factory.get('/contest/x')
        request.META[BEK_HEADER] = seb_hash('http://testserver/contest/x', 'f' * 64)
        with self.assertLogs('judge.seb', level='WARNING') as logs:
            self.assertFalse(request_matches_seb(request, [BEK], []))
        self.assertIn('do not belong to the .seb', logs.output[0])
        self.assertIn('changes both keys', logs.output[0])

    def test_missing_headers_are_named_as_such(self):
        with self.assertLogs('judge.seb', level='WARNING') as logs:
            self.assertFalse(request_matches_seb(self.factory.get('/contest/x'), [BEK], []))
        self.assertIn('neither', logs.output[0])

    def test_no_keys_configured_is_named_as_such(self):
        with self.assertLogs('judge.seb', level='WARNING') as logs:
            self.assertFalse(request_matches_seb(self.factory.get('/contest/x'), [], []))
        self.assertIn('no Browser Exam Key and no Config Key', logs.output[0])

    def test_a_valid_request_logs_nothing(self):
        request = self.factory.get('/contest/x')
        request.META[BEK_HEADER] = seb_hash('http://testserver/contest/x', BEK)
        with self.assertNoLogs('judge.seb', level='WARNING'):
            self.assertTrue(request_matches_seb(request, [BEK], []))


class SEBLaunchTokenTestCase(SimpleTestCase):
    def test_round_trip(self):
        contest = Contest(key='demo', id=7)
        token = sign_seb_launch_token(contest, SimpleNamespace(id=13))
        self.assertEqual(read_seb_launch_token(token), {'c': 7, 'u': 13})

    def test_tampered_token_rejected(self):
        contest = Contest(key='demo', id=7)
        token = sign_seb_launch_token(contest, SimpleNamespace(id=13))
        # Flip a character in the middle of the payload, not the padding.
        tampered = token[:10] + ('A' if token[10] != 'A' else 'B') + token[11:]
        with self.assertRaises(signing.BadSignature):
            read_seb_launch_token(tampered)

    def test_garbage_token_rejected(self):
        with self.assertRaises(signing.BadSignature):
            read_seb_launch_token('not a token!!')

    @override_settings(DMOJ_SEB_LAUNCH_TOKEN_EXPIRY_MINUTES=30)
    def test_expired_token_rejected(self):
        contest = Contest(key='demo', id=7)
        # Sign it in the past rather than sleeping out the TTL.
        with mock.patch('django.core.signing.time.time', return_value=time.time() - 31 * 60):
            token = sign_seb_launch_token(contest, SimpleNamespace(id=13))
        with self.assertRaises(signing.SignatureExpired):
            read_seb_launch_token(token)


class SEBLaunchContextTestCase(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, path='/page', secure=False, profile=SimpleNamespace(id=13)):
        # DMOJLoginMiddleware sets request.profile; RequestFactory does not.
        request = self.factory.get(path, secure=secure)
        request.profile = profile
        return request

    def test_without_config_file(self):
        contest = Contest(key='demo', id=7)
        context = seb_launch_context(self._request(), contest)
        self.assertIsNone(context['download_url'])
        self.assertIsNone(context['sebs_url'])
        self.assertIsNone(context['token_url'])
        self.assertEqual(context['quit_url'], '')
        self.assertEqual(context['install_url'], SEB_INSTALL_URL)

    def test_anonymous_gets_no_launch_link(self):
        # No profile means no token to mint, so only the (login-gated) download
        # button is offered.
        contest = Contest(key='demo', id=7)
        contest.seb_config_file.name = 'seb_configs/demo.seb'
        context = seb_launch_context(self._request(profile=None), contest)
        self.assertEqual(context['download_url'], 'http://testserver' + reverse('contest_seb_config', args=('demo',)))
        self.assertIsNone(context['sebs_url'])
        self.assertIsNone(context['token_url'])

    def test_with_config_file_http(self):
        contest = Contest(key='demo', id=7, seb_quit_url='https://example.com/quit')
        contest.seb_config_file.name = 'seb_configs/demo.seb'
        context = seb_launch_context(self._request(), contest)
        self.assertEqual(context['download_url'],
                         'http://testserver' + reverse('contest_seb_config', args=('demo',)))
        self.assertTrue(context['sebs_url'].startswith('seb://testserver/seb/launch/'))
        self.assertEqual(context['quit_url'], 'https://example.com/quit')

    def test_with_config_file_https(self):
        contest = Contest(key='demo', id=7)
        contest.seb_config_file.name = 'seb_configs/demo.seb'
        context = seb_launch_context(self._request(secure=True), contest)
        self.assertEqual(context['download_url'],
                         'https://testserver' + reverse('contest_seb_config', args=('demo',)))
        self.assertTrue(context['sebs_url'].startswith('sebs://testserver/seb/launch/'))
        self.assertEqual(context['token_url'],
                         'https://testserver' + reverse('seb_launch_token', args=('demo',)))

    def test_launch_url_carries_a_usable_token(self):
        contest = Contest(key='demo', id=7)
        contest.seb_config_file.name = 'seb_configs/demo.seb'
        context = seb_launch_context(self._request(secure=True), contest)
        token = context['sebs_url'].rstrip('/').rsplit('/', 1)[-1]
        self.assertEqual(read_seb_launch_token(token), {'c': 7, 'u': 13})
