"""Building blocks of the subject registry: capabilities, feature packs, subjects.

This module is pure Python (no models, no database), so it can be imported from models,
migrations, forms and settings without import cycles. The concrete packs and subjects are
declared in ``judge/subjects/definitions.py``; read ``docs/classroom/ADDING_A_SUBJECT.md``.

Vocabulary
----------
* A **capability** is one thing the UI or the server can switch on or off, e.g. ``ide`` or
  ``question_programming``. Capabilities are plain strings (see :class:`Cap`).
* A **feature pack** is a named bundle of capabilities plus the dashboard sections and menu
  items it contributes (e.g. ``code`` = programming problems, judge, IDE, test data, MOSS...).
* A **subject** (e.g. Computer Science, Biology) is a key, a label, the DKU course-code prefixes
  that map to it, and the feature packs its courses get. ``core`` is always included.

``has_feature(name)`` accepts either a pack key or a capability, so templates can ask
``has_feature('code')`` (the whole pack) or ``has_feature('ide')`` (one capability).
"""
import re
from dataclasses import dataclass, field
from typing import Optional, Tuple

from django.core.exceptions import ValidationError
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _


class Cap:
    """Capability names. Add a constant here when a new switchable feature appears."""
    # Question types (core pack).
    QUESTION_CHOICE = 'question_choice'              # single and multiple choice
    QUESTION_TRUEFALSE = 'question_truefalse'
    QUESTION_FILL_BLANK = 'question_fill_blank'
    QUESTION_SHORT_ANSWER = 'question_short_answer'
    QUESTION_FILE_UPLOAD = 'question_file_upload'
    QUESTION_COMPOSITE = 'question_composite'        # shown in create v2, not savable yet
    QUESTION_OTHER = 'question_other'                # shown in create v2, not savable yet
    EXCEL_IMPORT = 'excel_import'

    # Programming (code pack).
    QUESTION_PROGRAMMING = 'question_programming'
    JUDGE = 'judge'                  # judge status / runtimes pages, judge-side settings
    IDE = 'ide'                      # /ide/ and "Run tests" in the workspace
    TEST_DATA = 'test_data'          # test data editor, test-case ZIPs, checkers
    HARNESS = 'harness'              # code harness (function-style problems)
    MOSS = 'moss'                    # MOSS plagiarism check
    RANKINGS = 'rankings'            # user ranking list, contest scoreboards for students
    LANGUAGE_STATS = 'language_stats'


# The values the problem create v2 page posts as ``type`` -> the capability that allows it.
CREATE_V2_TYPE_CAPS = {
    'programming': Cap.QUESTION_PROGRAMMING,
    'single': Cap.QUESTION_CHOICE,
    'multiple': Cap.QUESTION_CHOICE,
    'truefalse': Cap.QUESTION_TRUEFALSE,
    'blank': Cap.QUESTION_FILL_BLANK,
    'short': Cap.QUESTION_SHORT_ANSWER,
    'file': Cap.QUESTION_FILE_UPLOAD,
    'compound': Cap.QUESTION_COMPOSITE,
    'other': Cap.QUESTION_OTHER,
}

# ``Problem.submission_type`` values -> capability (kept as literals so this module stays model-free;
# ``judge.models.problem.ProblemSubmissionType`` defines the same letters).
SUBMISSION_TYPE_CAPS = {
    'C': Cap.QUESTION_PROGRAMMING,
    'M': Cap.QUESTION_CHOICE,
    'B': Cap.QUESTION_FILL_BLANK,
    'S': Cap.QUESTION_SHORT_ANSWER,
    'F': Cap.QUESTION_FILE_UPLOAD,
}


@dataclass(frozen=True)
class MenuItem:
    """A link a pack adds to a course card on the teacher home and to the course page.

    ``url_name`` is reversed without arguments; with ``course_param`` the link gets
    ``?course=<id>`` so the target page opens in that course's context.
    """
    key: str
    label: str
    url_name: str
    icon: str = 'fa-link'
    course_param: bool = False
    teacher_only: bool = True
    requires: Optional[object] = None   # permission name or callable(user), see access.menu_items_for

    def href(self, course=None):
        try:
            url = reverse(self.url_name)
        except NoReverseMatch:
            return None
        if self.course_param and course is not None and getattr(course, 'pk', None):
            url = '%s?course=%d' % (url, course.pk)
        return url


@dataclass(frozen=True)
class DashboardSection:
    """A block a pack contributes to a dashboard.

    ``places`` says where it renders: ``'course'`` (the course page) and/or ``'teacher_home'``
    (the "Your courses" panel on the home page). The template gets ``section``, ``course``,
    ``subject`` and ``is_teacher`` in its context.
    """
    key: str
    title: str
    template: str
    places: Tuple[str, ...] = ('course',)
    order: int = 100
    teacher_only: bool = False


