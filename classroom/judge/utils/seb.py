import base64
import binascii
import hashlib
import hmac
import logging
import re
from urllib.parse import urlparse, urlunparse

from django.conf import settings
from django.core import signing
from django.urls import reverse

# WSGI normalizes header names: X-SafeExamBrowser-RequestHash -> HTTP_X_SAFEEXAMBROWSER_REQUESTHASH
BEK_HEADER = 'HTTP_X_SAFEEXAMBROWSER_REQUESTHASH'
CFG_HEADER = 'HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH'

SEB_INSTALL_URL = 'https://safeexambrowser.org/download_en.html'

logger = logging.getLogger('judge.seb')

# Both Browser Exam Keys and Config Keys are SHA-256 hashes in lowercase hex.
SEB_KEY_RE = re.compile('[0-9a-f]{64}')

# Namespaces the launch-token signature so a token minted here can never be
# replayed against another TimestampSigner use in the codebase.
SEB_LAUNCH_TOKEN_SALT = 'judge.seb.launch'


def parse_seb_keys(text):
    """Split a key textarea into hashes: one per line, trailing comments dropped."""
    out = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        token = line.split()[0].lower()
        if token:
            out.append(token)
    return out


def first_invalid_seb_key(text):
    """Return the first parsed key that is not a 64-char hex hash, else ``None``."""
    for key in parse_seb_keys(text):
        if not SEB_KEY_RE.fullmatch(key):
            return key
    return None


def _sha256_hex(absolute_url, key):
    return hashlib.sha256((absolute_url + key).encode('utf-8')).hexdigest()


def _abbrev(digest):
    """First 12 chars: enough to compare two hashes by eye, useless for replay."""
    return (digest or '')[:12]


def seb_url_variants(url, extra_hosts=()):
    """URLs a client might have hashed that differ from ``url``.

    A mismatch has two very different causes with one symptom: the configured key
    hashes are stale, or the URL being hashed is not the URL the client
    requested. Re-running the comparison across these variants separates them.

    Varies scheme (TLS terminated at a proxy), host and port (a rewritten Host
    header), and path — a trailing slash matters because a client that hashes
    before following a redirect signs the pre-redirect URL.
    """
    parsed = urlparse(url)
    hosts = {parsed.netloc}
    hosts.update(h for h in extra_hosts if h)
    # A proxy forwarding to a non-default port leaves it in HTTP_HOST while the
    # client never saw it.
    if ':' in parsed.netloc:
        hosts.add(parsed.netloc.rsplit(':', 1)[0])

    paths = {parsed.path}
    if parsed.path.endswith('/'):
        paths.add(parsed.path.rstrip('/') or '/')
    else:
        paths.add(parsed.path + '/')

    # Some builds hash the path only; try dropping the query as well as keeping it.
    queries = {parsed.query, ''} if parsed.query else {''}

    variants = []
    for scheme in ('http', 'https'):
        for host in sorted(hosts):
            for path in sorted(paths):
                for query in sorted(queries):
                    candidate = urlunparse((scheme, host, path, parsed.params, query, ''))
                    if candidate != url and candidate not in variants:
                        variants.append(candidate)
    return variants


def _candidate_urls(request):
    """:func:`seb_url_variants` for the URL this request reconstructed."""
    forwarded_host = request.META.get('HTTP_X_FORWARDED_HOST')
    extra = [forwarded_host.split(',')[0].strip()] if forwarded_host else []
    return seb_url_variants(request.build_absolute_uri(), extra)


def _diagnose_url_mismatch(request, received, keys):
    """The URL, if any, whose hash matches what SEB sent. ``None`` if no variant does."""
    for candidate in _candidate_urls(request):
        for k in keys:
            if hmac.compare_digest(received, _sha256_hex(candidate, k)):
                return candidate
    return None


