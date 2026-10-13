"""Next unused ``p######`` / ``c######`` ids from one in-memory pass.

Callers load existing values once (one query), then this finds the first hole
in the zero-padded series. That is the same numbering as the old one-by-one
``exists()`` / HTTP checks, without a round trip per candidate.
"""
import re

from django.utils import timezone

SERIES_LIMIT = 1000000


def next_prefixed_id(existing_values, prefix, width=6, fallback_prefix=None):
    """Return ``prefix + zero-padded index`` for the first unused slot."""
    existing = existing_values if isinstance(existing_values, set) else set(existing_values)
    for index in range(SERIES_LIMIT):
        candidate = '%s%0*d' % (prefix, width, index)
        if candidate not in existing:
            return candidate
    stamp = str(int(timezone.now().timestamp()))[-width:]
    return '%s%s' % (fallback_prefix or prefix, stamp)


def is_series_id(value, prefix, width=6):
    """True when ``value`` is in the auto-suggested ``p000000`` / ``c000000`` series."""
    if not value:
        return False
    return re.fullmatch('%s[0-9]{%d}' % (re.escape(prefix), width), value) is not None


def wants_auto_series(flag, value, prefix, width=6):
    """Whether a colliding id should be replaced with the next free series slot.

    ``flag`` is the client ``key_auto`` / ``code_auto`` field. Missing flags
    fall back to the id looking like an auto-suggested series value.
    """
    if flag is None:
        return is_series_id(value, prefix, width)
    if flag is True or flag == 1:
        return True
    if flag is False or flag == 0:
        return False
    text = str(flag).strip().lower()
    if text in ('1', 'true', 'yes'):
        return True
    if text in ('0', 'false', 'no', ''):
        return False
    return is_series_id(value, prefix, width)
