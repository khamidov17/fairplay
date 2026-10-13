import base64
import hmac
import re
import struct
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.shortcuts import get_current_site
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import Resolver404, resolve, reverse
from django.utils.encoding import force_bytes
from requests.exceptions import HTTPError

from judge.models import MiscConfig
from judge.utils.seb import request_matches_seb, seb_launch_context

try:
    import uwsgi
except ImportError:
    uwsgi = None


class ShortCircuitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            callback, args, kwargs = resolve(request.path_info, getattr(request, 'urlconf', None))
        except Resolver404:
            callback, args, kwargs = None, None, None

        if getattr(callback, 'short_circuit_middleware', False):
            return callback(request, *args, **kwargs)
        return self.get_response(request)


class DMOJLoginMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            profile = request.profile = request.user.profile
            if uwsgi:
                uwsgi.set_logvar('username', request.user.username)
                uwsgi.set_logvar('language', request.LANGUAGE_CODE)

            logout_path = reverse('auth_logout')
            login_2fa_path = reverse('login_2fa')
            webauthn_path = reverse('webauthn_assert')
            change_password_path = reverse('password_change')
            change_password_done_path = reverse('password_change_done')
            has_2fa = profile.is_totp_enabled or profile.is_webauthn_enabled
            if (has_2fa and not request.session.get('2fa_passed', False) and
                    request.path not in (login_2fa_path, logout_path, webauthn_path) and
                    not request.path.startswith(settings.STATIC_URL)):
                return HttpResponseRedirect(login_2fa_path + '?next=' + quote(request.get_full_path()))
            elif (request.session.get('password_pwned', False) and
                    request.path not in (change_password_path, change_password_done_path,
                                         login_2fa_path, logout_path) and
                    not request.path.startswith(settings.STATIC_URL)):
                return HttpResponseRedirect(change_password_path + '?next=' + quote(request.get_full_path()))
        else:
            request.profile = None
        return self.get_response(request)


class DMOJImpersonationMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_impersonate:
            if uwsgi:
                uwsgi.set_logvar('username', f'{request.impersonator.username} as {request.user.username}')
            request.no_profile_update = True
            request.profile = request.user.profile
        return self.get_response(request)


# Reversed into the launch route to locate where its variable token begins. Must
# not be a substring of the surrounding path, so it is deliberately unlovely.
_SEB_LAUNCH_SENTINEL = 'seb-launch-token-sentinel-c7f1'


def _seb_launch_prefix():
    """The fixed leading part of the token-authorized ``.seb`` fetch URL.

    That route is the one exempt path that cannot be matched exactly, since its
    final segment is a per-click signed token. Reversing it with a sentinel and
    cutting at the sentinel yields the literal prefix, which stays correct under
    a mounted script prefix (FORCE_SCRIPT_NAME, a reverse-proxy subpath).

    Do not compute this by slicing the first path segment off some other SEB
    route: under a mount at ``/judge/`` that yields ``/judge/``, which matches
    every URL on the site and switches the lockdown off entirely.
    """
    launch = reverse('seb_launch_config', args=(_SEB_LAUNCH_SENTINEL,))
    return launch[:launch.index(_SEB_LAUNCH_SENTINEL)]


def _seb_block_exempt(path, contest):
    # Whitelist paths that must remain reachable for the SEB block page to function
    # (logout, the .seb download itself, the quit landing, and static/media assets).
    # A root-ish prefix (e.g. MEDIA_URL = '/') would exempt every path and
    # disable the lockdown entirely, so only honor real sub-path prefixes.
    try:
        launch_prefix = _seb_launch_prefix()
        exempt_paths = (reverse('auth_logout'), reverse('seb_quit'),
                        reverse('contest_seb_config', args=(contest.key,)))
    except Exception:
        return False
    for prefix in (settings.STATIC_URL, getattr(settings, 'MEDIA_URL', ''), launch_prefix):
        if prefix and prefix != '/' and path.startswith(prefix):
            return True
    return path in exempt_paths


