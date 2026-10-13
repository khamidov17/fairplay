"""Celery tasks that collect saved answers when an assignment closes.

Collecting a whole class is deliberately split in two. The contest-level task only *finds* the
participations that still have drafts and queues one task each; the per-participation task does the
work. A single task that created every submission for a 200-student class would hold one worker for
the length of the class and lose the whole assignment if it died halfway.
"""
import logging

from celery import shared_task
from django.utils import timezone

from judge.models import AssignmentDraft, Contest, ContestParticipation
from judge.utils.assignment_harvest import (
    assignment_harvest_enabled, harvest_participation, participation_deadline_has_arrived,
)

__all__ = ('harvest_contest_drafts', 'harvest_participation_drafts')

logger = logging.getLogger('judge.assignment_harvest')


@shared_task
def harvest_participation_drafts(participation_id):
    """Auto-submit one participation's remaining drafts. Idempotent.

    Refuses to collect while the student's window is still open, so a leftover ETA from before a
    deadline change cannot submit their paper early.
    """
    participation = (
        ContestParticipation.objects
        .select_related('contest', 'user__user')
        .filter(pk=participation_id)
        .first()
    )
    if participation is None:
        return 0
    if not assignment_harvest_enabled():
        return 0
    if not participation_deadline_has_arrived(participation):
        return 0
    return len(harvest_participation(participation))


@shared_task
def harvest_contest_drafts(contest_key):
    """Queue a harvest for every participation in this assignment that still has a draft.

    Participations without drafts are never queued, so an assignment where everybody submitted by
    hand costs one query rather than one task per student.

    A shared-deadline assignment whose end is still in the future is a no-op: the usual cause is
    a revoked-too-late ETA after the teacher moved the deadline back.
    """
    contest = Contest.objects.filter(key=contest_key).first()
    if contest is None:
        return 0
    if not assignment_harvest_enabled():
        return 0
    if contest.time_limit is None and contest.end_time > timezone.now():
        return 0

    participation_ids = list(
        AssignmentDraft.objects
        .filter(participation__contest__key=contest_key, harvested_at__isnull=True)
        .values_list('participation_id', flat=True)
        .distinct(),
    )
    for participation_id in participation_ids:
        harvest_participation_drafts.delay(participation_id)
    return len(participation_ids)
