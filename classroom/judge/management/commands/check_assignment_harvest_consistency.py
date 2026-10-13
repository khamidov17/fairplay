"""Read-only audit of assignment auto-submit: the answers it collected on paper but not in fact.

Auto-submit fails in two directions and only one of them is noisy. A sweep that never ran leaves
drafts sitting past their deadline, and the next sweep picks them up; that is self-healing, and
``harvest_assignment_drafts`` already exits nonzero when it hits one. The quiet direction is a
draft that was claimed and produced nothing. ``harvested_at`` is exactly what tells every later
sweep to skip a draft, so a claim with no submission behind it is an answer that is marked
collected, will never be looked at again, and appears nowhere a human would look: not in the
student's submission list, not on the scoreboard, not in any error count. It has happened in
production -- a file draft whose object-store read raised outside the exception set the harvester
caught, leaving the claim written and the submission never created -- and it stayed invisible
until somebody went looking. That is the case this command exists to make loud.

It reads the same two tables the harvester writes and never writes to either, so it is safe to
point at production, and it exits nonzero on any finding so cron MAILTO doubles as the alert
channel -- the contract ``harvest_assignment_drafts`` already uses.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max, Q
from django.utils import timezone

from judge.models import AssignmentDraft, ContestSubmission


def _claims_without_submissions(drafts):
    """Yield drafts whose claim produced no submission.

    "No submission" has to mean "none dated at or after the claim" rather than "none at all".
    Submitting deletes the draft, so a draft that outlived a submission is one the student edited
    afterwards, and its problem legitimately already carries that older submission; testing for
    merely *any* submission therefore passes precisely the drafts that lost work this way.

    The existence test is one aggregate query over the whole claimed set rather than a lookup per
    draft, because the answer needed here is a single date per (participation, problem) and this
    command is meant to be pointed at an entire database.
    """
    latest = {
        (row['participation_id'], row['problem__problem_id']): row['latest']
        for row in (ContestSubmission.objects
                    .filter(participation_id__in=drafts.values('participation_id'))
                    .values('participation_id', 'problem__problem_id')
                    .annotate(latest=Max('submission__date')))
    }
    for draft in drafts.iterator():
        newest = latest.get((draft.participation_id, draft.problem_id))
        if newest is None or newest < draft.harvested_at:
            yield draft


def _uncollected_past_deadline(drafts, cutoff):
    """Yield drafts still unclaimed after their deadline, meaning nothing ever collected them.

    A personal time limit makes the deadline a per-row Python computation
    (``ContestParticipation.end_time``), so SQL can only narrow to "the assignment is over" plus
    "the assignment is time-limited, so somebody's window may be" -- the same narrowing
    ``harvest_assignment_drafts`` makes, for the same reason.

    ``has_content()`` is left until after the deadline test on purpose: for a code draft it reads
    the problem's starter template, and deciding the deadline first keeps that read off every
    draft of every exam currently in progress, which is most of this table on a busy day.
    """
    candidates = drafts.filter(Q(participation__contest__end_time__lt=cutoff) |
                               Q(participation__contest__time_limit__isnull=False))
    for draft in candidates.iterator():
        end_time = draft.participation.end_time
        if end_time is None or end_time >= cutoff:
            continue
        if draft.has_content():
            yield draft


def _submissions_for(drafts):
    """Map (participation, problem) to its ``[(submission id, date)]``, for reporting only.

    Only the handful of drafts already known to be broken are looked up, so this stays one small
    query no matter how large the table is.
    """
    if not drafts:
        return {}
    found = {}
    rows = (ContestSubmission.objects
            .filter(participation_id__in={draft.participation_id for draft in drafts},
                    problem__problem_id__in={draft.problem_id for draft in drafts})
            .values_list('participation_id', 'problem__problem_id', 'submission_id', 'submission__date')
            .order_by('submission_id'))
    for participation_id, problem_id, submission_id, date in rows:
        found.setdefault((participation_id, problem_id), []).append((submission_id, date))
    return found


class Command(BaseCommand):
    help = ('Audit assignment auto-submit for answers it lost: drafts it marked collected without '
            'creating a submission, and drafts whose deadline passed without being collected at '
            'all. Read-only, so it is safe against production. Exits nonzero when it finds '
            'either, so cron MAILTO doubles as the alert channel (recommended: hourly).')

    def add_arguments(self, parser):
        parser.add_argument('--grace-minutes', type=int, default=30, metavar='N',
                            help='only treat a passed deadline as missed after N minutes (default '
                                 '30); the recovery sweep is meant to run every 10, so anything '
                                 'younger than a few sweeps is work in flight rather than a loss')
        parser.add_argument('--contest', metavar='KEY',
                            help='restrict the audit to one assignment, by contest key')

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(minutes=options['grace_minutes'])

        drafts = AssignmentDraft.objects.all()
        if options['contest']:
            drafts = drafts.filter(participation__contest__key=options['contest'])

        claimed = (drafts.filter(harvested_at__isnull=False)
                   .select_related('problem', 'participation__contest', 'participation__user__user')
                   .order_by('pk'))
        # ``upload`` and ``language`` are pulled in because has_content() consults both, and a
        # reverse one-to-one it has to fetch per row is the one N+1 this command could still hit.
        pending = (drafts.filter(harvested_at__isnull=True)
                   .select_related('problem', 'language', 'upload',
                                   'participation__contest', 'participation__user__user')
                   .order_by('pk'))

        lost = list(_claims_without_submissions(claimed))
        missed = list(_uncollected_past_deadline(pending, cutoff))

        if lost:
            self._report('CLAIMED BUT NEVER SUBMITTED (marked collected; no sweep will retry these):',
                         lost, _submissions_for(lost))
        if missed:
            self._report('PAST DEADLINE AND STILL UNCOLLECTED (substantive answers nothing has taken):',
                         missed, _submissions_for(missed))

        claimed_count, pending_count = claimed.count(), pending.count()
        if lost or missed:
            raise CommandError(f'{len(lost)} claimed draft(s) produced no submission, '
                               f'{len(missed)} draft(s) past deadline uncollected')
        self.stdout.write(self.style.SUCCESS(
            f'OK: {claimed_count} claimed draft(s) all have a submission, '
            f'{pending_count} unclaimed draft(s) none of them a missed collection',
        ))

    def _report(self, heading, drafts, submissions):
        self.stdout.write(heading)
        for draft in drafts:
            participation = draft.participation
            self.stdout.write(
                f'  draft {draft.pk} {participation.contest.key}/{participation.user.user.username} '
                f'participation={participation.pk} {draft.problem.code} '
                f'({draft.problem.submission_type}/{draft.problem.get_submission_type_display()}) '
                f'claimed={draft.harvested_at} deadline={participation.end_time}',
            )
            existing = submissions.get((draft.participation_id, draft.problem_id))
            self.stdout.write('    submissions on this problem: ' + (
                ', '.join(f'{submission_id} @ {date}' for submission_id, date in existing)
                if existing else '(none)'))
        self.stdout.write('')
