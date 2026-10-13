"""NetID-shaped usernames are reserved for Duke NetID sign-in (CA-7).

An SSO login binds to the local account whose username is the NetID. If anyone else could
create an account with that username first (local registration, or a social-auth sign-up
through Google/GitHub/Facebook), they would receive the student's SSO logins.
"""
import re

from django.conf import settings

__all__ = ['is_sso_reserved_username', 'SSO_RESERVED_USERNAME_MESSAGE']

SSO_RESERVED_USERNAME_MESSAGE = ('This username is reserved for NetID sign-in. Use the NetID login, '
                                 'or choose a different username.')


def is_sso_reserved_username(username):
    pattern = getattr(settings, 'SSO_RESERVED_USERNAME_REGEX', '')
    return bool(pattern and username and re.match(pattern, username))
