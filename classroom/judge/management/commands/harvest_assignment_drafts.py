"""Recovery sweep for assignment auto-submit.

The deadline itself is handled by Celery ETA tasks, and an ETA task lives in worker memory: a
worker restart between scheduling and the deadline drops it silently, and the students it was
holding never get collected. Losing a class's answers to a worker restart is not an acceptable
failure mode, so the ETA tasks are the fast path and this sweep is the one that must not miss.

It is driven by the draft table for the same reason ``harvest_participation`` is: an un-harvested
draft on a participation whose deadline has passed *is* the definition of a missed collection, so
nothing has to be reconstructed or compared. Harvesting is idempotent, so overlapping with a
deadline task that did fire costs a query and changes nothing.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from judge.models import AssignmentDraft, ContestParticipation
from judge.utils.assignment_harvest import assignment_harvest_enabled, harvest_participation


class Command(BaseCommand):
    help = ('Collect saved answers that auto-submit missed: submits the drafts still held by '
            'participations whose deadline has already passed. Safe to run at any time and '
            'safe to overlap with the scheduled deadline tasks, because collecting a draft is '
            'idempotent. Run from cron (recommended: */10). Exits nonzero when a participation '
            'could not be collected, so cron MAILTO doubles as the alert channel. '
            'Host crontab (adjust the manage.py path): '
            '*/10 * * * * cd /app && python3 manage.py harvest_assignment_drafts '
            '&& python3 manage.py check_assignment_harvest_consistency')

    def add_arguments(self, parser):
        parser.add_argument('--grace-minutes', type=int, default=5, metavar='N',
                            help='only collect deadlines that passed more than N minutes ago '
                                 '(default 5), so the sweep does not race the deadline task')
        parser.add_argument('--limit', type=int, default=500, metavar='N',
                            help='collect at most N participations per run (default 500); the '
                                 'remainder is reported and picked up by the next run')
        parser.add_argument('--contest', metavar='KEY',
                            help='restrict the sweep to one assignment, by contest key')

    def handle(self, *args, **options):
        if not assignment_harvest_enabled():
            self.stdout.write('ASSIGNMENT_HARVEST_ENABLED is off; not collecting')
            return

        cutoff = timezone.now() - timedelta(minutes=options['grace_minutes'])
        limit = options['limit']

        pending = AssignmentDraft.objects.filter(harvested_at__isnull=True)
        if options['contest']:
            pending = pending.filter(participation__contest__key=options['contest'])

        # A personal time limit makes the deadline a per-row Python computation
        # (``ContestParticipation.end_time``), so SQL can only narrow to "the assignment is over"
        # plus "the assignment is time-limited, so somebody's window may be". The exact decision
        # is made below, per participation.
        candidates = (ContestParticipation.objects
                      .filter(pk__in=pending.values_list('participation_id', flat=True).distinct())
                      .filter(Q(contest__end_time__lt=cutoff) | Q(contest__time_limit__isnull=False))
                      .select_related('contest', 'user__user')
                      .order_by('pk'))

        swept = submitted = errors = overflow = 0
        for participation in candidates.iterator():
            if participation.end_time is None or participation.end_time >= cutoff:
                # Still open for this student; their deadline task has not come due yet.
                continue
            if swept >= limit:
                # Counted rather than collected, so --limit caps the work done and the sweep
                # still walks the whole backlog instead of stalling on the same head every run.
                overflow += 1
                continue
            swept += 1
            try:
                submitted += len(harvest_participation(participation))
            except Exception as e:
                errors += 1
                self.stderr.write(f'{participation.contest.key}/{participation.user.username}: '
                                  f'{type(e).__name__}: {e}')

        if overflow:
            self.stdout.write(f'--limit {limit} reached; {overflow} participation(s) still due, '
                              f'the next run continues from there')
        self.stdout.write(f'swept={swept} submitted={submitted} errors={errors}')

        if errors:
            raise CommandError(f'{errors} participation(s) could not be collected')
