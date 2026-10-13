"""User avatars.

By default avatars are drawn locally: a small SVG with the user's initial on a colour derived
from their e-mail, returned as a ``data:`` URI. Nothing is fetched from another site, so pages
load the same on networks that block or throttle gravatar.com (DKU is in mainland China).
Set ``DMOJ_USE_GRAVATAR = True`` in local settings to use Gravatar instead.
"""
import hashlib
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.utils.http import urlencode

from judge.models import Profile
from judge.utils.unicode import utf8bytes
from . import registry

_PALETTE = ('#4f6d7a', '#c0392b', '#2e86ab', '#7b5ea7', '#3b8f5a', '#b5651d', '#5c6bc0', '#8d6e63',
            '#00838f', '#ad1457', '#558b2f', '#6d4c41')


def _gravatar_url(email, size, default):
    url = 'https://www.gravatar.com/avatar/' + hashlib.md5(utf8bytes(email.strip().lower())).hexdigest() + '?'
    args = {'d': 'identicon', 's': str(size)}
    if default:
        args['f'] = 'y'
    return url + urlencode(args)


def _local_avatar(email, initial, size, muted):
    digest = hashlib.md5(utf8bytes(email.strip().lower())).hexdigest()
    colour = '#9e9e9e' if muted else _PALETTE[int(digest[:8], 16) % len(_PALETTE)]
    letter = (initial or '?')[:1].upper()
    letter = letter.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}" viewBox="0 0 64 64">'
           '<rect width="64" height="64" fill="{c}"/>'
           '<text x="32" y="43" font-family="Helvetica,Arial,sans-serif" font-size="32" '
           'font-weight="600" fill="#fff" text-anchor="middle">{l}</text></svg>').format(
        s=int(size), c=colour, l=letter)
    return 'data:image/svg+xml;charset=utf-8,' + quote(svg)


@registry.function
def gravatar(email, size=80, default=None):
    initial = ''
    muted = False
    if isinstance(email, Profile):
        if default is None:
            default = email.mute
        muted = bool(email.mute)
        user = email.user
        initial = user.first_name or user.username
        email = user.email
    elif isinstance(email, AbstractUser):
        initial = email.first_name or email.username
        email = email.email
    email = email or ''
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 80

    if getattr(settings, 'DMOJ_USE_GRAVATAR', False):
        return _gravatar_url(email, size, default)
    return _local_avatar(email, initial or email, size, muted)


def avatar_icon_url(email):
    """An http(s) avatar for WebAuthn's optional icon, or None when Gravatar is off."""
    if getattr(settings, 'DMOJ_USE_GRAVATAR', False):
        return _gravatar_url(email or '', 80, None)
    return None
