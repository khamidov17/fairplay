"""Student AI grading quota and per-problem rate limiting.

Students get a global request quota (for usage monitoring) plus a sliding
per-problem rate limit: at most ``rate_limit_count()`` calls in
``rate_window_seconds()``. Hitting the limit returns "try again later";
there is no separate cooldown / ban — the window keeps counting.

Both the window length and the per-window count are configurable via
``DMOJ_AI_GRADING_RATE_WINDOW_SECONDS`` / ``DMOJ_AI_GRADING_RATE_LIMIT``.
"""
from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.cache import cache

from judge.utils.cache_mutex import cache_mutex

logger = logging.getLogger(__name__)


def rate_window_seconds() -> int:
    """Length of one rate-limit window in seconds (5 minutes by default)."""
    return int(getattr(settings, 'DMOJ_AI_GRADING_RATE_WINDOW_SECONDS', 300))


def rate_limit_count() -> int:
    """Max on-demand AI grading requests a student may make within one window."""
    return int(getattr(settings, 'DMOJ_AI_GRADING_RATE_LIMIT', 5))


def _quota_key(user_id: int) -> str:
    return f'ai_grade_quota:{user_id}'


def _rate_key(user_id: int, problem_id: int) -> str:
    return f'ai_grade_rate:{user_id}:{problem_id}'


def _rate_lock_key(user_id: int, problem_id: int) -> str:
    return f'ai_grade_rate:{user_id}:{problem_id}:lock'


def student_quota_limit() -> int:
    return int(getattr(settings, 'DMOJ_AI_GRADING_STUDENT_QUOTA', 0))


def is_teacher_viewer(user, submission) -> bool:
    """Whether ``user`` counts as a teacher for AI grading purposes.

    Permission-based only — never bare ``is_staff`` and never group/roster
    membership. A user is a teacher when they hold an explicit grading or
    course-editing permission (rejudge, edit all contests, or create course),
    or when they are an editor (author/curator) of the contest the submission
    belongs to (``is_editable_by`` is itself permission-first and falls back to
    contest membership for course instructors).
    """
    if user is None or not user.is_authenticated:
        return False
    if user.has_perm('judge.rejudge_submission') or \
            user.has_perm('judge.edit_all_contest') or \
            user.has_perm('judge.add_organization'):
        return True
    try:
        contest = submission.contest_object
        if contest is not None and contest.is_editable_by(user):
            return True
    except Exception:
        pass
    return False


def _recent_timestamps(user_id: int, problem_id: int) -> list[float]:
    now = time.time()
    timestamps = list(cache.get(_rate_key(user_id, problem_id)) or [])
    return [t for t in timestamps if now - t < rate_window_seconds()]


def is_rate_limited(user_id: int, problem_id: int) -> bool:
    """True when this user already hit the window limit for this problem."""
    return len(_recent_timestamps(user_id, problem_id)) >= rate_limit_count()


def rate_limit_remaining_seconds(user_id: int, problem_id: int) -> int:
    """Seconds until the oldest call in the window ages out (0 if not limited)."""
    recent = _recent_timestamps(user_id, problem_id)
    if len(recent) < rate_limit_count():
        return 0
    oldest = min(recent)
    return max(0, int(rate_window_seconds() - (time.time() - oldest)) + 1)


def rate_limit_remaining_count(user_id: int, problem_id: int) -> int:
    """How many more on-demand AI grading calls the user may make this window."""
    recent = _recent_timestamps(user_id, problem_id)
    return max(0, rate_limit_count() - len(recent))


def get_remaining(user_id: int) -> int:
    """How many AI grading requests this student still has (global quota)."""
    limit = student_quota_limit()
    if limit <= 0:
        return 0
    used = cache.get(_quota_key(user_id)) or 0
    return max(0, limit - used)


def try_reserve(user_id: int, problem_id: int) -> tuple[bool, str | None]:
    """Claim one AI grading slot; returns (ok, error_message).

    The per-problem window is a cached list, so its read-check-append runs under
    a cache mutex: parallel POSTs cannot all read the same short list and pass.
    The global quota uses ``add`` + ``incr``, which are atomic.
    """
    with cache_mutex(_rate_lock_key(user_id, problem_id)):
        recent = _recent_timestamps(user_id, problem_id)
        if len(recent) >= rate_limit_count():
            secs = rate_limit_remaining_seconds(user_id, problem_id)
            return False, f'rate_limited:{secs}'

        limit = student_quota_limit()
        if limit <= 0:
            return False, 'quota_disabled'

        key = _quota_key(user_id)
        cache.add(key, 0, None)
        try:
            used = cache.incr(key)
        except ValueError:
            # Evicted between add() and incr(); count this request as the first.
            cache.add(key, 1, None)
            used = 1
        if used > limit:
            refund(user_id)
            return False, 'quota_exhausted'

        now = time.time()
        recent.append(now)
        cache.set(_rate_key(user_id, problem_id), recent, rate_window_seconds())
        return True, None


def refund(user_id: int) -> None:
    try:
        cache.decr(_quota_key(user_id))
    except ValueError:
        pass
