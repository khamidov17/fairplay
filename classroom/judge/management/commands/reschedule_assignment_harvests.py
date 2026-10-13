"""Point auto-submit ETA tasks at every assignment that is still open.

New assignments get this when they are saved. Assignments that were already running when
auto-submit shipped do not, until a teacher edits them. Run once after deploy so those classes
get a deadline task without waiting for an edit. Safe to repeat: each call revokes the previous
task id and writes a new one.

Already-ended assignments are skipped. Their leftover drafts are the recovery sweep's job
(``harvest_assignment_drafts``), not an ETA in the past.
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from judge.models import Contest
from judge.utils.assignment_schedule import reschedule_assignment_harvest


class Command(BaseCommand):
    help = ('Schedule auto-submit for assignments that have not ended yet. Run once after '
            'deploying auto-submit so in-progress classes get a deadline task. Also used when '
            'Celery was restarted and the in-memory ETAs were lost — though '
            'harvest_assignment_drafts is the backstop that must not be missing.')

    def add_arguments(self, parser):
        parser.add_argument('--contest', metavar='KEY',
                            help='restrict to one assignment, by contest key')
        parser.add_argument('--dry-run', action='store_true',
                            help='list the assignments that would be scheduled, without writing')

    def handle(self, *args, **options):
        contests = Contest.objects.filter(end_time__gt=timezone.now()).order_by('end_time')
        if options['contest']:
            contests = contests.filter(key=options['contest'])
            if not contests.exists():
                raise CommandError('No open assignment with that key.')

        scheduled = 0
        for contest in contests.iterator():
            if options['dry_run']:
                self.stdout.write(f'{contest.key} end={contest.end_time} '
                                  f'time_limit={contest.time_limit}')
                scheduled += 1
                continue
            reschedule_assignment_harvest(contest.id)
            scheduled += 1

        verb = 'would schedule' if options['dry_run'] else 'scheduled'
        self.stdout.write(f'{verb} {scheduled} open assignment(s)')
