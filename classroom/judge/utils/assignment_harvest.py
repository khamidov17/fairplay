"""Turn the drafts a participation still has into real submissions.

This is the "collect the papers" step. It is driven by the draft table rather than by the problem
list, because a successful manual submit deletes its draft: a draft that is still there means the
problem was never submitted, or was submitted and then edited. Those are exactly the two cases a
student loses marks on today, and nothing else has to be compared or guessed at.

Harvesting can be triggered more than once for the same participation — Leave, a deadline task
and the recovery sweep can overlap — so each draft is claimed with a compare-and-set on
``harvested_at`` before a submission is created. A claim that fails to produce a submission is released again,
because a duplicate submission is a far smaller problem than an answer that silently disappeared.
"""
import logging

from django.db.models import Count
from django.utils import timezone

from judge.models import AssignmentDraft, ContestProblem, ContestSubmission, Language, ProblemSubmissionType
from judge.utils.assignment_draft import draft_answer
from judge.utils.submission_create import create_submission

__all__ = [
    'assignment_harvest_enabled', 'harvest_participation',
    'participation_deadline_has_arrived', 'participation_is_harvestable',
    'user_bypasses_assignment_rules',
]


def assignment_harvest_enabled():
    """Whether drafts may be turned into submissions.

    Draft saving does not consult this: students still get a server copy while
    they type. Every *trigger* (Leave, deadline task, recovery sweep, pre-grade
    harvest) must, so a leftover Celery ETA cannot collect after the switch
    is flipped off.
    """
    from django.conf import settings
    return bool(getattr(settings, 'ASSIGNMENT_HARVEST_ENABLED', False))


logger = logging.getLogger('judge.assignment_harvest')


def user_bypasses_assignment_rules(user, contest):
    """Staff, teachers, testers and spectators are not sitting the assignment for a grade."""
    if user is None or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if contest.is_editable_by(user):
        return True
    profile_id = user.profile.id
    if profile_id in contest.editor_ids or profile_id in contest.tester_ids or profile_id in contest.spectator_ids:
        return True
    if contest.is_viewable_by_org_admin(user) or contest.is_viewable_by_course_teacher(user):
        return True
    return False


def participation_deadline_has_arrived(participation, now=None):
    """Whether this student's answering window is already closed.

    Scheduled deadline tasks can fire after a teacher moved the deadline later, if revoking the
    old Celery ETA failed. Collecting then would submit work while the student is still writing.
    ``ended`` uses a strict ``<``, so a task scheduled at the exact ``end_time`` would skip; ``<=``
    is the check that matches "the deadline has arrived".
    """
    if now is None:
        now = timezone.now()
    end = participation.end_time
    return end is not None and end <= now


def participation_is_harvestable(participation):
    """Whether this participation's work counts towards a grade at all."""
    if participation.spectate:
        return False
    if participation.is_disqualified:
        return False
    # Staff who read the assignment with bypass rights are not sitting it.
    return not user_bypasses_assignment_rules(participation.user.user, participation.contest)


def _claim(draft, now):
    """Mark ``draft`` harvested, but only if nobody else already did. True if we won the claim."""
    return AssignmentDraft.objects.filter(pk=draft.pk, harvested_at__isnull=True).update(harvested_at=now) == 1


def _release(draft):
    AssignmentDraft.objects.filter(pk=draft.pk).update(harvested_at=None)


def _answer_checker_for_open_exam(contest_problem, problem, participation):
    """Whether handing in this draft would grade, with the verdict shown, a problem that an open
    exam hides results for (G-7, see ``ProblemSubmit._submission_blocked_response``)."""
    from judge.utils.result_visibility import contest_problem_hides_results, problem_in_active_hidden_exam
    if contest_problem_hides_results(contest_problem):
        return False
    if not problem_in_active_hidden_exam(problem):
        return False
    return not problem.is_editable_by(participation.user.user)


def harvest_participation(participation):
    """Submit every substantive draft ``participation`` still holds; return the submissions made."""
    if not participation_is_harvestable(participation):
        return []

    drafts = list(
        AssignmentDraft.objects
        .filter(participation=participation, harvested_at__isnull=True)
        .select_related('problem', 'language'),
    )
    if not drafts:
        return []

    contest_problems = {
        contest_problem.problem_id: contest_problem
        for contest_problem in ContestProblem.objects.filter(contest_id=participation.contest_id)
    }
    used = dict(
        ContestSubmission.objects
        .filter(participation=participation)
        .exclude(submission__status='IE')
        .values_list('problem_id')
        .annotate(Count('id')),
    )

    now = timezone.now()
    created = []
    for draft in drafts:
        contest_problem = contest_problems.get(draft.problem_id)
        if contest_problem is None:
            # The problem was removed from the assignment after the student answered it.
            continue
        if not draft.has_content():
            continue
        max_submissions = contest_problem.max_submissions
        if max_submissions and used.get(contest_problem.id, 0) >= max_submissions:
            continue
        if _answer_checker_for_open_exam(contest_problem, draft.problem, participation):
            # G-7: same rule as a direct submit. The draft stays a draft; a later sweep (after
            # the exam) can still hand it in.
            continue

        if not _claim(draft, now):
            continue
        try:
            answer = draft_answer(draft)
        except Exception:
            # Not just DraftContentError: reading a file draft goes to the object store, which can
            # fail in ways this module has no list of. Letting one escape would leave the claim on
            # a draft that produced no submission — an answer nobody can recover, since the claim
            # is what tells every later sweep to skip it — and abandon the drafts this
            # participation has left, which is the whole rest of the student's paper.
            logger.exception('Unusable draft %d for participation %d', draft.pk, participation.pk)
            _release(draft)
            continue

        is_code = draft.problem.submission_type == ProblemSubmissionType.CODE
        try:
            submission = create_submission(
                participation.user, draft.problem,
                language=draft.language if is_code else Language.get_answer_language(),
                answer=answer,
                participation=participation, contest_problem=contest_problem,
            )
        except Exception:
            logger.exception('Auto-submit failed for draft %d, participation %d', draft.pk, participation.pk)
            _release(draft)
            continue

        # The submission is now the record of what the student wrote, so the draft body is no
        # longer needed; the row stays as evidence of when the answer was collected.
        AssignmentDraft.objects.filter(pk=draft.pk).update(content='')
        upload = getattr(draft, 'upload', None)
        if upload is not None:
            # The bytes were copied into the SubmissionFile above, so this second copy is now
            # just a bill and a second place to leak unsubmitted work from.
            upload.delete()
        used[contest_problem.id] = used.get(contest_problem.id, 0) + 1
        created.append(submission)

    return created
