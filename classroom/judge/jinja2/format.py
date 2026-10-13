import html

from django.template.defaultfilters import truncatechars as django_truncatechars
from django.utils.html import escapejs, format_html

from . import registry


@registry.function
def bold(text):
    return format_html('<b>{0}</b>', text)


@registry.filter
def htmltojs(text):
    return format_html("'{0}'", escapejs(html.unescape(text)))


@registry.filter
def truncatechars(text, length=80):
    """Django-compatible truncatechars for Jinja course/join templates."""
    return django_truncatechars(text, length)
