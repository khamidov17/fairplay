"""Helpers for creating problems with sensible defaults for the code-centric required fields.

Mirrors the "simple preset" defaulting used by the admin's ``SimpleProblemCreationForm`` so that
front-end authoring of non-code question types (e.g. multiple-choice) doesn't make teachers fill
in time/memory limits, languages, or a problem code they don't care about.
"""
from django.conf import settings
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models import Problem, ProblemGroup, ProblemType, Solution
from judge.utils.unique_ids import next_prefixed_id

# Scalar / FK attnames compared when building an admin CHANGE message. FK ids map back to
# the model field (``group_id`` → ``group``) so the log shows the same verbose names as
# classic ProblemAdmin construct_change_message.
_PROBLEM_LOG_ATTRS = (
    'code', 'name', 'description', 'points', 'partial', 'time_limit', 'memory_limit',
    'group_id', 'is_public', 'is_organization_private', 'is_manually_managed',
    'is_full_markup', 'view_test_cases', 'view_tester', 'ai_hints_enabled',
    'short_circuit', 'og_image', 'summary', 'submission_source_visibility_mode',
    'license_id', 'submission_type', 'date',
)
_PROBLEM_LOG_M2M = (
    'types', 'organizations', 'allowed_languages', 'authors', 'curators',
    'testers', 'banned_users', 'classes',
)


def generate_problem_code():
    return next_prefixed_id(
        Problem.objects.values_list('code', flat=True),
        'p',
        fallback_prefix='mcq',
    )


def default_problem_group():
    """The group new problems default to.

    Prefer an explicit ``uncategorized`` group so front-end authoring lands there regardless of
    how many other groups exist; fall back to the first group, creating ``uncategorized`` if the
    table is empty.
    """
    group = ProblemGroup.objects.filter(name='uncategorized').first()
    if group is not None:
        return group
    group = ProblemGroup.objects.order_by('full_name', 'name').first()
    if group is None:
        group = ProblemGroup.objects.create(name='uncategorized', full_name='Uncategorized')
    return group


def default_problem_type():
    ptype = ProblemType.objects.order_by('full_name', 'name').first()
    if ptype is None:
        ptype = ProblemType.objects.create(name='uncategorized', full_name='Uncategorized')
    return ptype


def apply_simple_problem_defaults(problem, preferred_code=None):
    """Fill the code-centric required scalar fields a non-code problem doesn't use.

    M2M defaults (``types``) must be assigned by the caller after the problem is saved.
    """
    if not problem.code:
        preferred = (preferred_code or '').strip()
        problem.code = preferred or generate_problem_code()
    if problem.date is None:
        problem.date = timezone.now()
    if problem.time_limit is None:
        problem.time_limit = min(max(1, settings.DMOJ_PROBLEM_MIN_TIME_LIMIT), settings.DMOJ_PROBLEM_MAX_TIME_LIMIT)
    if not problem.memory_limit:
        problem.memory_limit = min(max(262144, settings.DMOJ_PROBLEM_MIN_MEMORY_LIMIT),
                                   settings.DMOJ_PROBLEM_MAX_MEMORY_LIMIT)
    if problem.group_id is None:
        problem.group = default_problem_group()


def _problem_log_field_label(attr):
    field_name = attr[:-3] if attr.endswith('_id') else attr
    try:
        return str(Problem._meta.get_field(field_name).verbose_name)
    except FieldDoesNotExist:
        return field_name


def problem_admin_snapshot(problem):
    """Capture field values used to describe a later CHANGE LogEntry."""
    if problem is None or problem.pk is None:
        return None
    data = {attr: getattr(problem, attr) for attr in _PROBLEM_LOG_ATTRS}
    for field in _PROBLEM_LOG_M2M:
        data[field] = list(getattr(problem, field).values_list('pk', flat=True).order_by('pk'))
    data['solution'] = Solution.objects.filter(problem_id=problem.pk).values_list(
        'content', 'is_public', 'publish_on',
    ).first()
    return data


def problem_admin_changed_fields(before, problem):
    """Return verbose field labels that differ from ``before`` (admin CHANGE format)."""
    after = problem_admin_snapshot(problem)
    if not before or not after:
        return []
    labels = []
    seen = set()
    for key, value in after.items():
        if before.get(key) == value:
            continue
        label = str(_('editorial')) if key == 'solution' else _problem_log_field_label(key)
        if label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def log_problem_admin_entry(request, problem, *, created, changed_fields=None, message=''):
    """Write a django.contrib.admin LogEntry for a front-end problem create/edit.

    Calls ``LogEntry.objects.log_action``, the same helper ModelAdmin.log_addition /
    log_change use, so the admin Log entries list includes new-frontend saves.
    A non-empty ``message`` (the optional edit reason) replaces the structured
    payload, matching ProblemAdmin.construct_change_message.
    """
    message = (message or '').strip()
    if message:
        change_message = message
    elif created:
        change_message = [{'added': {}}]
    elif changed_fields:
        change_message = [{'changed': {'fields': [str(label) for label in changed_fields]}}]
    else:
        change_message = []

    return LogEntry.objects.log_action(
        user_id=request.user.pk,
        content_type_id=ContentType.objects.get_for_model(problem, for_concrete_model=False).pk,
        object_id=problem.pk,
        object_repr=str(problem),
        action_flag=ADDITION if created else CHANGE,
        change_message=change_message,
    )
