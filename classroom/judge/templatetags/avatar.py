"""``{% avatar_url user 16 %}`` for Django templates (the admin): the same avatar as the site.

Uses ``judge.jinja2.gravatar.gravatar``, so it is drawn locally unless DMOJ_USE_GRAVATAR is on.
"""
from django import template

register = template.Library()


@register.simple_tag
def avatar_url(user, size=80):
    from judge.jinja2.gravatar import gravatar
    return gravatar(user, size)
