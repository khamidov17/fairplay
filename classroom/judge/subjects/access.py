"""Who sees which features: the runtime half of the subject registry.

Rules (also in docs/classroom/SUBJECTS.md):

* A course (``Organization``) has one subject; the subject's packs decide its features.
* In a course context (a course page, a class, an assignment or problem attached to courses),
  a user sees the union of the features of those courses only. A teacher of a CS course and a
  Biology course therefore sees each course with its own features.
* Without a course context (site-wide pages: the navigation bar, the problem list, the IDE),
  a user sees the union of the features of every course they belong to (member, class member,
  course admin, course instructor or class admin).
* A user who belongs to no course at all (anonymous visitors, new accounts) sees the features of
  ``DEFAULT_SUBJECT`` (CS = everything), so nothing changes for DKU OJ's public pages.
* Site-wide course editors (superusers and ``judge.edit_all_organization``) see everything. Note
  that site teachers get ``is_staff``, so ``is_staff`` alone is deliberately NOT "see everything".
"""
from django.db.models import Q
from django.utils.translation import gettext as _

from judge.subjects.base import (CREATE_V2_TYPE_CAPS, SUBMISSION_TYPE_CAPS, all_features, get_pack, get_subject,
                                 sorted_sections, subject_features)
from judge.subjects.definitions import DEFAULT_SUBJECT

_USER_CACHE_ATTR = '_subject_features_cache'


def sees_everything(user):
    return bool(user is not None and getattr(user, 'is_authenticated', False) and (
        user.is_superuser or user.has_perm('judge.edit_all_organization')))


def subject_for(course):
    """The :class:`~judge.subjects.base.Subject` of a course (``Organization``), or of a ``Class``."""
    if course is None:
        return get_subject(DEFAULT_SUBJECT)
    organization = getattr(course, 'organization', None) if not hasattr(course, 'subject') else course
    key = getattr(organization, 'subject', None) or DEFAULT_SUBJECT
    return get_subject(key)


def course_features(course):
    return subject_features(subject_for(course))


def resolve_courses(context):
    """Turn a course context into a list of ``Organization`` objects.

    Accepts an ``Organization``, a ``Class``, a ``Contest`` (its courses plus its classes'
    courses), a ``Problem`` (its courses), or an iterable of those. ``None`` -> ``[]``.
    """
    from judge.models import Class, Contest, Organization, Problem
    if context is None:
        return []
    if isinstance(context, Organization):
        return [context]
    if isinstance(context, Class):
        return [context.organization]
    if isinstance(context, Contest):
        if context.pk is None:
            return []
        return list(Organization.objects.filter(
            Q(contest=context) | Q(**{'class__contest': context})).distinct())
    if isinstance(context, Problem):
        if context.pk is None:
            return []
        return list(context.organizations.all())
    if isinstance(context, (list, tuple, set, frozenset)) or hasattr(context, 'model'):
        courses = []
        for item in context:
            courses.extend(resolve_courses(item))
        return courses
    raise TypeError('Not a course context: %r' % (context,))


def user_course_subject_keys(user):
    """Subject keys of every course ``user`` belongs to (one query, cached on the user object)."""
    from judge.models import Organization
    if user is None or not getattr(user, 'is_authenticated', False):
        return frozenset()
    profile = getattr(user, 'profile', None)
    if profile is None:
        return frozenset()
    q = (Q(member=profile) | Q(admins=profile) | Q(instructors=profile) |
         Q(**{'class__admins': profile}) | Q(**{'class__members': profile}))
    return frozenset(Organization.objects.filter(q).values_list('subject', flat=True).distinct())


def site_features(user):
    """Features ``user`` sees without a course context (cached on ``user`` for the request)."""
    cached = getattr(user, _USER_CACHE_ATTR, None) if user is not None else None
    if cached is not None:
        return cached
    if sees_everything(user):
        features = all_features()
    else:
        keys = user_course_subject_keys(user)
        if not keys:
            features = subject_features(get_subject(DEFAULT_SUBJECT))
        else:
            features = frozenset().union(*(subject_features(get_subject(k)) for k in keys))
    if user is not None:
        try:
            setattr(user, _USER_CACHE_ATTR, features)
        except AttributeError:
            pass
    return features


def clear_cached_features(user):
    if user is not None and hasattr(user, _USER_CACHE_ATTR):
        delattr(user, _USER_CACHE_ATTR)


def features_for(user, course=None):
    """The set of pack keys and capabilities ``user`` has.

    ``course`` is a course context (see :func:`resolve_courses`). With a context that resolves to
    at least one course, the result is the union of those courses' features; otherwise it is the
    user's site-wide features. Site-wide editors always get everything.
    """
    if sees_everything(user):
        return all_features()
    courses = resolve_courses(course)
    if courses:
        return frozenset().union(*(course_features(c) for c in courses))
    return site_features(user)


def has_feature(user, feature, course=None):
    """Whether ``feature`` (a pack key or a capability) is on for ``user`` in ``course``."""
    return feature in features_for(user, course)


# ----------------------------------------------------------------------------- question types

def capability_for_question_type(question_type):
    """Capability for a create-v2 type ('programming', 'single', ...) or a submission_type letter."""
    return CREATE_V2_TYPE_CAPS.get(question_type) or SUBMISSION_TYPE_CAPS.get(question_type)


def question_type_allowed(user, question_type, courses=None):
    cap = capability_for_question_type(question_type)
    if cap is None:
        return True
    return question_type_refusal(user, question_type, courses) is None


