"""Human-friendly language matching for spreadsheet imports."""
import re


def normalize_language_name(value):
    """Normalize display names and internal keys, e.g. ``C++11`` -> ``cpp11``."""
    text = str(value or '').casefold().replace('++', 'pp').replace('#', 'sharp')
    return re.sub(r'[^a-z0-9]+', '', text)


def matching_languages(queryset, token):
    """Find exact or normalized full-name matches without substring guessing."""
    normalized = normalize_language_name(token)
    matches = []
    for language in queryset:
        values = (language.key, language.short_name, language.common_name, language.name)
        if any(normalize_language_name(value) == normalized for value in values):
            matches.append(language)
    return matches
