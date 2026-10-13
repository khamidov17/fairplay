"""Whether the current assignment session hides sandbox stdout from this user."""

from judge.models.contest import ContestParticipation

# Same value as judge.views.ide.IDE_PROBLEM_CODE (imported from here would be circular).
IDE_PROBLEM_CODE = 'idepractice'


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


def _participation_hides(participation, user):
    if participation.virtual != ContestParticipation.LIVE or participation.ended:
        return False
    contest = participation.contest
    if not contest.hide_sandbox_output or not contest.started or contest.ended:
        return False
    return not user_is_assignment_staff(user, contest)


def sandbox_hide_contest_for_new_run(user):
    """The assignment that hides the output of a Run this user starts now, or ``None``.

    Recorded on the run (``Submission.sandbox_hidden_contest``) when it is created. It is
    the current assignment when that one hides output, and otherwise any other live
    participation that is still running with the switch on: leaving the assignment,
    running code, and rejoining must not reveal the output either.
    """
    if not user.is_authenticated:
        return None
    profile = getattr(user, 'profile', None)
    if profile is None:
        return None
    if should_hide_sandbox_output(user):
        return profile.current_contest.contest
    candidates = ContestParticipation.objects.filter(
        user=profile, virtual=ContestParticipation.LIVE, contest__hide_sandbox_output=True,
    ).select_related('contest')
    for participation in candidates:
        if _participation_hides(participation, user):
            return participation.contest
    return None


def submission_output_hidden_for(submission, user):
    """Whether ``user`` must not see the stdout of this IDE / workspace Run.

    True when the run was created while an assignment hid sandbox output and that
    assignment has not ended (its staff still see it), or when the viewer is the owner
    and is in such an assignment right now.
    """
    if submission.problem.code != IDE_PROBLEM_CODE:
        return False
    contest_id = getattr(submission, 'sandbox_hidden_contest_id', None)
    if contest_id is not None:
        contest = submission.sandbox_hidden_contest
        if not contest.ended and not user_is_assignment_staff(user, contest):
            return True
    if user.is_authenticated and submission.user_id == getattr(getattr(user, 'profile', None), 'id', None):
        return should_hide_sandbox_output(user)
    return False