def question_type_refusal(user, question_type, courses=None):
    """``None`` when ``user`` may create a ``question_type`` problem attached to ``courses``,
    else a user-facing message.

    Every attached course must allow the type (a programming problem cannot be put into a
    Biology course, not even by a site editor: the rule protects the course, not the user).
    With no course attached, the user's site-wide features decide.
    """
    cap = capability_for_question_type(question_type)
    if cap is None:
        return None
    courses = resolve_courses(courses)
    if courses:
        for course in courses:
            if cap not in course_features(course):
                subject = subject_for(course)
                return _('%(type)s questions are not available in %(course)s (subject: %(subject)s). '
                         'Ask a course administrator to change the course subject.') % {
                    'type': _question_type_label(question_type), 'course': course.name,
                    'subject': subject.label}
        return None
    if cap not in features_for(user):
        return _('%(type)s questions are not available in the subjects of your courses.') % {
            'type': _question_type_label(question_type)}
    return None


def ensure_question_type_allowed(user, question_type, courses=None):
    """Raise ``PermissionError`` with a user-facing message when the type is not allowed."""
    message = question_type_refusal(user, question_type, courses)
    if message:
        raise PermissionError(message)


def _question_type_label(question_type):
    labels = {
        'programming': _('Programming'), 'C': _('Programming'),
        'single': _('Single choice'), 'multiple': _('Multiple choice'), 'M': _('Choice'),
        'truefalse': _('True/False'), 'blank': _('Fill-in-blank'), 'B': _('Fill-in-blank'),
        'short': _('Short answer'), 'S': _('Short answer'), 'file': _('File upload'), 'F': _('File upload'),
        'compound': _('Composite'), 'other': _('Other'),
    }
    return labels.get(question_type, question_type)


def filter_question_types(user, question_types, course=None):
    """Keep the registered ``QuestionType`` objects whose capability ``user`` has (in ``course``)."""
    features = features_for(user, course)
    kept = []
    for question_type in question_types:
        cap = SUBMISSION_TYPE_CAPS.get(question_type.submission_type)
        if cap is None or cap in features:
            kept.append(question_type)
    return kept


def course_question_caps(courses):
    """``{course_id: [capability, ...]}`` for the create page's client-side type check."""
    return {c.pk: sorted(course_features(c)) for c in resolve_courses(courses)}


# ----------------------------------------------------------------------------- dashboards

def _is_teacher(user, course):
    if user is None or not getattr(user, 'is_authenticated', False) or course is None:
        return False
    if sees_everything(user):
        return True
    return course.profile_teaches_course(getattr(user, 'profile', None))


def teaching_courses(user, limit=12):
    """Courses ``user`` teaches (course admin, course instructor or class admin), for the teacher home."""
    from judge.models import Organization
    if user is None or not getattr(user, 'is_authenticated', False) or getattr(user, 'profile', None) is None:
        return []
    profile = user.profile
    q = Q(admins=profile) | Q(instructors=profile) | Q(**{'class__admins': profile})
    ids = Organization.objects.filter(q).values('id').distinct()
    return list(Organization.objects.filter(id__in=ids).order_by('name')[:limit])


def sections_for(user, place, course=None, is_teacher=None):
    """Dashboard sections for ``place`` ('course' or 'teacher_home').

    With ``course``: that course's packs. Without: the packs of every subject in the user's
    courses (each section once), used by the teacher home.
    """
    if course is not None:
        pack_keys = set(subject_for(course).packs) | {'core'}
        teacher = _is_teacher(user, course) if is_teacher is None else is_teacher
    else:
        pack_keys = {'core'}
        for key in user_course_subject_keys(user) or {DEFAULT_SUBJECT}:
            pack_keys |= set(get_subject(key).packs)
        teacher = True if is_teacher is None else is_teacher
    return [s for s in sorted_sections(pack_keys, place) if teacher or not s.teacher_only]


def _meets(user, requires):
    """``requires`` is a permission name, or a callable taking the user (for rules with an OR)."""
    if user is None:
        return False
    if callable(requires):
        return bool(requires(user))
    return user.has_perm(requires)


def menu_items_for(user, course, is_teacher=None, pack=None):
    """``[(MenuItem, href), ...]`` the course's packs (or only ``pack``, if the course has it)
    contribute for ``user``."""
    subject = subject_for(course)
    teacher = _is_teacher(user, course) if is_teacher is None else is_teacher
    keys = ('core',) + tuple(k for k in subject.packs if k != 'core')
    if pack is not None:
        keys = tuple(k for k in keys if k == pack)
    items = []
    for key in keys:
        for item in get_pack(key).menu_items:
            if item.teacher_only and not teacher:
                continue
            if item.requires and not _meets(user, item.requires):
                continue
            href = item.href(course)
            if href:
                items.append((item, href))
    return items


# ----------------------------------------------------------------------------- navigation

# Navigation-bar entries (``NavigationBar`` rows are admin-editable) that belong to a capability.
# Matched on the entry's ``key`` or on the start of its ``path``.
NAV_KEY_CAPS = {
    'ide': 'ide',
    'language_stats': 'language_stats',
    'runtimes': 'judge',
    'judges': 'judge',
}
NAV_PATH_CAPS = (
    ('/ide', 'ide'),
    ('/stats/language', 'language_stats'),
    ('/runtimes', 'judge'),
    ('/status', 'judge'),
    ('/users', 'rankings'),
)


def nav_capability(node):
    key = getattr(node, 'key', '') or ''
    if key in NAV_KEY_CAPS:
        return NAV_KEY_CAPS[key]
    path = getattr(node, 'path', '') or ''
    for prefix, cap in NAV_PATH_CAPS:
        if path.startswith(prefix):
            return cap
    return None


def nav_item_visible(user, node):
    cap = nav_capability(node)
    return cap is None or has_feature(user, cap)
