"""Scheduling the auto-submit that runs when an assignment closes.

The trigger is the moment the assignment closes *for a given student*, which is not one moment for
everybody: with a personal time limit each student's window ends at their own ``end_time``. So
assignments without a time limit get one task at ``contest.end_time`` that fans out, and
assignments with one get a task per participation.

Task ids are stored on the row they belong to so a deadline change can revoke the old task before
scheduling the new one — the same pattern as ``schedule_batch_ai_grade``. Celery holds an ETA task
in worker memory, so a restart can drop it; ``harvest_assignment_drafts`` exists as the
cron-driven backstop and none of this is the only line of defence.
"""
import logging

from django.conf import settings
from django.utils import timezone

from judge.utils.assignment_harvest import assignment_harvest_enabled

__all__ = [
    'schedule_contest_harvest', 'schedule_participation_harvest', 'reschedule_assignment_harvest',
]

logger = logging.getLogger('judge.assignment_harvest')


def _revoke(task_id):
    if not task_id:
        return
    try:
        from celery.result import AsyncResult
        AsyncResult(task_id).revoke()
    except Exception:
        logger.exception('Could not revoke previous auto-submit task %s', task_id)


def _can_schedule(eta):
    if eta is None or eta <= timezone.now():
        return False
    # An eager worker would run the task immediately, collecting drafts long before the deadline.
    return not getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False)


def schedule_contest_harvest(contest_id):
    """(Re)schedule the assignment-wide auto-submit at ``end_time``.

    A contest-level task is only right when every student shares one deadline. With a personal
    time limit the per-participation tasks own the scheduling, and this clears the shared one.
    """
    from judge.models import Contest

    contest = Contest.objects.filter(pk=contest_id).first()
    if contest is None:
        return
    _revoke(contest.harvest_scheduled_task_id)
    contest.harvest_scheduled_task_id = ''

    if assignment_harvest_enabled() and contest.time_limit is None and _can_schedule(contest.end_time):
        try:
            from judge.tasks.assignment_harvest import harvest_contest_drafts
            result = harvest_contest_drafts.apply_async(args=[contest.key], eta=contest.end_time)
            contest.harvest_scheduled_task_id = result.id or ''
        except Exception:
            logger.exception('Could not schedule auto-submit for contest %s', contest.key)
    contest.save(update_fields=['harvest_scheduled_task_id'])


def schedule_participation_harvest(participation_id):
    """(Re)schedule one student's auto-submit at their own ``end_time``.

    Only used when the assignment has a personal time limit; otherwise the contest-level task
    covers this student and a second task would just race it.
    """
    from judge.models import ContestParticipation

    participation = (
        ContestParticipation.objects.select_related('contest').filter(pk=participation_id).first()
    )
    if participation is None:
        return
    _revoke(participation.harvest_scheduled_task_id)
    participation.harvest_scheduled_task_id = ''

    if assignment_harvest_enabled() and participation.contest.time_limit is not None \
            and not participation.spectate and _can_schedule(participation.end_time):
        try:
            from judge.tasks.assignment_harvest import harvest_participation_drafts
            result = harvest_participation_drafts.apply_async(
                args=[participation.pk], eta=participation.end_time,
            )
            participation.harvest_scheduled_task_id = result.id or ''
        except Exception:
            logger.exception('Could not schedule auto-submit for participation %s', participation.pk)
    ContestParticipation.objects.filter(pk=participation.pk).update(
        harvest_scheduled_task_id=participation.harvest_scheduled_task_id,
    )


def reschedule_assignment_harvest(contest_id):
    """Re-point auto-submit after a change to ``end_time`` or ``time_limit``.

    Both levels are redone, because the change may have moved the assignment between the two
    scheduling modes: turning a time limit on has to drop the shared task and create per-student
    ones, and turning it off has to do the reverse.
    """
    from judge.models import Contest, ContestParticipation

    schedule_contest_harvest(contest_id)
    contest = Contest.objects.filter(pk=contest_id).first()
    if contest is None:
        return

    participations = ContestParticipation.objects.filter(contest_id=contest_id)
    if contest.time_limit is None:
        # Nothing per-student to schedule; only participations left over from when the assignment
        # did have a time limit need their stale task revoked.
        participations = participations.exclude(harvest_scheduled_task_id='')
    for participation_id in list(participations.values_list('pk', flat=True)):
        schedule_participation_harvest(participation_id)
