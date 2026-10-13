"""Helpers for dual name/username user search and Select2 labels.

Teacher-facing pickers show ``Name (username)`` and match either side.
Permissions / who is searchable stay in the calling view; this module only
builds the OR filter and the display label.
"""
from django.db.models import Q


def format_user_label(name, username):
    """Return ``Name (username)``; fall back to username for the name side."""
    username = (username or '').strip()
    name = (name or '').strip() or username
    if not username:
        return name
    return '%s (%s)' % (name, username)


def profile_person_name(first_name='', last_name='', display_override='', username=''):
    """Pick the human-readable name: display override, then first/last, then username."""
    override = (display_override or '').strip()
    if override:
        return override
    first = (first_name or '').strip()
    last = (last_name or '').strip()
    full = ('%s %s' % (first, last)).strip()
    if full:
        return full
    return (username or '').strip()


def _profile_name_parts(profile):
    user = getattr(profile, 'user', None)
    username = user.username if user is not None else getattr(profile, 'username', '') or str(profile)
    first = getattr(user, 'first_name', '') if user is not None else ''
    last = getattr(user, 'last_name', '') if user is not None else ''
    override = getattr(profile, 'username_display_override', '') or ''
    return profile_person_name(first, last, override, username), username


def profile_choice_label(profile):
    """Select2 / chip label for a Profile: ``Name (username)``."""
    name, username = _profile_name_parts(profile)
    return format_user_label(name, username)


def profile_author_label(profile):
    """List-column label: real name, or ``Name (username)`` when they differ."""
    name, username = _profile_name_parts(profile)
    if not name or name == username:
        return username
    return format_user_label(name, username)


def profile_choice_payload(profile):
    """JSON dict for a profile picker option (id, username, name, text)."""
    if profile is None:
        return {'id': None, 'username': '', 'name': '', 'text': ''}
    user = profile.user
    username = user.username
    name = profile_person_name(
        user.first_name, user.last_name, profile.username_display_override, username,
    )
    return {
        'id': profile.id,
        'username': username,
        'name': name,
        'text': format_user_label(name, username),
    }


def profile_choice_payloads(queryset):
    return [profile_choice_payload(profile) for profile in queryset.select_related('user')]


def _and_token_q(term, token_q):
    tokens = (term or '').split()
    if not tokens:
        return Q()
    combined = Q()
    for token in tokens:
        combined &= token_q(token)
    return combined


def profile_user_search_q(term):
    """OR-filter a Profile queryset by username, display name, or real name."""
    return _and_token_q(term, lambda token: (
        Q(user__username__icontains=token) |
        Q(username_display_override__icontains=token) |
        Q(user__first_name__icontains=token) |
        Q(user__last_name__icontains=token)
    ))


def auth_user_search_q(term):
    """OR-filter a django.contrib.auth User queryset the same way."""
    return _and_token_q(term, lambda token: (
        Q(username__icontains=token) |
        Q(first_name__icontains=token) |
        Q(last_name__icontains=token) |
        Q(profile__username_display_override__icontains=token)
    ))


def apply_profile_choice_labels(form):
    """Make already-selected Profile chips use ``Name (username)``."""
    for field in form.fields.values():
        queryset = getattr(field, 'queryset', None)
        model = getattr(queryset, 'model', None)
        if model is None:
            continue
        meta = getattr(model, '_meta', None)
        if meta is not None and meta.app_label == 'judge' and meta.model_name == 'profile':
            field.label_from_instance = profile_choice_label