def _ended_window_workspace_redirect(request, contest):
    """Send a GET of the solving page to the assignment once the window ends."""
    if request.method != 'GET':
        return None
    try:
        match = resolve(request.path_info)
    except Resolver404:
        return None
    if match.url_name != 'problem_detail':
        return None
    return HttpResponseRedirect(reverse('contest_view', args=[contest.key]))


class ContestMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        profile = request.profile
        if profile:
            previous = profile.current_contest
            profile.update_contest()
            request.participation = profile.current_contest
            request.in_contest = request.participation is not None
            # Participation just ended: SEB exams go to the quit URL; a GET of
            # the solving page goes to the assignment detail.
            if previous is not None and request.participation is None and previous.ended:
                contest = previous.contest
                if not previous.spectate and contest.seb_required \
                        and not contest.user_bypasses_seb(request.user) \
                        and not _seb_block_exempt(request.path, contest):
                    return HttpResponseRedirect(contest.seb_effective_quit_url or reverse('seb_quit'))
                redirect = _ended_window_workspace_redirect(request, contest)
                if redirect is not None:
                    return redirect
        else:
            request.in_contest = False
            request.participation = None

        if request.in_contest:
            contest = request.participation.contest
            if contest.seb_required and not contest.user_bypasses_seb(request.user) \
                    and not _seb_block_exempt(request.path, contest) \
                    and not request_matches_seb(
                        request, contest.seb_allowed_bek(), contest.seb_allowed_config_keys()):
                return render(request, 'contest/seb_required.html', {
                    'contest': contest,
                    'seb_launch': seb_launch_context(request, contest),
                    'title': 'Safe Exam Browser required',
                }, status=403)

        return self.get_response(request)


class APIMiddleware(object):
    header_pattern = re.compile('^Bearer ([a-zA-Z0-9_-]{48})$')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        full_token = request.headers.get('authorization', '')
        if not full_token:
            return self.get_response(request)

        token = self.header_pattern.match(full_token)
        if not token:
            return HttpResponse('Invalid authorization header', status=400)
        if request.path.startswith(reverse('admin:index')):
            return HttpResponse('Admin inaccessible', status=403)

        try:
            id, secret = struct.unpack('>I32s', base64.urlsafe_b64decode(token.group(1)))
            request.user = User.objects.get(id=id)

            # User hasn't generated a token
            if not request.user.profile.api_token:
                raise HTTPError()

            # Token comparison
            digest = hmac.new(force_bytes(settings.SECRET_KEY), msg=secret, digestmod='sha256').hexdigest()
            if not hmac.compare_digest(digest, request.user.profile.api_token):
                raise HTTPError()

            request._cached_user = request.user
            request.csrf_processing_done = True
            request.session['2fa_passed'] = True
        except (User.DoesNotExist, HTTPError):
            response = HttpResponse('Invalid token')
            response['WWW-Authenticate'] = 'Bearer realm="API"'
            response.status_code = 401
            return response
        return self.get_response(request)


class MiscConfigDict(dict):
    __slots__ = ('language', 'site', 'backing')

    def __init__(self, language='', domain=None):
        self.language = language
        self.site = domain
        self.backing = None
        super().__init__()

    def __missing__(self, key):
        if self.backing is None:
            cache_key = 'misc_config'
            backing = cache.get(cache_key)
            if backing is None:
                backing = dict(MiscConfig.objects.values_list('key', 'value'))
                cache.set(cache_key, backing, 86400)
            self.backing = backing

        keys = ['%s.%s' % (key, self.language), key] if self.language else [key]
        if self.site is not None:
            keys = ['%s:%s' % (self.site, key) for key in keys] + keys

        for attempt in keys:
            result = self.backing.get(attempt)
            if result is not None:
                break
        else:
            result = ''

        self[key] = result
        return result


class MiscConfigMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        domain = get_current_site(request).domain
        request.misc_config = MiscConfigDict(language=request.LANGUAGE_CODE, domain=domain)
        return self.get_response(request)
