"""Whether the current assignment session hides sandbox stdout from this user."""

from judge.models.contest import ContestParticipation


def user_is_assignment_staff(user, contest):
    """Authors, curators, testers, superusers, and course teachers bypass hide."""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    if profile.id in contest.editor_ids or profile.id in contest.tester_ids:
        return True
    return contest.is_viewable_by_course_teacher(user)


def should_hide_sandbox_output(user):
    """True only for a non-spectate session with the switch on, for students.

    Not joined, switch off, spectators, and assignment staff see full stdout.
    """
    if not user.is_authenticated:
        return False
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    participation = profile.current_contest
    if participation is None:
        return False
    if participation.virtual == ContestParticipation.SPECTATE:
        return False
    contest = participation.contest
    if not contest.hide_sandbox_output:
        return False
    if user_is_assignment_staff(user, contest):
        return False
    return True