@dataclass(frozen=True)
class FeaturePack:
    key: str
    label: str
    description: str = ''
    capabilities: frozenset = field(default_factory=frozenset)
    sections: Tuple[DashboardSection, ...] = ()
    menu_items: Tuple[MenuItem, ...] = ()
    # True for packs that exist so subjects can already list them, but that add nothing yet.
    placeholder: bool = False


@dataclass(frozen=True)
class Subject:
    key: str
    label: str
    packs: Tuple[str, ...]
    course_prefixes: Tuple[str, ...] = ()
    description: str = ''
    # Badge colour on course cards (CSS colour); purely cosmetic.
    color: str = '#5c6b7a'
    order: int = 100


SUBJECT_KEY_RE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')

_packs = {}
_subjects = {}


def register_pack(pack):
    if pack.key in _packs:
        raise ValueError('Duplicate feature pack %r' % pack.key)
    if not SUBJECT_KEY_RE.match(pack.key):
        raise ValueError('Invalid feature pack key %r' % pack.key)
    _packs[pack.key] = pack
    return pack


def register_subject(subject):
    if subject.key in _subjects:
        raise ValueError('Duplicate subject %r' % subject.key)
    if not SUBJECT_KEY_RE.match(subject.key):
        raise ValueError('Invalid subject key %r (lowercase letters, digits, _; max 32)' % subject.key)
    missing = [p for p in subject.packs if p not in _packs]
    if missing:
        raise ValueError('Subject %r uses unknown feature packs: %s' % (subject.key, ', '.join(missing)))
    for prefix in subject.course_prefixes:
        prefix = prefix.upper()
        for other in _subjects.values():
            if prefix in (p.upper() for p in other.course_prefixes):
                raise ValueError('Course prefix %r is already mapped to subject %r' % (prefix, other.key))
    _subjects[subject.key] = subject
    return subject


def get_pack(key):
    return _packs[key]


def all_packs():
    return list(_packs.values())


def get_subject(key):
    """The registered subject for ``key``; unknown or empty keys fall back to ``FALLBACK_SUBJECT``."""
    from judge.subjects.definitions import FALLBACK_SUBJECT
    return _subjects.get(key) or _subjects[FALLBACK_SUBJECT]


def is_registered_subject(key):
    return key in _subjects


def all_subjects():
    return sorted(_subjects.values(), key=lambda s: (s.order, str(s.label)))


def subject_choices():
    """``(key, label)`` pairs for forms, in display order."""
    return [(s.key, s.label) for s in all_subjects()]


def validate_subject_key(value):
    """Model-field validator: the value must be a registered subject."""
    if value not in _subjects:
        raise ValidationError(_('Unknown subject %(value)s.'), params={'value': value}, code='invalid_subject')


_PREFIX_RE = re.compile(r'^\s*([A-Za-z]+)')


def course_code_prefix(code):
    """``'COMPSCI 201'`` -> ``'COMPSCI'``; ``'math205'`` -> ``'MATH'``; no letters -> ``''``."""
    match = _PREFIX_RE.match(code or '')
    return match.group(1).upper() if match else ''


def infer_subject_key(*codes, default=None):
    """The subject whose DKU course prefix starts the first matching code, else ``default``.

    Each argument is a course code or name (``Organization.short_name`` first, then ``name``).
    """
    from judge.subjects.definitions import DEFAULT_SUBJECT
    for code in codes:
        prefix = course_code_prefix(code)
        if not prefix:
            continue
        for subject in _subjects.values():
            if prefix in (p.upper() for p in subject.course_prefixes):
                return subject.key
    return default if default is not None else DEFAULT_SUBJECT


def prefix_labels():
    """``{'COMPSCI': 'Computer science', ...}`` for the course form's "Detected:" hint."""
    return {prefix.upper(): str(s.label) for s in _subjects.values() for prefix in s.course_prefixes}


def subject_features(subject):
    """Every pack key and capability a subject grants (``core`` is always included)."""
    pack_keys = set(subject.packs) | {'core'}
    features = set(pack_keys)
    for key in pack_keys:
        features |= set(_packs[key].capabilities)
    return frozenset(features)


def all_features():
    features = set(_packs)
    for pack in _packs.values():
        features |= set(pack.capabilities)
    return frozenset(features)


def sorted_sections(pack_keys, place):
    sections = []
    for key in pack_keys:
        sections.extend(s for s in _packs[key].sections if place in s.places)
    return sorted(sections, key=lambda s: (s.order, s.key))
