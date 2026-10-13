"""Jinja globals for the subject registry (registered from ``judge/jinja2/__init__.py``).

``has_feature(feature, course=None)`` reads the request user from the template context. When
``course`` is omitted it uses the context variable ``subject_course`` if the view set one, so a
course page can say ``has_feature('code')`` and get that course's answer.
"""
from jinja2 import pass_context

from judge.jinja2 import registry
from judge.subjects import access


def _user(ctx):
    request = ctx.get('request')
    return getattr(request, 'user', None) if request is not None else None


def _course(ctx, course):
    return course if course is not None else ctx.get('subject_course')


@registry.function('has_feature')
@pass_context
def has_feature(ctx, feature, course=None):
    return access.has_feature(_user(ctx), feature, _course(ctx, course))


@registry.function('course_subject')
def course_subject(course):
    return access.subject_for(course)


@registry.function('subject_sections')
@pass_context
def subject_sections(ctx, place, course=None, is_teacher=None):
    return access.sections_for(_user(ctx), place, _course(ctx, course), is_teacher=is_teacher)


@registry.function('subject_menu_items')
@pass_context
def subject_menu_items(ctx, course, is_teacher=None, pack=None):
    return access.menu_items_for(_user(ctx), course, is_teacher=is_teacher, pack=pack)


@registry.function('nav_item_visible')
@pass_context
def nav_item_visible(ctx, node):
    return access.nav_item_visible(_user(ctx), node)