def request_matches_seb(request, allowed_bek, allowed_cfg):
    if not allowed_bek and not allowed_cfg:
        logger.warning('SEB check failed for %s: the contest has no Browser Exam Key and no Config Key '
                       'configured, so no client can ever match.', request.path)
        return False
    # SEB hashes the absolute URL including the query string; only the fragment
    # is stripped, and fragments never reach the server anyway.
    url = request.build_absolute_uri()
    seen = []
    for hdr, keys, label in ((BEK_HEADER, allowed_bek, 'Browser Exam Key'),
                             (CFG_HEADER, allowed_cfg, 'Config Key')):
        received = request.META.get(hdr)
        if not received:
            continue
        received = received.strip().lower()
        for k in keys:
            if hmac.compare_digest(received, _sha256_hex(url, k)):
                return True
        seen.append((label, received, keys))

    # Every rejection below blocks a student mid-exam, and the difference between
    # its causes is invisible from the block page. Say which one it was.
    if not seen:
        logger.warning(
            'SEB check failed for %s: the request carried neither the %s nor the %s header. Either it did '
            'not come from Safe Exam Browser, or the .seb has "Use Browser Exam Key and Config Key" '
            'switched off, or a proxy is stripping X-SafeExamBrowser-* headers.',
            request.path, BEK_HEADER, CFG_HEADER)
        return False

    for label, received, keys in seen:
        matching_url = _diagnose_url_mismatch(request, received, keys)
        if matching_url is not None:
            logger.warning(
                'SEB check failed for %s: %s hash mismatch, but the keys are CORRECT — SEB hashed the URL '
                '%r while this server hashed %r. Django is reconstructing the wrong URL, so fix the proxy '
                'configuration rather than the keys: set SECURE_PROXY_SSL_HEADER for a scheme difference '
                '(and make the proxy send X-Forwarded-Proto), USE_X_FORWARDED_HOST for a host difference, '
                'and make sure the proxy passes the original Host header.',
                request.path, label, matching_url, url)
            continue
        logger.warning(
            'SEB check failed for %s: %s hash mismatch, and no plausible URL variant matches either, so '
            'the configured key hashes do not belong to the .seb the student launched. Re-copy the %s from '
            'the SEB Configuration Tool for the exact .seb in use and paste it into the contest (or its '
            'preset). Rebuilding a .seb — even to add one URL-filter rule — changes both keys. SEB sent '
            '%s…; this server hashed the URL %r and expected one of %s.',
            request.path, label, label, _abbrev(received), url,
            ', '.join('%s… (from key %s…)' % (_abbrev(_sha256_hex(url, k)), _abbrev(k)) for k in keys))
    return False


def sign_seb_launch_token(contest, profile):
    """Sign a short-lived token authorizing one fetch of ``contest``'s ``.seb`` file.

    SEB fetches the configuration with a bare, cookie-less request before it has
    a browser session, so a login-gated endpoint answers it with a redirect to
    HTML. SEB reads that as "this resource needs authentication", falls back to
    opening the page in a temporary window running its *local* settings, and
    never enters lockdown. The authorization therefore has to travel in the URL.

    The token appears only in the fetch URL, never inside the ``.seb`` bytes, so
    the file's Config Key and Browser Exam Key are unchanged and configurations
    already published to students keep working.
    """
    signer = signing.TimestampSigner(salt=SEB_LAUNCH_TOKEN_SALT)
    payload = signer.sign_object({'c': contest.id, 'u': getattr(profile, 'id', None)})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def read_seb_launch_token(token):
    """Reverse :func:`sign_seb_launch_token`.

    Raises ``signing.SignatureExpired`` past the TTL and ``signing.BadSignature``
    for anything else unusable, so callers can tell "your link went stale" apart
    from "this was tampered with".
    """
    signer = signing.TimestampSigner(salt=SEB_LAUNCH_TOKEN_SALT)
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise signing.BadSignature('malformed SEB launch token')
    return signer.unsign_object(raw, max_age=settings.DMOJ_SEB_LAUNCH_TOKEN_EXPIRY_MINUTES * 60)


def to_seb_scheme(absolute_url):
    """Rewrite an http(s) URL to the scheme that hands the URL to the SEB client."""
    parsed = urlparse(absolute_url)
    scheme = 'sebs' if parsed.scheme == 'https' else 'seb'
    return urlunparse((scheme, parsed.netloc, parsed.path,
                       parsed.params, parsed.query, parsed.fragment))


def seb_launch_url(request, contest, profile):
    """The ``sebs://`` link that hands a freshly signed launch token to SEB."""
    token = sign_seb_launch_token(contest, profile)
    return to_seb_scheme(request.build_absolute_uri(reverse('seb_launch_config', args=(token,))))


def seb_launch_context(request, contest):
    download_url = None
    sebs_url = None
    token_url = None
    if contest.seb_effective_config_file:
        # The download button is for a real browser, so it stays on the
        # login-gated endpoint; only the sebs:// link needs the token.
        download_url = request.build_absolute_uri(reverse('contest_seb_config', args=(contest.key,)))
        profile = getattr(request, 'profile', None)
        if profile is not None:
            sebs_url = seb_launch_url(request, contest, profile)
            # Minting again on click keeps a page that has been open since
            # before the exam started from launching with an expired token.
            token_url = request.build_absolute_uri(reverse('seb_launch_token', args=(contest.key,)))
    return {
        'download_url': download_url,
        'sebs_url': sebs_url,
        'token_url': token_url,
        'quit_url': contest.seb_effective_quit_url,
        'install_url': SEB_INSTALL_URL,
    }
