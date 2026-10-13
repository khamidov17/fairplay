"""URLs of front-end libraries that are served from this site's static files.

Classroom must work on networks that block or throttle public CDNs (DKU is in mainland
China), so libraries come from ``resources/libs`` (copied there from npm at build time) unless
a setting points somewhere else on purpose.
"""
from django.conf import settings
from django.templatetags.static import static

__all__ = ['ace_url']


def ace_url():
    """Base URL of the Ace editor build, without a trailing slash (``<base>/ace.js``)."""
    return (getattr(settings, 'ACE_URL', None) or static('libs/ace')).rstrip('/')
