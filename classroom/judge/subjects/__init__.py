"""Subject registry: which features each DKU subject's courses get.

Public API (import from ``judge.subjects``)::

    subject_for(course)                  -> Subject
    features_for(user, course=None)      -> frozenset of pack keys and capabilities
    has_feature(user, feature, course=None) -> bool
    ensure_question_type_allowed(user, question_type, courses)  # raises PermissionError
    infer_subject_key(course_code, ...)  -> subject key from a DKU course-code prefix

In Jinja templates: ``has_feature('code')``, ``has_feature('ide', course)``, ``course_subject(course)``,
``subject_sections('course', course)``, ``subject_menu_items(course)``. A view can set
``subject_course`` in its context to make it the default course for ``has_feature``.

Packs and subjects are declared in ``definitions.py``; see docs/classroom/ADDING_A_SUBJECT.md.
"""
from judge.subjects import definitions  # noqa: F401  (registers the built-in packs and subjects)
from judge.subjects.access import (
    capability_for_question_type, clear_cached_features, course_question_caps, ensure_question_type_allowed,
    features_for, filter_question_types, has_feature, menu_items_for, nav_item_visible, question_type_allowed,
    question_type_refusal, resolve_courses, sections_for, sees_everything, site_features, subject_for,
    teaching_courses,
)
from judge.subjects.base import (
    CREATE_V2_TYPE_CAPS, Cap, DashboardSection, FeaturePack, MenuItem, SUBMISSION_TYPE_CAPS, Subject, all_features,
    all_packs, all_subjects, course_code_prefix, get_pack, get_subject, infer_subject_key, is_registered_subject,
    prefix_labels, register_pack, register_subject, subject_choices, subject_features, validate_subject_key,
)
from judge.subjects.definitions import DEFAULT_SUBJECT, FALLBACK_SUBJECT

__all__ = [
    'CREATE_V2_TYPE_CAPS', 'Cap', 'DEFAULT_SUBJECT', 'DashboardSection', 'FALLBACK_SUBJECT', 'FeaturePack',
    'MenuItem', 'SUBMISSION_TYPE_CAPS', 'Subject', 'all_features', 'all_packs', 'all_subjects',
    'capability_for_question_type', 'clear_cached_features', 'course_code_prefix', 'course_question_caps',
    'ensure_question_type_allowed', 'features_for', 'filter_question_types', 'get_pack', 'get_subject',
    'has_feature', 'infer_subject_key', 'is_registered_subject', 'menu_items_for', 'nav_item_visible',
    'prefix_labels', 'question_type_allowed', 'question_type_refusal', 'register_pack', 'register_subject',
    'resolve_courses', 'sections_for', 'sees_everything', 'site_features', 'subject_choices', 'subject_features',
    'subject_for', 'teaching_courses', 'validate_subject_key',
]
